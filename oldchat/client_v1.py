
"""oldchat/client_v1.py - OldChat HTTP API 客户端（v1 端点）。

基于旧版 API：``/v1/auth/login``、``/v1/groups/message/send``、
``/v1/direct/message/send`` 等。所有异常与日志经 ``_redact`` 脱敏。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx

from astrbot import logger

from ._redact import mask, redact, safe_url
from .client_base import OldChatClientBase


class OldChatClientV1(OldChatClientBase):
    """OldChat HTTP API 客户端（v1）。"""

    def __init__(self, config: dict) -> None:
        self.base_url: str = config["base_url"].rstrip("/")
        self.media_base_url: str = (
            config.get("media_base_url", "").rstrip("/") or self.base_url
        )
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.user: Optional[dict] = None
        self.session = httpx.AsyncClient(
            headers={"User-Agent": "oldchat-adapter/2.0-v1"},
            timeout=30,
        )
        self._user_profile_cache: Dict[str, dict] = {}
        # 401 可选密码回退凭证（内存，由外部注入；默认空）
        self._fallback_credentials: Optional[Tuple[str, str]] = None

    def set_fallback_credentials(self, username: str, password: str) -> None:
        """注入内存凭证，供 401 时密码回退（password_fallback=true 时使用）。"""
        self._fallback_credentials = (username, password)

    def clear_fallback_credentials(self) -> None:
        self._fallback_credentials = None

    # ---------------- 底层请求 ---------------- #

    async def _request(
        self, method: str, path: str, auth: bool = True, **kwargs
    ) -> Dict:
        url = f"{self.base_url}{path}"
        headers: dict = {}
        if auth and self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        response = await self.session.request(method, url, headers=headers, **kwargs)

        if response.status_code == 401 and auth:
            refreshed = False
            if self.refresh_token:
                refreshed = await self._refresh()
            if not refreshed and self._fallback_credentials:
                refreshed = await self._relogin()
            if refreshed:
                headers["Authorization"] = f"Bearer {self.access_token}"
                response = await self.session.request(
                    method, url, headers=headers, **kwargs
                )
            else:
                raise Exception("登录已过期，请重新登录")

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text}

        if not response.is_success:
            if isinstance(data, dict):
                error_code = data.get("code", "unknown")
                error_msg = data.get("message") or data.get("error") or str(data)
            else:
                error_code, error_msg = "unknown", str(data)
            raise Exception(
                f"HTTP {response.status_code} [{error_code}]: {redact(error_msg)}"
            )

        return data

    async def _refresh(self) -> bool:
        try:
            resp = await self._request(
                "POST",
                "/v1/auth/refresh",
                auth=False,
                json={"refresh_token": self.refresh_token},
            )
            self.access_token = resp["access_token"]
            if resp.get("refresh_token"):
                self.refresh_token = resp["refresh_token"]
            return True
        except Exception as e:
            logger.warning("[OldChat/v1] Token 刷新失败: %s", redact(e))
            return False

    async def _relogin(self) -> bool:
        if not self._fallback_credentials:
            return False
        username, password = self._fallback_credentials
        try:
            await self.login(username, password)
            return True
        except Exception as e:
            logger.warning("[OldChat/v1] 密码回退登录失败: %s", redact(e))
            return False

    # ---------------- 生命周期 ---------------- #

    async def close(self) -> None:
        await self.session.aclose()

    # ---------------- 认证 ---------------- #

    async def login(
        self, username: str, password: str, device_name: str = "oldchat-adapter"
    ) -> dict:
        resp = await self._request(
            "POST",
            "/v1/auth/login",
            auth=False,
            json={
                "identifier": username,  # v1 字段名为 identifier
                "password": password,
                "device_id": "oldchat-adapter",
                "device_name": device_name,
                "platform": "cli",
                "app_version": "2.0",
            },
        )
        self.access_token = resp["access_token"]
        self.refresh_token = resp.get("refresh_token")
        self.user = resp.get("user") or {}
        logger.info(
            "[OldChat/v1] 登录成功: %s（uid=%s）",
            mask(self.user.get("display_name") or self.user.get("username")),
            mask(str(self.user.get("uid", ""))),
        )
        return self.user

    # ---------------- 用户 ---------------- #

    async def get_user_profile(
        self, uid: str, use_cache: bool = True
    ) -> Optional[dict]:
        if use_cache and uid in self._user_profile_cache:
            return self._user_profile_cache[uid]
        try:
            data = await self._request(
                "GET", "/v1/users/profile", params={"uid": uid}
            )
            profile = data if isinstance(data, dict) else None
            if profile:
                self._user_profile_cache[uid] = profile
            return profile
        except Exception as e:
            logger.warning(
                "[OldChat/v1] 获取用户资料失败 %s: %s", mask(uid), redact(e)
            )
            return None

    def clear_user_cache(self, uid: Optional[str] = None) -> None:
        if uid:
            self._user_profile_cache.pop(uid, None)
        else:
            self._user_profile_cache.clear()

    # ---------------- 群组 ---------------- #

    async def get_group_members(self, group_id: str) -> List[dict]:
        data = await self._request(
            "GET", "/v1/groups/members", params={"group_id": group_id}
        )
        return data.get("members", [])

    async def get_group_messages(
        self,
        group_id: str,
        limit: int = 50,
        offset: int = 0,
        before_created_at: Optional[int] = None,
        before_id: Optional[str] = None,
    ) -> dict:
        params: dict = {"group_id": group_id, "limit": limit}
        if before_created_at is not None and before_id:
            params["before_created_at"] = before_created_at
            params["before_id"] = before_id
        elif offset:
            params["offset"] = offset
        return await self._request("GET", "/v1/groups/messages/v2", params=params)

    async def send_group_message(
        self,
        group_id: str,
        body: str,
        msg_type: str = "text",
        burn_after_seconds: int = 0,
        media_url: Optional[str] = None,
        thumb_url: Optional[str] = None,
        **kwargs,
    ) -> dict:
        if not isinstance(body, str):
            body = str(body) if body else ""
        payload: dict = {
            "group_id": group_id,
            "body": body,
            "msg_type": msg_type,
        }
        if burn_after_seconds:
            payload["burn_after_seconds"] = burn_after_seconds
        if media_url:
            payload["media_url"] = media_url
        if thumb_url:
            payload["thumb_url"] = thumb_url
        payload.update(kwargs)
        return await self._request("POST", "/v1/groups/message/send", json=payload)

    async def get_group_unread(self, limit: int = 50) -> dict:
        return await self._request(
            "POST", "/v1/groups/unread", json={"limit": limit}
        )

    # ---------------- 私聊 ---------------- #

    async def send_direct_message(
        self,
        to_uid: str,
        body: str,
        msg_type: str = "text",
        media_url: Optional[str] = None,
        thumb_url: Optional[str] = None,
        **kwargs,
    ) -> dict:
        if not isinstance(body, str):
            body = str(body) if body else ""
        payload: dict = {"to_uid": to_uid, "body": body, "msg_type": msg_type}
        if media_url:
            payload["media_url"] = media_url
        if thumb_url:
            payload["thumb_url"] = thumb_url
        payload.update(kwargs)
        return await self._request("POST", "/v1/direct/message/send", json=payload)

    async def get_direct_unread(self, limit: int = 50) -> dict:
        return await self._request(
            "POST", "/v1/direct/unread", json={"limit": limit}
        )

    # ---------------- 媒体 ---------------- #

    async def upload_media(
        self, file_path: str
    ) -> Tuple[Optional[str], Optional[str]]:
        if not os.path.exists(file_path):
            logger.warning("[OldChat/v1] 文件不存在: %s", file_path)
            return None, None
        filename = os.path.basename(file_path)
        ext = Path(file_path).suffix.lower()
        mime_map = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".mp4": "video/mp4",
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
        }
        ctype = mime_map.get(ext, "application/octet-stream")
        with open(file_path, "rb") as f:
            file_data = f.read()
        return await self.upload_media_bytes(file_data, filename, ctype)

    async def upload_media_bytes(
        self, file_data: bytes, filename: str, ctype: str = "image/jpeg"
    ) -> Tuple[Optional[str], Optional[str]]:
        attempts = [
            ("file", (filename, file_data)),
            ("media", (filename, file_data)),
            ("file", (filename, file_data, ctype)),
        ]
        for field_name, files_tuple in attempts:
            try:
                headers = {"Authorization": f"Bearer {self.access_token}"}
                resp = await self.session.post(
                    f"{self.base_url}/v1/media",
                    headers=headers,
                    files={field_name: files_tuple},
                )
                if resp.status_code in (200, 201):
                    r = resp.json()
                    media_url = self._resolve_media_url(r.get("url", ""))
                    thumb_url = self._resolve_media_url(r.get("thumb_url", ""))
                    return media_url, thumb_url
            except Exception as e:
                logger.warning("[OldChat/v1] 上传尝试失败: %s", redact(e))
        return None, None

    async def download_media(self, url: str, save_path: str) -> bool:
        try:
            async with self.session.stream("GET", url, timeout=30) as resp:
                resp.raise_for_status()
                with open(save_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=8192):
                        f.write(chunk)
            return True
        except Exception as e:
            logger.warning(
                "[OldChat/v1] 下载媒体失败 %s: %s", safe_url(url), redact(e)
            )
            return False