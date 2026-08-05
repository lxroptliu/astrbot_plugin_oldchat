"""oldchat/ws.py - OldChat 加密 WebSocket 客户端

通过 ECDH 握手协商 AES-CBC + HMAC-SHA256 会话密钥，
实时接收 OldChat 服务端推送的加密消息帧。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac as hmac_mod
import json
from typing import Awaitable, Callable, Optional

import aiohttp
import websockets
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from astrbot import logger


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if pad < 1 or pad > 16:
        return data
    return data[:-pad]


class OldChatWS:
    """OldChat WebSocket 客户端，支持加密通信与自动重连。"""

    def __init__(
        self,
        base_url: str,
        access_token: str,
        on_unauthorized: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self.base_url: str = base_url.rstrip("/")
        self.access_token: str = access_token
        self._message_handler: Optional[Callable] = None
        self._on_unauthorized = on_unauthorized
        self._ws = None
        self._session_id: Optional[str] = None
        self._aes_key: Optional[bytes] = None
        self._hmac_key: Optional[bytes] = None
        self._terminating = False

    def on_message(self, handler: Callable[..., Awaitable]) -> None:
        self._message_handler = handler

    async def terminate(self) -> None:
        """标记终止并关闭当前连接。"""
        self._terminating = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # ECDH 握手
    # ------------------------------------------------------------------ #

    async def _handshake(self) -> None:
        ec_private = ec.generate_private_key(ec.SECP256R1(), default_backend())
        ec_public = ec_private.public_key()

        client_pub_bytes = ec_public.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        client_pub_b64 = base64.b64encode(client_pub_bytes).decode("ascii")

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/v1/auth/handshake",
                json={"client_pub": client_pub_b64},
            ) as resp:
                data = await resp.json()

        server_pub_bytes = base64.b64decode(data["server_pub"])
        server_pub = serialization.load_der_public_key(
            server_pub_bytes, default_backend()
        )

        shared_secret = ec_private.exchange(ec.ECDH(), server_pub)
        raw = (
            shared_secret[-32:]
            if len(shared_secret) > 32
            else shared_secret.zfill(32)
        )

        self._aes_key = hashlib.sha256(raw + b"enc").digest()
        self._hmac_key = hashlib.sha256(raw + b"mac").digest()
        self._session_id = data["session_id"]

        logger.info("ECDH 握手完成, session_id=%s", self._session_id[:8])

    # ------------------------------------------------------------------ #
    # 解密
    # ------------------------------------------------------------------ #

    async def _decrypt(self, payload: str) -> Optional[str]:
        try:
            env = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return None

        if not all(k in env for k in ("iv", "data", "mac")):
            return None

        iv = base64.b64decode(env["iv"])
        ciphertext = base64.b64decode(env["data"])
        mac = base64.b64decode(env["mac"])

        expected = hmac_mod.new(
            self._hmac_key, iv + ciphertext, hashlib.sha256
        ).digest()
        if not hmac_mod.compare_digest(mac, expected):
            logger.warning("HMAC 验证失败")
            return None

        cipher = Cipher(
            algorithms.AES(self._aes_key),
            modes.CBC(iv),
            backend=default_backend(),
        )
        decryptor = cipher.decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        plain = _pkcs7_unpad(padded)
        return plain.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------ #
    # 连接主循环
    # ------------------------------------------------------------------ #

    def _is_unauthorized(self, exc: Exception) -> bool:
        if isinstance(exc, websockets.exceptions.InvalidStatus):
            return exc.response.status_code == 401
        return "HTTP 401" in str(exc)

    async def _connect(self) -> None:
        """持续连接 OldChat WebSocket，直到 ``terminate()`` 被调用。"""
        while not self._terminating:
            try:
                await self._handshake()
                ws_url = (
                    f"ws://{self.base_url.replace('http://', '')}"
                    f"/v1/ws?sid={self._session_id}"
                )

                logger.info("连接 OldChat WebSocket: %s", ws_url.split("?")[0])
                async with websockets.connect(
                    ws_url,
                    additional_headers={
                        "Authorization": f"Bearer {self.access_token}",
                    },
                ) as ws:
                    self._ws = ws
                    logger.info("已连接到 OldChat WebSocket")
                    async for raw_msg in ws:
                        try:
                            plain = await self._decrypt(raw_msg)
                            if not plain:
                                continue
                            msg = json.loads(plain)
                            if self._message_handler:
                                await self._message_handler(msg)
                        except json.JSONDecodeError:
                            pass
                        except Exception as e:
                            logger.error("处理 WS 消息异常: %s", e)
            except asyncio.CancelledError:
                logger.info("WebSocket 任务已取消")
                break
            except websockets.exceptions.ConnectionClosed:
                if self._terminating:
                    break
                logger.warning("WebSocket 连接已关闭，3 秒后重连...")
                await asyncio.sleep(3)
            except Exception as e:
                if self._terminating:
                    break
                if self._is_unauthorized(e) and self._on_unauthorized:
                    logger.warning("WebSocket 认证失败 (401)，10 秒后重新登录...")
                    await asyncio.sleep(10)
                    try:
                        await self._on_unauthorized()
                    except Exception as login_err:
                        logger.error("重新登录失败: %s，10 秒后重试...", login_err)
                        await asyncio.sleep(10)
                else:
                    logger.error("WebSocket 连接失败: %s，3 秒后重连...", e)
                    await asyncio.sleep(3)

        logger.info("OldChat WebSocket 已停止")
