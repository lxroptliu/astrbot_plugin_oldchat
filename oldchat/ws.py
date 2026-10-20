"""oldchat/ws.py - OldChat WebSocket 客户端（v1/v2 共用，profile 驱动）。

* 加密通道由 ``crypto.ECDHSession`` 负责；
* 本模块负责：握手、WS URL 构造、应用层心跳（间隔可配）、
  指数退避重连、按 profile 过滤帧类型、记录 ``pts`` 供 v2 重连同步；
* 所有日志经 ``_redact`` 脱敏。
"""
from __future__ import annotations

import asyncio
import json
import random
from typing import Awaitable, Callable, Optional
from urllib.parse import urlsplit

import websockets

from astrbot import logger

from ._redact import redact, safe_url
from .crypto import ECDHSession


# 协议 profile：v1 / v2 的握手路径、URL 参数、认证方式、帧类型集合
_WS_PROFILES: dict[str, dict] = {
    "v1": {
        "handshake_path": "/v1/auth/handshake",
        "url_params": ("sid",),                 # ?sid={session_id}
        "auth_header": True,                    # + Authorization: Bearer {token}
        "message_types": ("group_message", "direct_message"),
        "supports_pts": False,
    },
    "v2": {
        # 阶段 0 抓帧确认；先按文档给默认值
        "handshake_path": "/auth/handshake",
        "url_params": ("token", "session"),     # ?token=xxx&session=yyy
        "auth_header": False,                   # token 走 query，无需 header
        "message_types": ("group_message", "direct_message", "new_message"),
        "supports_pts": True,
    },
}


class OldChatWS:
    """OldChat WebSocket 客户端（v1/v2，加密通道共用）。"""

    def __init__(
        self,
        base_url: str,
        access_token: str,
        device_id: str,
        version: str = "v1",
        ping_interval: int = 25,
        reconnect_backoff: bool = True,
        on_unauthorized: Optional[Callable[[], Awaitable[None]]] = None,
        on_reconnect: Optional[Callable[[int], Awaitable[None]]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.device_id = device_id
        self.version = version if version in _WS_PROFILES else "v1"
        self.profile = _WS_PROFILES[self.version]
        self.ping_interval = max(5, int(ping_interval or 25))
        self.reconnect_backoff = reconnect_backoff
        self._on_unauthorized = on_unauthorized
        self._on_reconnect = on_reconnect
        self._message_handler: Optional[Callable[[dict], Awaitable[None]]] = None
        self._ws = None
        self._terminating = False
        self._last_pts: int = 0
        self._first_connect = True
        self._session: Optional[ECDHSession] = None
        self._backoff: float = 1.0

    def on_message(self, handler: Callable[[dict], Awaitable[None]]) -> None:
        self._message_handler = handler

    async def terminate(self) -> None:
        self._terminating = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    # ---------------- 握手 / URL / Header ---------------- #

    async def _ensure_session(self) -> bool:
        self._session = ECDHSession(self.base_url)
        return await self._session.handshake(self.profile["handshake_path"])

    def _build_url(self) -> str:
        assert self._session is not None and self._session.session_id
        p = urlsplit(self.base_url)
        scheme = "wss" if p.scheme == "https" else "ws"
        params: list[str] = []
        for name in self.profile["url_params"]:
            if name == "sid":
                params.append(f"sid={self._session.session_id}")
            elif name == "session":
                params.append(f"session={self._session.session_id}")
            elif name == "token":
                params.append(f"token={self.access_token}")
            elif name == "device_id":
                params.append(f"device_id={self.device_id}")
        return f"{scheme}://{p.netloc}/v1/ws?" + "&".join(params)

    def _build_headers(self) -> Optional[dict]:
        """按 profile 决定是否附加 Authorization 头。"""
        if self.profile.get("auth_header"):
            return {"Authorization": f"Bearer {self.access_token}"}
        return None

    # ---------------- 心跳 ---------------- #

    async def _heartbeat(self, ws) -> None:
        """应用层心跳：加密通道内发送 ``{"type":"ping"}``。"""
        while not self._terminating:
            await asyncio.sleep(self.ping_interval)
            try:
                if self._session is None:
                    return
                frame = await self._session.encrypt(json.dumps({"type": "ping"}))
                if not frame:
                    return
                await ws.send(frame)
            except Exception:
                return

    # ---------------- 重连退避 ---------------- #

    def _next_sleep(self) -> float:
        if not self.reconnect_backoff:
            return 3.0
        base = self._backoff
        jitter = base * 0.2 * (random.random() * 2 - 1)
        sleep = max(1.0, min(60.0, base + jitter))
        self._backoff = min(60.0, base * 2)
        return sleep

    # ---------------- 主循环 ---------------- #

    async def connect(self) -> None:
        """持续连接 OldChat WebSocket，直到 ``terminate()`` 被调用。"""
        while not self._terminating:
            try:
                ok = await self._ensure_session()
                if not ok:
                    raise Exception("握手失败")

                url = self._build_url()
                headers = self._build_headers()
                logger.info(
                    "[OldChat/%s] 连接 WS: %s（心跳 %ds，认证方式: %s）",
                    self.version,
                    safe_url(url),
                    self.ping_interval,
                    "header" if headers else "query",
                )

                connect_kwargs: dict = {
                    "ping_interval": None,
                    "max_size": 2 ** 22,
                }
                if headers:
                    connect_kwargs["additional_headers"] = headers

                async with websockets.connect(url, **connect_kwargs) as ws:
                    self._ws = ws
                    self._backoff = 1.0
                    hb = asyncio.create_task(self._heartbeat(ws))
                    try:
                        if (
                            not self._first_connect
                            and self.profile["supports_pts"]
                            and self._on_reconnect
                            and self._last_pts
                        ):
                            try:
                                await self._on_reconnect(self._last_pts)
                            except Exception as e:
                                logger.error(
                                    "[OldChat/%s] 重连同步失败: %s",
                                    self.version,
                                    redact(e),
                                )
                        self._first_connect = False
                        logger.info("[OldChat/%s] WS 已连接", self.version)
                        async for raw in ws:
                            await self._handle_raw(raw)
                    finally:
                        hb.cancel()
            except asyncio.CancelledError:
                logger.info("[OldChat/%s] WS 任务取消", self.version)
                break
            except websockets.exceptions.ConnectionClosed as e:
                if self._terminating:
                    break
                logger.warning(
                    "[OldChat/%s] WS 关闭(%s)，重连中", self.version, e
                )
                await asyncio.sleep(self._next_sleep())
            except Exception as e:
                if self._terminating:
                    break
                if self._is_unauthorized(e) and self._on_unauthorized:
                    logger.warning(
                        "[OldChat/%s] WS 认证失败，重登", self.version
                    )
                    # 401 通常是 token 失效；重登后短暂等待再重连，不累积 backoff
                    await asyncio.sleep(1)
                    try:
                        await self._on_unauthorized()
                        # 重登成功后重置退避，避免下次正常断线时被指数惩罚
                        self._backoff = 1.0
                    except Exception as le:
                        logger.error(
                            "[OldChat/%s] 重登失败: %s",
                            self.version,
                            redact(le),
                        )
                        await asyncio.sleep(10)
                else:
                    logger.warning(
                        "[OldChat/%s] WS 异常: %s，重连中",
                        self.version,
                        redact(e),
                    )
                    await asyncio.sleep(self._next_sleep())
        logger.info("[OldChat/%s] WS 已停止", self.version)

    # ---------------- 帧处理 ---------------- #

    async def _handle_raw(self, raw) -> None:
        if isinstance(raw, str):
            try:
                quick = json.loads(raw)
                if isinstance(quick, dict) and quick.get("type") == "pong":
                    return
            except json.JSONDecodeError:
                pass

        assert self._session is not None
        plain = await self._session.decrypt(raw)
        if not plain:
            return
        try:
            msg = json.loads(plain)
        except json.JSONDecodeError:
            return
        if not isinstance(msg, dict):
            return

        msg_type = msg.get("type", "")
        if msg_type == "pong":
            return

        pts = msg.get("pts")
        if isinstance(pts, int):
            self._last_pts = pts

        if msg_type not in self.profile["message_types"]:
            return

        if self._message_handler:
            try:
                await self._message_handler(msg)
            except Exception as e:
                logger.error(
                    "[OldChat/%s] 处理 WS 消息异常: %s",
                    self.version,
                    redact(e),
                )

    @staticmethod
    def _is_unauthorized(exc: Exception) -> bool:
        if isinstance(exc, websockets.exceptions.InvalidStatus):
            return exc.response.status_code == 401
        return "401" in str(exc)
