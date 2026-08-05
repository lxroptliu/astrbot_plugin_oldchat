"""oldchat/event.py - OldChat 平台消息事件实现。

继承 ``AstrMessageEvent``，实现 ``send()`` 方法将 AstrBot 消息链
转换回 OldChat 的文本 / 图片消息并发送。
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.platform import AstrBotMessage, MessageType, PlatformMetadata

if TYPE_CHECKING:  # pragma: no cover
    from .client import OldChatClient


class OldChatPlatformEvent(AstrMessageEvent):
    """OldChat 适配器的消息事件实现。"""

    def __init__(
        self,
        message_str: str,
        message_obj: AstrBotMessage,
        platform_meta: PlatformMetadata,
        session_id: str,
        client: "OldChatClient",
    ) -> None:
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.client = client

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #

    def _get_target(self) -> tuple[str, bool]:
        """返回 (目标 ID, 是否群聊)。"""
        if self.get_message_type() == MessageType.GROUP_MESSAGE:
            return self.message_obj.group_id or self.session_id, True
        return self.get_sender_id(), False

    async def _send_text(self, target: str, is_group: bool, text: str) -> None:
        if is_group:
            await self.client.send_group_message(target, text)
        else:
            await self.client.send_direct_message(target, text)

    async def _send_image(self, target: str, is_group: bool, image_path: str) -> None:
        """读取本地图片文件，上传后发送。"""
        try:
            with open(image_path, "rb") as f:
                img_bytes = f.read()
        except Exception as e:
            logger.error(f"[OldChat] 读取图片失败: {e}")
            return

        media_url, thumb_url = await self.client.upload_media_bytes(
            img_bytes, os.path.basename(image_path) or "image.jpg"
        )
        if not media_url:
            logger.error("[OldChat] 图片上传失败，未获得 URL")
            return

        if is_group:
            await self.client.send_group_message(
                target, "", "image", media_url=media_url, thumb_url=thumb_url
            )
        else:
            await self.client.send_direct_message(
                target, "", "image", media_url=media_url, thumb_url=thumb_url
            )

    # ------------------------------------------------------------------ #
    # 核心发送入口
    # ------------------------------------------------------------------ #

    async def send(self, message: MessageChain) -> None:
        """将 MessageChain 发送到 OldChat。"""
        target, is_group = self._get_target()

        text_parts: list[str] = []
        image_paths: list[str] = []

        for seg in message.chain:
            if isinstance(seg, Plain):
                if seg.text and seg.text.strip():
                    text_parts.append(seg.text)
            elif isinstance(seg, At):
                # 将 @提及 转换为文本
                name = seg.name or str(seg.qq)
                text_parts.append(f"@{name} ")
            elif isinstance(seg, Image):
                try:
                    path = await seg.convert_to_file_path()
                    image_paths.append(path)
                except Exception as e:
                    logger.error(f"[OldChat] 处理图片组件失败: {e}")
            elif isinstance(seg, Reply):
                # Reply 在 OldChat 中无直接对应，跳过
                continue

        # 先发送文本
        text = "".join(text_parts).strip()
        if text:
            try:
                await self._send_text(target, is_group, text)
            except Exception as e:
                logger.error(f"[OldChat] 发送文本失败: {e}")

        # 再逐张发送图片
        for img_path in image_paths:
            try:
                await self._send_image(target, is_group, img_path)
            except Exception as e:
                logger.error(f"[OldChat] 发送图片失败: {e}")

        await super().send(message)
