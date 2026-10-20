
"""oldchat/client_v2.py - OldChat HTTP API 客户端（v2 端点，阶段 A 骨架）。

阶段 A：仅类骨架，所有网络方法抛 NotImplementedError。
阶段 B：完整实现 v2 端点、username 登录字段、增量同步、已读回执等。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from .client_base import OldChatClientBase


_NYI = "v2 客户端尚未实现（阶段 B 交付）"


class OldChatClientV2(OldChatClientBase):
    """OldChat HTTP API 客户端（v2，阶段 A 仅骨架）。"""

    def __init__(self, config: dict) -> None:
        self.base_url: str = config["base_url"].rstrip("/")
        self.media_base_url: str = (
            config.get("media_base_url", "").rstrip("/") or self.base_url
        )
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.user: Optional[dict] = None

    async def close(self) -> None:
        return None

    async def login(self, username: str, password: str) -> dict:
        raise NotImplementedError(_NYI)

    async def get_user_profile(
        self, uid: str, use_cache: bool = True
    ) -> Optional[dict]:
        raise NotImplementedError(_NYI)

    def clear_user_cache(self, uid: Optional[str] = None) -> None:
        return None

    async def get_group_members(self, group_id: str) -> List[dict]:
        raise NotImplementedError(_NYI)

    async def get_group_messages(
        self,
        group_id: str,
        limit: int = 50,
        offset: int = 0,
        before_created_at: Optional[int] = None,
        before_id: Optional[str] = None,
    ) -> dict:
        raise NotImplementedError(_NYI)

    async def send_group_message(
        self, group_id: str, body: str, msg_type: str = "text", **kwargs
    ) -> dict:
        raise NotImplementedError(_NYI)

    async def get_group_unread(self, limit: int = 50) -> dict:
        raise NotImplementedError(_NYI)

    async def send_direct_message(
        self, to_uid: str, body: str, msg_type: str = "text", **kwargs
    ) -> dict:
        raise NotImplementedError(_NYI)

    async def get_direct_unread(self, limit: int = 50) -> dict:
        raise NotImplementedError(_NYI)

    async def upload_media_bytes(
        self, file_data: bytes, filename: str, ctype: str = "image/jpeg"
    ) -> Tuple[Optional[str], Optional[str]]:
        raise NotImplementedError(_NYI)

    async def download_media(self, url: str, save_path: str) -> bool:
        raise NotImplementedError(_NYI)

    async def get_updates_difference(self, pts: int, limit: int = 100) -> dict:
        raise NotImplementedError(_NYI)

    async def mark_group_read(self, group_id: str, seq: int) -> dict:
        raise NotImplementedError(_NYI)

    async def mark_direct_read(self, peer_uid: str, seq: int) -> dict:
        raise NotImplementedError(_NYI)