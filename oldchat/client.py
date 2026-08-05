"""oldchat/client.py - OldChat HTTP API 客户端（异步版）

基于 API_STANDARD.md 接口规范，封装登录、消息收发、媒体上传等 HTTP 接口。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from astrbot import logger


class OldChatClient:
    """OldChat HTTP API 客户端。"""

    def __init__(self, config: dict) -> None:
        self.base_url: str = config["base_url"].rstrip("/")
        self.media_base_url: str = (
            config.get("media_base_url", "").rstrip("/") or self.base_url
        )
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.user: Optional[dict] = None
        self.session = httpx.AsyncClient(
            headers={"User-Agent": "oldchat-adapter/1.0"},
            timeout=30,
        )
        self._user_profile_cache: Dict[str, dict] = {}

    # ------------------------------------------------------------------ #
    # 工具方法
    # ------------------------------------------------------------------ #

    def _resolve_media_url(self, url: str) -> str:
        """将相对媒体路径补全为完整 URL。"""
        if not url:
            return url
        if url.startswith("/"):
            return self.media_base_url + url
        return url

    async def close(self) -> None:
        await self.session.aclose()

    # ------------------------------------------------------------------ #
    # 底层请求
    # ------------------------------------------------------------------ #

    async def _request(
        self, method: str, path: str, auth: bool = True, **kwargs
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers: dict = {}
        if auth and self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        response = await self.session.request(method, url, headers=headers, **kwargs)

        # 401 时尝试用 refresh_token 刷新
        if response.status_code == 401 and auth and self.refresh_token:
            if await self._refresh():
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
            error_code = data.get("code", "unknown") if isinstance(data, dict) else "unknown"
            error_msg = data.get("error", str(data)) if isinstance(data, dict) else str(data)
            raise Exception(f"HTTP {response.status_code} [{error_code}]: {error_msg}")

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
            if "refresh_token" in resp:
                self.refresh_token = resp["refresh_token"]
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # 认证
    # ------------------------------------------------------------------ #

    async def login(
        self, identifier: str, password: str, device_name: str = "oldchat-adapter"
    ) -> dict:
        resp = await self._request(
            "POST",
            "/v1/auth/login",
            auth=False,
            json={
                "identifier": identifier,
                "password": password,
                "device_id": "oldchat-adapter",
                "device_name": device_name,
                "platform": "cli",
                "app_version": "1.0",
            },
        )
        self.access_token = resp["access_token"]
        self.refresh_token = resp["refresh_token"]
        self.user = resp["user"]
        logger.info(
            "OldChat 登录成功: %s",
            self.user.get("display_name", self.user.get("username")),
        )
        return self.user

    # ------------------------------------------------------------------ #
    # 用户资料
    # ------------------------------------------------------------------ #

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
            logger.warning("获取用户资料失败 %s: %s", uid, e)
            return None

    def clear_user_cache(self, uid: Optional[str] = None) -> None:
        if uid:
            self._user_profile_cache.pop(uid, None)
        else:
            self._user_profile_cache.clear()

    # ------------------------------------------------------------------ #
    # 群组
    # ------------------------------------------------------------------ #

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
        """获取群聊未读消息（轮询模式）。"""
        return await self._request(
            "POST", "/v1/groups/unread", json={"limit": limit}
        )

    # ------------------------------------------------------------------ #
    # 私聊
    # ------------------------------------------------------------------ #

    async def send_direct_message(
        self,
        to_uid: str,
        body: str,
        msg_type: str = "text",
        media_url: Optional[str] = None,
        thumb_url: Optional[str] = None,
        **kwargs,
    ) -> dict:
        """发送私聊消息。

        端点遵循与群组 ``/v1/groups/message/send`` 相同的命名风格。
        """
        if not isinstance(body, str):
            body = str(body) if body else ""
        payload: dict = {
            "to_uid": to_uid,
            "body": body,
            "msg_type": msg_type,
        }
        if media_url:
            payload["media_url"] = media_url
        if thumb_url:
            payload["thumb_url"] = thumb_url
        payload.update(kwargs)
        return await self._request("POST", "/v1/direct/message/send", json=payload)

    async def get_direct_unread(self, limit: int = 50) -> dict:
        """获取私聊未读消息（轮询模式）。"""
        return await self._request(
            "POST", "/v1/direct/unread", json={"limit": limit}
        )

    # ------------------------------------------------------------------ #
    # 媒体上传 / 下载
    # ------------------------------------------------------------------ #

    async def upload_media(self, file_path: str) -> Tuple[Optional[str], Optional[str]]:
        if not os.path.exists(file_path):
            logger.warning("文件不存在: %s", file_path)
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
                logger.warning("上传失败: %s", e)

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
            logger.warning("下载媒体失败: %s", e)
            return False
