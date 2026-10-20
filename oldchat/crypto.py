"""oldchat/crypto.py - ECDH 握手与 AES-CBC/HMAC 加解密（v1/v2 共用）。

* ECDH (SECP256R1) 协商共享密钥
* SHA256(raw + b"enc") → AES-256 密钥
* SHA256(raw + b"mac") → HMAC 密钥
* 帧格式 ``{iv, data, mac}``（base64）
* AES-CBC + PKCS7 加解密
"""
from __future__ import annotations

import base64
import hashlib
import hmac as hmac_mod
import json
import os
from typing import Optional

import aiohttp
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from astrbot import logger

from ._redact import redact, safe_url


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if pad < 1 or pad > 16:
        return data
    return data[:-pad]


def _pkcs7_pad(data: bytes) -> bytes:
    pad = 16 - (len(data) % 16)
    return data + bytes([pad] * pad)


class ECDHSession:
    """ECDH 会话：负责握手与帧加解密。"""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session_id: Optional[str] = None
        self._aes_key: Optional[bytes] = None
        self._hmac_key: Optional[bytes] = None

    async def handshake(self, path: str) -> bool:
        """执行 ECDH 握手；成功返回 True。"""
        try:
            ec_private = ec.generate_private_key(ec.SECP256R1(), default_backend())
            ec_public = ec_private.public_key()

            client_pub_bytes = ec_public.public_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            client_pub_b64 = base64.b64encode(client_pub_bytes).decode("ascii")

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}{path}",
                    json={"client_pub": client_pub_b64},
                ) as resp:
                    if not resp.ok:
                        logger.warning(
                            "[OldChat] 握手 HTTP %d: %s",
                            resp.status,
                            safe_url(f"{self.base_url}{path}"),
                        )
                        return False
                    data = await resp.json()

            server_pub_bytes = base64.b64decode(data["server_pub"])
            server_pub = serialization.load_der_public_key(
                server_pub_bytes, default_backend()
            )

            shared_secret = ec_private.exchange(ec.ECDH(), server_pub)
            # P-256 ECDH 固定返回 32 字节；rjust 防御非标准实现
            raw = shared_secret[-32:].rjust(32, b"\x00")

            self._aes_key = hashlib.sha256(raw + b"enc").digest()
            self._hmac_key = hashlib.sha256(raw + b"mac").digest()
            self.session_id = data.get("session_id")
            logger.info(
                "[OldChat] ECDH 握手完成, session=%s",
                (self.session_id or "")[:8],
            )
            return True
        except Exception as e:
            logger.error("[OldChat] ECDH 握手失败: %s", redact(e))
            return False

    async def decrypt(self, payload: str) -> Optional[str]:
        """解密一帧；失败返回 None。"""
        if self._aes_key is None or self._hmac_key is None:
            return None
        try:
            env = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return None
        if not all(k in env for k in ("iv", "data", "mac")):
            return None
        try:
            iv = base64.b64decode(env["iv"])
            ciphertext = base64.b64decode(env["data"])
            mac = base64.b64decode(env["mac"])
        except Exception:
            return None

        expected = hmac_mod.new(
            self._hmac_key, iv + ciphertext, hashlib.sha256
        ).digest()
        if not hmac_mod.compare_digest(mac, expected):
            logger.warning("[OldChat] HMAC 验证失败")
            return None

        try:
            cipher = Cipher(
                algorithms.AES(self._aes_key),
                modes.CBC(iv),
                backend=default_backend(),
            )
            decryptor = cipher.decryptor()
            padded = decryptor.update(ciphertext) + decryptor.finalize()
            return _pkcs7_unpad(padded).decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning("[OldChat] 解密失败: %s", redact(e))
            return None

    async def encrypt(self, plaintext: str) -> Optional[str]:
        """加密文本为 WS 帧（base64 JSON 信封）。"""
        if self._aes_key is None or self._hmac_key is None:
            return None
        try:
            iv = os.urandom(16)
            padded = _pkcs7_pad(plaintext.encode("utf-8"))
            cipher = Cipher(
                algorithms.AES(self._aes_key),
                modes.CBC(iv),
                backend=default_backend(),
            )
            encryptor = cipher.encryptor()
            ciphertext = encryptor.update(padded) + encryptor.finalize()
            mac = hmac_mod.new(
                self._hmac_key, iv + ciphertext, hashlib.sha256
            ).digest()
            return json.dumps({
                "iv": base64.b64encode(iv).decode("ascii"),
                "data": base64.b64encode(ciphertext).decode("ascii"),
                "mac": base64.b64encode(mac).decode("ascii"),
            })
        except Exception as e:
            logger.warning("[OldChat] 加密失败: %s", redact(e))
            return None
