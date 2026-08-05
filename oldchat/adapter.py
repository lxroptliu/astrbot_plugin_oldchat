"""oldchat/adapter.py - OldChat 平台适配器主体。

通过 ``@register_platform_adapter`` 注册到 AstrBot 平台注册表。
负责：
* 登录 OldChat 服务器；
* 通过加密 WebSocket 或 HTTP 轮询接收消息；
* 将 OldChat 消息转换为 ``AstrBotMessage`` 并提交到事件队列；
* 通过 ``send_by_session`` 支持跨会话主动发送。
"""
from __future__ import annotations

import asyncio
import json
import time
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

from .client import OldChatClient
from .event import OldChatPlatformEvent
from .ws import OldChatWS

# ------------------------------------------------------------------ #
# 配置模板（注册到 AstrBot WebUI 平台管理用于生成表单）
# ------------------------------------------------------------------ #

DEFAULT_CONFIG_TEMPLATE: dict[str, Any] = {
    "id": "oldchat",
    "base_url": "http://60.205.94.101:8080",
    "media_base_url": "",
    "identifier": "",
    "password": "",
    "use_websocket": True,
    "poll_interval": 5,
}

CONFIG_METADATA: dict[str, dict[str, Any]] = {
    "id": {
        "type": "string",
        "description": "适配器实例 ID，用于在 AstrBot 中唯一标识。",
        "hint": "建议保持默认或使用易识别名称。",
    },
    "base_url": {
        "type": "string",
        "description": "OldChat 服务器地址（不含 /v1/ 后缀），例如 http://60.205.94.101:8080",
    },
    "media_base_url": {
        "type": "string",
        "description": "媒体文件基础 URL，留空则与 base_url 相同。",
    },
    "identifier": {
        "type": "string",
        "description": "登录用户名（对应 OldChat API 的 identifier 字段）。",
    },
    "password": {
        "type": "string",
        "description": "登录密码。",
    },
    "use_websocket": {
        "type": "bool",
        "description": "是否使用 WebSocket 实时连接，关闭则使用 HTTP 轮询。",
    },
    "poll_interval": {
        "type": "int",
        "description": "HTTP 轮询间隔（秒），仅在 use_websocket=false 时生效。",
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

    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)
        self.settings = platform_settings
        self.client: Optional[OldChatClient] = None
        self.ws: Optional[OldChatWS] = None
        self._running = True
        self._processed_ids: set[str] = set()
        self._nickname_cache: dict[str, str] = {}

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
    # 生命周期
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        """启动适配器并阻塞，直到 ``terminate()`` 被调用。"""
        identifier = self.config.get("identifier", "")
        password = self.config.get("password", "")
        if not identifier or not password:
            logger.error("[OldChat] 请配置 identifier 和 password")
            return

        base_url = self.config.get("base_url", "").rstrip("/")
        media_base_url = self.config.get("media_base_url", "").rstrip("/") or base_url

        self.client = OldChatClient(
            {"base_url": base_url, "media_base_url": media_base_url}
        )

        try:
            await self.client.login(identifier, password)
        except Exception as e:
            logger.error(f"[OldChat] 登录失败: {e}")
            return

        logger.info(
            "[OldChat] 登录成功，UID: %s",
            self.client.user.get("uid", "unknown"),
        )

        if self.config.get("use_websocket", True):
            await self._run_websocket()
        else:
            await self._run_polling()

    async def terminate(self) -> None:
        """终止适配器。"""
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
            self.client.access_token,
            on_unauthorized=self._on_unauthorized,
        )
        self.ws.on_message(self._on_ws_message)
        await self.ws._connect()

    async def _on_unauthorized(self) -> None:
        """Token 过期时重新登录。"""
        assert self.client is not None
        logger.warning("[OldChat] Token 过期，重新登录...")
        try:
            await self.client.login(
                self.config["identifier"], self.config["password"]
            )
            if self.ws:
                self.ws.access_token = self.client.access_token
            self.client.clear_user_cache()
            logger.info("[OldChat] 重新登录成功")
        except Exception as e:
            logger.error(f"[OldChat] 重新登录失败: {e}")

    async def _on_ws_message(self, msg: dict) -> None:
        """处理 WebSocket 推送的消息信封。"""
        if not isinstance(msg, dict):
            return

        msg_type = msg.get("type", "")
        data = msg.get("data", {})

        if msg_type in ("group_message", "direct_message"):
            await self._process_message(data)
        # 其他事件类型（typing、online 等）忽略

    # ------------------------------------------------------------------ #
    # 轮询模式
    # ------------------------------------------------------------------ #

    async def _run_polling(self) -> None:
        interval = int(self.config.get("poll_interval", 5))
        logger.info("[OldChat] 开始轮询未读消息，间隔 %d 秒", interval)
        while self._running:
            try:
                await self._poll_once()
            except Exception as e:
                logger.error(f"[OldChat] 轮询出错: {e}")
            await asyncio.sleep(interval)

    async def _poll_once(self) -> None:
        """执行一次轮询，拉取群聊和私聊未读消息。"""
        assert self.client is not None

        # 群聊未读
        try:
            group_resp = await self.client.get_group_unread(limit=50)
            for msg in self._extract_messages(group_resp):
                await self._process_message(msg)
        except Exception as e:
            logger.error(f"[OldChat] 获取群未读失败: {e}")

        # 私聊未读
        try:
            direct_resp = await self.client.get_direct_unread(limit=50)
            for msg in self._extract_messages(direct_resp):
                await self._process_message(msg)
        except Exception as e:
            logger.error(f"[OldChat] 获取私聊未读失败: {e}")

    @staticmethod
    def _extract_messages(resp: Any) -> list[dict]:
        """从未读响应中提取消息列表。"""
        if isinstance(resp, dict):
            return resp.get("messages", [])
        if isinstance(resp, list):
            return resp
        return []

    # ------------------------------------------------------------------ #
    # 消息处理与分发
    # ------------------------------------------------------------------ #

    async def _process_message(self, raw_msg: dict) -> None:
        """处理单条 OldChat 原始消息：去重、过滤自身、转换并分发。"""
        if not isinstance(raw_msg, dict):
            return

        msg_id = str(raw_msg.get("id", ""))
        if not msg_id or msg_id in self._processed_ids:
            return
        self._processed_ids.add(msg_id)
        if len(self._processed_ids) > 2000:
            self._processed_ids = set(list(self._processed_ids)[-1000:])

        # 过滤自己发的消息
        assert self.client is not None and self.client.user is not None
        from_uid = str(raw_msg.get("from_uid", "")).upper()
        self_uid = str(self.client.user.get("uid", "")).upper()
        if from_uid and from_uid == self_uid:
            return

        abm = await self.convert_message(raw_msg)
        if abm is None:
            return

        await self._dispatch(abm)

    async def _dispatch(self, abm: AstrBotMessage) -> None:
        """创建事件并提交到 AstrBot 事件队列。"""
        assert self.client is not None
        event = OldChatPlatformEvent(
            message_str=abm.message_str,
            message_obj=abm,
            platform_meta=self.meta(),
            session_id=abm.session_id,
            client=self.client,
        )
        self.commit_event(event)

    # ------------------------------------------------------------------ #
    # 消息转换
    # ------------------------------------------------------------------ #

    async def convert_message(self, raw_msg: dict) -> Optional[AstrBotMessage]:
        """将 OldChat 原始消息转换为 ``AstrBotMessage``。"""
        assert self.client is not None and self.client.user is not None

        abm = AstrBotMessage()
        abm.self_id = str(self.client.user.get("uid", ""))
        abm.message_id = str(raw_msg.get("id", ""))
        abm.timestamp = int(raw_msg.get("created_at", time.time()))
        abm.raw_message = raw_msg

        from_uid = str(raw_msg.get("from_uid", ""))
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

        # 解析消息体
        msg_type = raw_msg.get("msg_type", "text")
        body = raw_msg.get("body", "")
        message_str, mentions, quote = self._parse_body(body)

        message_chain: list = []

        # 引用回复
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

        # @ 提及
        for m in mentions:
            uid = str(m.get("uid", ""))
            name = str(m.get("name", uid))
            message_chain.append(At(qq=uid, name=name))

        if msg_type == "image" and raw_msg.get("media_url"):
            media_url = self.client._resolve_media_url(raw_msg["media_url"])
            message_chain.append(Image(file=media_url, url=media_url))
            abm.message_str = message_str or "[图片]"
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
            from_uid,
            abm.message_str[:50],
        )
        return abm

    @staticmethod
    def _parse_body(body: Any) -> tuple[str, list, dict]:
        """解析消息体，返回 (纯文本, mentions, quote)。

        OldChat 的结构化消息体使用 v2 JSON 格式：
        ``{"v": 2, "text": "...", "mentions": [...], "quote": {...}}``
        """
        if isinstance(body, dict):
            if body.get("v") == 2:
                text = str(body.get("text", ""))
                text = (
                    text.replace("\u200b", "")
                    .replace("\u200c", "")
                    .replace("\u200d", "")
                    .replace("\ufeff", "")
                )
                return text, body.get("mentions", []), body.get("quote") or {}
            return str(body.get("text", "")), [], {}

        if isinstance(body, str) and body.startswith("{"):
            try:
                obj = json.loads(body)
                if isinstance(obj, dict):
                    if obj.get("v") == 2:
                        text = str(obj.get("text", ""))
                        text = (
                            text.replace("\u200b", "")
                            .replace("\u200c", "")
                            .replace("\u200d", "")
                            .replace("\ufeff", "")
                        )
                        return (
                            text,
                            obj.get("mentions", []),
                            obj.get("quote") or {},
                        )
                    return str(obj.get("text", body)), [], {}
            except json.JSONDecodeError:
                pass

        return str(body) if body else "", [], {}

    # ------------------------------------------------------------------ #
    # 发送者名称解析
    # ------------------------------------------------------------------ #

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
    # 跨会话发送（供其他插件主动推送消息）
    # ------------------------------------------------------------------ #

    async def send_by_session(
        self,
        session: MessageSesion,
        message_chain: MessageChain,
    ) -> None:
        """通过可持久化的会话数据发送消息。"""
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
                name = seg.name or str(seg.qq)
                text_parts.append(f"@{name} ")
            elif isinstance(seg, Image):
                try:
                    path = await seg.convert_to_file_path()
                    image_paths.append(path)
                except Exception as e:
                    logger.error(f"[OldChat] 处理图片失败: {e}")

        text = "".join(text_parts).strip()

        # 发送文本
        if text:
            try:
                if is_group:
                    await self.client.send_group_message(target, text)
                else:
                    await self.client.send_direct_message(target, text)
            except Exception as e:
                logger.error(f"[OldChat] send_by_session 发送文本失败: {e}")

        # 发送图片
        for img_path in image_paths:
            try:
                import os

                with open(img_path, "rb") as f:
                    img_bytes = f.read()
                media_url, thumb_url = await self.client.upload_media_bytes(
                    img_bytes, os.path.basename(img_path) or "image.jpg"
                )
                if media_url:
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
                logger.error(f"[OldChat] send_by_session 发送图片失败: {e}")

        await super().send_by_session(session, message_chain)
