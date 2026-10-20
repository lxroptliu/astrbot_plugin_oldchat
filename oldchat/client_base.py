"""oldchat/client_base.py - OldChat HTTP 客户端抽象接口。

v1 / v2 两套实现均须遵守本接口；adapter / event 只依赖本接口，
不感知具体协议版本。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional, Tuple


class OldChatClientBase(ABC):
    """OldChat HTTP API 客户端抽象基类。"""

    # ---- 实例属性（子类 __init__ 必须赋值）----
    base_url: str = ""
    media_base_url: str = ""
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    user: Optional[dict] = None

    # ---- 工具方法（默认实现）----

    def _resolve_media_url(self, url: str) -> str:
        """相对媒体路径 → 完整 URL。"""
        if not url:
            return url
        if url.startswith("/"):
            return self.media_base_url + url
        return url

    # ---- 生命周期 ----

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def login(self, username: str, password: str) -> dict: ...

    # ---- 用户 ----

    @abstractmethod
    async def get_user_profile(
        self, uid: str, use_cache: bool = True
    ) -> Optional[dict]: ...

    @abstractmethod
    def clear_user_cache(self, uid: Optional[str] = None) -> None: ...

    # ---- 群组 ----

    @abstractmethod
    async def get_group_members(self, group_id: str) -> List[dict]: ...

    @abstractmethod
    async def get_group_messages(
        self,
        group_id: str,
        limit: int = 50,
        offset: int = 0,
        before_created_at: Optional[int] = None,
        before_id: Optional[str] = None,
    ) -> dict: ...

    @abstractmethod
    async def send_group_message(
        self, group_id: str, body: str, msg_type: str = "text", **kwargs
    ) -> dict: ...

    @abstractmethod
    async def get_group_unread(self, limit: int = 50) -> dict: ...

    # ---- 私聊 ----

    @abstractmethod
    async def send_direct_message(
        self, to_uid: str, body: str, msg_type: str = "text", **kwargs
    ) -> dict: ...

    @abstractmethod
    async def get_direct_unread(self, limit: int = 50) -> dict: ...

    # ---- 媒体 ----

    @abstractmethod
    async def upload_media_bytes(
        self, file_data: bytes, filename: str, ctype: str = "image/jpeg"
    ) -> Tuple[Optional[str], Optional[str]]: ...

    @abstractmethod
    async def download_media(self, url: str, save_path: str) -> bool: ...

    # ---- v2 独有（v1 默认抛 NotImplementedError）----

    async def get_updates_difference(self, pts: int, limit: int = 100) -> dict:
        raise NotImplementedError("v1 不支持 get_updates_difference")

    async def mark_group_read(self, group_id: str, seq: int) -> dict:
        raise NotImplementedError("v1 不支持 mark_group_read")

    async def mark_direct_read(self, peer_uid: str, seq: int) -> dict:
        raise NotImplementedError("v1 不支持 mark_direct_read")
