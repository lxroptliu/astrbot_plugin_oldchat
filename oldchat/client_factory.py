
"""oldchat/client_factory.py - 按 api_version 构造 HTTP 客户端。"""
from __future__ import annotations

from typing import Optional, Tuple

from astrbot import logger

from .client_base import OldChatClientBase
from .client_v1 import OldChatClientV1
from .client_v2 import OldChatClientV2


def create_client(api_version: str, config: dict) -> OldChatClientBase:
    """根据 api_version 创建客户端。

    * ``v1``   → OldChatClientV1
    * ``v2``   → OldChatClientV2（阶段 A 尚未实现登录）
    * ``auto`` → AutoClient 包装器（v2 端点不存在时回退 v1）
    """
    version = (api_version or "auto").lower()
    if version == "v1":
        return OldChatClientV1(config)
    if version == "v2":
        return OldChatClientV2(config)
    if version == "auto":
        return AutoClient(config)
    logger.warning("[OldChat] 未知 api_version=%r，回退 v1", api_version)
    return OldChatClientV1(config)


class AutoClient(OldChatClientBase):
    """auto 模式包装器。

    * 先尝试 v2 登录；
    * v2 抛 ``NotImplementedError``（阶段 A 骨架）→ 回退 v1；
    * v2 因端点不存在（404/405/501）失败 → 回退 v1；
    * v2 因 401/403 认证失败 → **不**回退（避免掩盖密码错误）。
    """

    def __init__(self, config: dict) -> None:
        self._config = config
        self._fallback: Optional[Tuple[str, str]] = None
        self._active: OldChatClientBase = OldChatClientV2(config)
        self._resolved: str = ""

    @property
    def active_version(self) -> str:
        return self._resolved

    def __getattr__(self, name):
        # 实例属性优先；未命中转发到 _active
        return getattr(self._active, name)

    # 显式声明媒体 URL 解析（避免 __getattr__ 在 _active 尚未初始化时递归）
    def _resolve_media_url(self, url: str) -> str:
        return self._active._resolve_media_url(url)

    def set_fallback_credentials(self, username: str, password: str) -> None:
        self._fallback = (username, password)
        if hasattr(self._active, "set_fallback_credentials"):
            self._active.set_fallback_credentials(username, password)

    def clear_fallback_credentials(self) -> None:
        self._fallback = None
        if hasattr(self._active, "clear_fallback_credentials"):
            self._active.clear_fallback_credentials()

    async def _switch_to(self, client: OldChatClientBase, reason: str) -> None:
        try:
            await self._active.close()
        except Exception:
            pass
        self._active = client
        logger.info("[OldChat] auto 模式：%s", reason)

    async def login(self, username: str, password: str) -> dict:
        # 尝试 v2
        try:
            user = await self._active.login(username, password)
            self._resolved = "v2"
            return user
        except NotImplementedError as e:
            logger.info("[OldChat] auto 模式：v2 未就绪（%s），回退 v1", e)
        except Exception as e:
            msg = str(e)
            if not any(code in msg for code in ("HTTP 404", "HTTP 405", "HTTP 501")):
                # 非"端点不存在"错误：向上抛，不降级
                raise
            logger.info("[OldChat] auto 模式：v2 端点不存在（%s），回退 v1", msg)

        # 构造 v1
        v1 = OldChatClientV1(self._config)
        if self._fallback:
            v1.set_fallback_credentials(*self._fallback)
        await self._switch_to(v1, "使用 v1")
        user = await self._active.login(username, password)
        self._resolved = "v1"
        return user

    # ---- 显式转发（不走 __getattr__ 路径，保证日志/异常栈清晰）----

    async def close(self) -> None:
        await self._active.close()

    async def get_user_profile(self, uid: str, use_cache: bool = True):
        return await self._active.get_user_profile(uid, use_cache)

    def clear_user_cache(self, uid: Optional[str] = None) -> None:
        self._active.clear_user_cache(uid)

    async def get_group_members(self, group_id: str):
        return await self._active.get_group_members(group_id)

    async def get_group_messages(
        self,
        group_id: str,
        limit: int = 50,
        offset: int = 0,
        before_created_at: Optional[int] = None,
        before_id: Optional[str] = None,
    ):
        return await self._active.get_group_messages(
            group_id, limit, offset, before_created_at, before_id
        )

    async def send_group_message(self, group_id, body, msg_type="text", **kw):
        return await self._active.send_group_message(
            group_id, body, msg_type, **kw
        )

    async def get_group_unread(self, limit: int = 50):
        return await self._active.get_group_unread(limit)

    async def send_direct_message(self, to_uid, body, msg_type="text", **kw):
        return await self._active.send_direct_message(
            to_uid, body, msg_type, **kw
        )

    async def get_direct_unread(self, limit: int = 50):
        return await self._active.get_direct_unread(limit)

    async def upload_media_bytes(self, file_data, filename, ctype="image/jpeg"):
        return await self._active.upload_media_bytes(file_data, filename, ctype)

    async def download_media(self, url: str, save_path: str):
        return await self._active.download_media(url, save_path)

    async def get_updates_difference(self, pts: int, limit: int = 100):
        return await self._active.get_updates_difference(pts, limit)

    async def mark_group_read(self, group_id: str, seq: int):
        return await self._active.mark_group_read(group_id, seq)

    async def mark_direct_read(self, peer_uid: str, seq: int):
        return await self._active.mark_direct_read(peer_uid, seq)