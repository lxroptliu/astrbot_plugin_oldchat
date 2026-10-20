"""oldchat/adapter.py - OldChat 平台适配器主体（阶段 A）。

* 通过 ``@register_platform_adapter`` 注册到 AstrBot 平台注册表；
* 按 ``api_version`` 选择 v1/v2/auto 客户端；
* 所有实验性功能均按开关裁剪，不生效版本打 warning 并忽略；
* 所有日志、异常回显、URL 一律脱敏。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import OrderedDict
from typing import Any, Optional

from astrbot import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.core.platform.astr_message_event import MessageSesion

from ._redact import mask, redact
from .client_base import OldChatClientBase
from .client_factory import create_client
from .event import OldChatPlatformEvent
from .ws import OldChatWS


# ------------------------------------------------------------------ #
# 完整配置模板（含全部实验项声明）
# ------------------------------------------------------------------ #

DEFAULT_CONFIG_TEMPLATE: dict[str, Any] = {
    # ---- 基础连接 ----
    "id": "oldchat",
    "base_url": "http://your-server:8080",
    "media_base_url": "",
    "identifier": "",
    "password": "",
    "use_websocket": True,
    "poll_interval": 5,
    "api_version": "auto",
    # ---- 心跳与重连 ----
    "ws_ping_interval": 25,
    "ws_reconnect_backoff": True,
    "login_retry_interval": 10,
    # ---- 协议兼容 ----
    "auth_path_mode": "auto",
    "password_fallback": False,
    "sync_on_reconnect": True,
    "timestamp_auto_scale": True,
    # ---- 实验性：媒体 ----
    "media_url_fallback": False,
    "media_url_candidates": "",
    "enable_file_upload": False,
    "file_upload_chunk_size": 1048576,
    "file_upload_max_size": 1073741824,
    # ---- 实验性：事件 ----
    "enable_read_receipt": False,
    "enable_recall_handling": False,
    "enable_typing_presence": False,
    # ---- 实验性：网关 ----
    "enable_gateway": False,
}


CONFIG_METADATA: dict[str, dict[str, Any]] = {
    "id": {
        "type": "string",
        "description": "适配器实例 ID，用于在 AstrBot 中唯一标识。",
        "hint": "建议保持默认或使用易识别名称。",
    },
    "base_url": {
        "type": "string",
        "description": "OldChat 服务器地址（不含 /v1/ 后缀）。",
        "hint": "例如 http://your-server:8080",
    },
    "media_base_url": {
        "type": "string",
        "description": "媒体文件基础 URL，留空则与 base_url 相同。",
    },
    "identifier": {
        "type": "string",
        "description": "登录用户名（v1 字段名 identifier，v2 内部映射为 username）。",
    },
    "password": {
        "type": "string",
        "description": "登录密码。",
        "hint": "由 AstrBot 平台层存储；适配器日志绝不打印明文。",
    },
    "use_websocket": {
        "type": "bool",
        "description": "是否使用 WebSocket 实时连接，关闭则使用 HTTP 轮询。",
    },
    "poll_interval": {
        "type": "int",
        "description": "HTTP 轮询间隔（秒），仅在 use_websocket=false 时生效。",
    },
    "api_version": {
        "type": "string",
        "description": "协议版本：v1 / v2 / auto。auto 先试 v2，端点不存在时回退 v1。",
        "hint": "切换后需重新加载平台实例。",
    },
    "ws_ping_interval": {
        "type": "int",
        "description": "WebSocket 应用层心跳间隔（秒），默认 25，最小 5。",
        "hint": "网络较差可适当调大，过小会增加流量。",
    },
    "ws_reconnect_backoff": {
        "type": "bool",
        "description": "重连是否使用指数退避（1→60s ±20% 抖动）。关闭则固定 3s。",
    },
    "login_retry_interval": {
        "type": "int",
        "description": "登录失败后的重试间隔（秒），默认 10。",
    },
    "auth_path_mode": {
        "type": "string",
        "description": "认证握手路径：auto（新路径 404 回退旧路径）/ v2 / v1。",
    },
    "password_fallback": {
        "type": "bool",
        "description": "【实验性】401 时 refresh 失败后用内存密码重新登录。",
        "hint": "默认关闭（安全优先）。密码仅在内存，绝不落盘/日志。",
    },
    "sync_on_reconnect": {
        "type": "bool",
        "description": "重连后是否按 pts 调用 /v2/updates/difference 拉增量（仅 v2 生效）。",
    },
    "timestamp_auto_scale": {
        "type": "bool",
        "description": "时间戳秒/毫秒自动识别（>1e12 视为毫秒）。",
    },
    "media_url_fallback": {
        "type": "bool",
        "description": "【实验性】媒体 URL 多候选回退。",
        "hint": "开启后图片加载失败会自动尝试备用地址。默认关闭。",
    },
    "media_url_candidates": {
        "type": "string",
        "description": "逗号分隔的自定义候选（media_url_fallback=true 时生效）。",
    },
    "enable_file_upload": {
        "type": "bool",
        "description": "【实验性】文件资源上传（仅 v2）。",
        "hint": "关闭时文件类消息会被忽略。默认关闭。",
    },
    "file_upload_chunk_size": {
        "type": "int",
        "description": "文件上传分块大小（字节），默认 1MB。",
    },
    "file_upload_max_size": {
        "type": "int",
        "description": "文件上传上限（字节），默认 1GB。",
    },
    "enable_read_receipt": {
        "type": "bool",
        "description": "【实验性】分发消息后自动发送已读回执（仅 v2）。",
    },
    "enable_recall_handling": {
        "type": "bool",
        "description": "【实验性】处理 WS recall 事件。",
    },
    "enable_typing_presence": {
        "type": "bool",
        "description": "【实验性】处理 WS typing / presence 事件。",
    },
    "enable_gateway": {
        "type": "bool",
        "description": "【实验性】走 /v2/gateway 折叠网关（仅 v2）。",
    },
}


@register_platform_adapter(
    "oldchat",
    "OldChat 平台适配器",
    default_config_tmpl=DEFAULT_CONFIG_TEMPLATE,
    config_metadata=CONFIG_METADATA,
    adapter_display_name="OldChat",
    support_streaming_message=False,
)
class OldChatAdapter(Platform):
    """OldChat 平台适配器。"""

    PLATFORM_NAME = "oldchat"
    _MAX_DEDUP = 2000

    # v2 独有开关：非 v2 版本强制关闭 + warning
    _V2_ONLY_FLAGS = {
        "sync_on_reconnect",
        "enable_file_upload",
        "enable_read_receipt",
        "enable_gateway",
    }

    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)
        self.settings = platform_settings
        self.client: Optional[OldChatClientBase] = None
        self.ws: Optional[OldChatWS] = None
        self._running = True
        self._processed_ids: "OrderedDict[str, None]" = OrderedDict()
        self._nickname_cache: dict[str, str] = {}
        self._api_version = str(self.config.get("api_version", "auto")).lower()
        self._resolved_version = ""
        self._effective_flags: dict[str, bool] = {}

    # ------------------------------------------------------------------ #
    # 元数据
    # ------------------------------------------------------------------ #

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name=self.PLATFORM_NAME,
            description="OldChat 平台适配器",
            id=str(self.config.get("id", self.PLATFORM_NAME)),
            support_streaming_message=False,
        )

    # ------------------------------------------------------------------ #
    # 开关裁剪
    # ------------------------------------------------------------------ #

    def _compute_effective_flags(self) -> dict[str, bool]:
        version = self._resolved_version or "v1"
        flags: dict[str, bool] = {}
        for key, default in DEFAULT_CONFIG_TEMPLATE.items():
            if not isinstance(default, bool):
                continue
            want = bool(self.config.get(key, default))
            if key in self._V2_ONLY_FLAGS and version != "v2":
                if want:
                    logger.warning(
                        "[OldChat] 配置项 %s 仅在 v2 生效，当前版本 %s，已忽略",
                        key,
                        version,
                    )
                flags[key] = False
            else:
                flags[key] = want
        return flags

    def _flag(self, name: str) -> bool:
        return self._effective_flags.get(name, False)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        username = self.config.get("identifier", "")
        password = self.config.get("password", "")
        if not username or not password:
            logger.error("[OldChat] 请配置 identifier 和 password")
            return

        base_url = self.config.get("base_url", "").rstrip("/")
        media_base_url = (
            self.config.get("media_base_url", "").rstrip("/") or base_url
        )

        self.client = create_client(
            self._api_version,
            {"base_url": base_url, "media_base_url": media_base_url},
        )

        # 密码回退凭证仅在开启 password_fallback 时注入（内存，不落盘）
        if bool(self.config.get("password_fallback", False)):
            setter = getattr(self.client, "set_fallback_credentials", None)
            if callable(setter):
                setter(username, password)

        retry_interval = max(3, int(self.config.get("login_retry_interval", 10)))

        while self._running:
            try:
                await self.client.login(username, password)
                break
            except NotImplementedError as e:
                logger.error("[OldChat] 协议版本未就绪：%s", e)
                return
            except Exception as e:
                logger.error(
                    "[OldChat] 登录失败：%s，%d 秒后重试",
                    redact(e),
                    retry_interval,
                )
                await asyncio.sleep(retry_interval)
        else:
            return

        # 解析实际版本
        resolved = getattr(self.client, "active_version", "") or ""
        self._resolved_version = resolved or (
            "v1" if self._api_version in ("v1", "auto") else self._api_version
        )
        logger.info("[OldChat] 使用协议版本: %s", self._resolved_version)

        self._effective_flags = self._compute_effective_flags()

        logger.info(
            "[OldChat] 登录成功，UID: %s",
            mask(str((self.client.user or {}).get("uid", ""))),
        )

        if self.config.get("use_websocket", True):
            await self._run_websocket()
        else:
            await self._run_polling()

    async def terminate(self) -> None:
        self._running = False
        if self.ws:
            await self.ws.terminate()
        if self.client:
            await self.client.close()
        logger.info("[OldChat] 适配器已停止")

    # ------------------------------------------------------------------ #
    # WebSocket 模式
    # ------------------------------------------------------------------ #

    async def _run_websocket(self) -> None:
        assert self.client is not None
        self.ws = OldChatWS(
            self.config["base_url"],
            self.client.access_token or "",
            device_id=str(self.config.get("id", "oldchat-adapter")),
            version=self._resolved_version,
            ping_interval=int(self.config.get("ws_ping_interval", 25)),
            reconnect_backoff=bool(
                self.config.get("ws_reconnect_backoff", True)
            ),
            on_unauthorized=self._on_unauthorized,
            on_reconnect=self._sync_after_reconnect,
        )
        self.ws.on_message(self._on_ws_message)
        await self.ws.connect()

    async def _sync_after_reconnect(self, pts: int) -> None:
        if not self._flag("sync_on_reconnect"):
            return
        assert self.client is not None
        try:
            resp = await self.client.get_updates_difference(pts)
            for msg in self._extract_messages(resp):
                await self._process_message(msg)
            logger.info("[OldChat] 重连增量同步完成（pts=%d）", pts)
        except NotImplementedError:
            logger.info(
                "[OldChat] 当前客户端未实现 get_updates_difference，跳过重连同步"
            )
        except Exception as e:
            logger.error("[OldChat] 重连增量同步失败：%s", redact(e))

    async def _on_unauthorized(self) -> None:
        assert self.client is not None
        logger.warning("[OldChat] Token 过期，尝试重新登录...")
        try:
            await self.client.login(
                self.config["identifier"], self.config["password"]
            )
            if self.ws:
                self.ws.access_token = self.client.access_token or ""
            self.client.clear_user_cache()
            logger.info("[OldChat] 重新登录成功")
        except Exception as e:
            logger.error("[OldChat] 重新登录失败：%s", redact(e))

    async def _on_ws_message(self, msg: dict) -> None:
        if not isinstance(msg, dict):
            return
        msg_type = msg.get("type", "")
        if msg_type in ("group_message", "direct_message", "new_message"):
            data = msg.get("data") or {}
            if isinstance(data, dict):
                await self._process_message(data)
        elif msg_type == "recall":
            if self._flag("enable_recall_handling"):
                logger.info("[OldChat] 收到撤回事件（阶段 C 接入 pipeline）")
        elif msg_type in ("typing", "presence"):
            if self._flag("enable_typing_presence"):
                pass  # 阶段 C 实现

    # ------------------------------------------------------------------ #
    # 轮询模式
    # ------------------------------------------------------------------ #

    async def _run_polling(self) -> None:
        interval = max(1, int(self.config.get("poll_interval", 5)))
        logger.info("[OldChat] 开始轮询未读消息，间隔 %d 秒", interval)
        while self._running:
            try:
                await self._poll_once()
            except Exception as e:
                logger.error("[OldChat] 轮询出错：%s", redact(e))
            await asyncio.sleep(interval)

    async def _poll_once(self) -> None:
        assert self.client is not None
        try:
            group_resp = await self.client.get_group_unread(limit=50)
            for msg in self._extract_messages(group_resp):
                await self._process_message(msg)
        except Exception as e:
            logger.error("[OldChat] 获取群未读失败：%s", redact(e))
        try:
            direct_resp = await self.client.get_direct_unread(limit=50)
            for msg in self._extract_messages(direct_resp):
                await self._process_message(msg)
        except Exception as e:
            logger.error("[OldChat] 获取私聊未读失败：%s", redact(e))

    @staticmethod
    def _extract_messages(resp: Any) -> list[dict]:
        if isinstance(resp, dict):
            return resp.get("messages", []) or resp.get("updates", [])
        if isinstance(resp, list):
            return resp
        return []

    # ------------------------------------------------------------------ #
    # 处理与分发
    # ------------------------------------------------------------------ #

    def _mark_processed(self, msg_id: str) -> None:
        self._processed_ids[msg_id] = None
        while len(self._processed_ids) > self._MAX_DEDUP:
            self._processed_ids.popitem(last=False)

    async def _process_message(self, raw_msg: dict) -> None:
        if not isinstance(raw_msg, dict):
            return
        msg_id = str(raw_msg.get("id") or raw_msg.get("message_id") or "")
        if not msg_id or msg_id in self._processed_ids:
            return
        self._mark_processed(msg_id)

        assert self.client is not None and self.client.user is not None
        from_uid = str(
            raw_msg.get("from_uid") or raw_msg.get("sender_id") or ""
        ).upper()
        self_uid = str(self.client.user.get("uid", "")).upper()
        if from_uid and from_uid == self_uid:
            return

        abm = await self.convert_message(raw_msg)
        if abm is None:
            return
        await self._dispatch(abm)

    async def _dispatch(self, abm: AstrBotMessage) -> None:
        assert self.client is not None
        event = OldChatPlatformEvent(
            message_str=abm.message_str,
            message_obj=abm,
            platform_meta=self.meta(),
            session_id=abm.session_id,
            client=self.client,
        )
        self.commit_event(event)

        # 已读回执（实验性，v2 独有；阶段 C 完善）
        if self._flag("enable_read_receipt"):
            try:
                if abm.type == MessageType.GROUP_MESSAGE:
                    await self.client.mark_group_read(abm.group_id, 0)
                else:
                    await self.client.mark_direct_read(abm.sender.user_id, 0)
            except NotImplementedError:
                pass
            except Exception as e:
                logger.warning("[OldChat] 已读回执失败：%s", redact(e))

    # ------------------------------------------------------------------ #
    # 消息转换
    # ------------------------------------------------------------------ #

    async def convert_message(self, raw_msg: dict) -> Optional[AstrBotMessage]:
        assert self.client is not None and self.client.user is not None

        abm = AstrBotMessage()
        abm.self_id = str(self.client.user.get("uid", ""))
        abm.message_id = str(raw_msg.get("id") or raw_msg.get("message_id") or "")
        abm.raw_message = raw_msg

        ts = raw_msg.get("created_at", time.time())
        try:
            ts = int(ts)
            if self._flag("timestamp_auto_scale") and ts > 10 ** 12:
                ts //= 1000
        except (TypeError, ValueError):
            ts = int(time.time())
        abm.timestamp = ts

        from_uid = str(raw_msg.get("from_uid") or raw_msg.get("sender_id") or "")
        sender_name = await self._resolve_sender_name(from_uid, raw_msg)
        abm.sender = MessageMember(user_id=from_uid, nickname=sender_name)

        group_id = raw_msg.get("group_id", "")
        if group_id:
            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = str(group_id)
            abm.session_id = str(group_id)
        else:
            abm.type = MessageType.FRIEND_MESSAGE
            abm.session_id = from_uid

        msg_type = raw_msg.get("msg_type", "text")
        body = raw_msg.get("body", "")
        message_str, mentions, quote = self._parse_body(body)

        message_chain: list = []

        if quote and quote.get("id"):
            message_chain.append(
                Reply(
                    id=str(quote.get("id", "")),
                    sender_id=str(quote.get("from_uid", "")),
                    sender_nickname=str(quote.get("from_nickname", "")),
                    message_str=str(quote.get("body", "")),
                    text=str(quote.get("body", "")),
                )
            )

        for m in mentions:
            uid = str(m.get("uid", ""))
            name = str(m.get("name", uid))
            message_chain.append(At(qq=uid, name=name))

        if msg_type == "image" and raw_msg.get("media_url"):
            media_url = self.client._resolve_media_url(raw_msg["media_url"])
            message_chain.append(Image(file=media_url, url=media_url))
            if message_str:
                message_chain.append(Plain(text=message_str))
                abm.message_str = message_str
            else:
                abm.message_str = "[图片]"
        else:
            if message_str:
                message_chain.append(Plain(text=message_str))
            abm.message_str = message_str

        if not message_chain:
            return None
        abm.message = message_chain

        logger.info(
            "[OldChat] 收到 %s 消息 from %s(%s): %s",
            abm.type.value,
            sender_name,
            mask(from_uid),
            (abm.message_str or "")[:50],
        )
        return abm

    @staticmethod
    def _parse_body(body: Any) -> tuple[str, list, dict]:
        zero_width = ("\u200b", "\u200c", "\u200d", "\ufeff")

        def _clean(text: Any) -> str:
            s = str(text)
            for ch in zero_width:
                s = s.replace(ch, "")
            return s

        if isinstance(body, dict):
            if body.get("v") == 2:
                return (
                    _clean(body.get("text", "")),
                    body.get("mentions", []) or [],
                    body.get("quote") or {},
                )
            return _clean(body.get("text", "")), [], {}

        if isinstance(body, str) and body.startswith("{"):
            try:
                obj = json.loads(body)
                if isinstance(obj, dict):
                    if obj.get("v") == 2:
                        return (
                            _clean(obj.get("text", "")),
                            obj.get("mentions", []) or [],
                            obj.get("quote") or {},
                        )
                    return _clean(obj.get("text", body)), [], {}
            except json.JSONDecodeError:
                pass
        return (str(body) if body else ""), [], {}

    async def _resolve_sender_name(self, uid: str, data: dict) -> str:
        for field in (
            "from_nickname",
            "from_display_name",
            "from_username",
            "nickname",
            "display_name",
        ):
            val = data.get(field, "")
            if val and isinstance(val, str) and val.strip():
                self._nickname_cache[uid] = val.strip()
                return val.strip()
        if uid in self._nickname_cache:
            return self._nickname_cache[uid]
        assert self.client is not None
        profile = await self.client.get_user_profile(uid)
        if profile:
            name = (
                profile.get("display_name", "")
                or profile.get("username", "")
                or uid
            )
            self._nickname_cache[uid] = name
            return name
        return uid

    # ------------------------------------------------------------------ #
    # 跨会话发送
    # ------------------------------------------------------------------ #

    async def send_by_session(
        self, session: MessageSesion, message_chain: MessageChain
    ) -> None:
        assert self.client is not None
        is_group = session.message_type == MessageType.GROUP_MESSAGE
        target = session.session_id

        text_parts: list[str] = []
        image_paths: list[str] = []

        for seg in message_chain.chain:
            if isinstance(seg, Plain):
                if seg.text and seg.text.strip():
                    text_parts.append(seg.text)
            elif isinstance(seg, At):
                text_parts.append(f"@{seg.name or seg.qq} ")
            elif isinstance(seg, Image):
                try:
                    image_paths.append(await seg.convert_to_file_path())
                except Exception as e:
                    logger.error("[OldChat] 处理图片失败：%s", redact(e))

        text = "".join(text_parts).strip()
        if text:
            try:
                if is_group:
                    await self.client.send_group_message(target, text)
                else:
                    await self.client.send_direct_message(target, text)
            except Exception as e:
                logger.error(
                    "[OldChat] send_by_session 发送文本失败：%s", redact(e)
                )

        for img_path in image_paths:
            try:
                with open(img_path, "rb") as f:
                    img_bytes = f.read()
                media_url, thumb_url = await self.client.upload_media_bytes(
                    img_bytes, os.path.basename(img_path) or "image.jpg"
                )
                if not media_url:
                    continue
                if is_group:
                    await self.client.send_group_message(
                        target, "", "image",
                        media_url=media_url, thumb_url=thumb_url,
                    )
                else:
                    await self.client.send_direct_message(
                        target, "", "image",
                        media_url=media_url, thumb_url=thumb_url,
                    )
            except Exception as e:
                logger.error(
                    "[OldChat] send_by_session 发送图片失败：%s", redact(e)
                )

        await super().send_by_session(session, message_chain)