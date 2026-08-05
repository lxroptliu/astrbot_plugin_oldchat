"""AstrBot 插件入口（壳插件）。

仅做一件事：导入 ``oldchat`` 子包以触发 ``@register_platform_adapter``
装饰器，将 ``OldChatAdapter`` 注册进 AstrBot 平台注册表。
"""
from __future__ import annotations

from astrbot.api.star import Context, Star, register


@register(
    "oldchat_adapter",
    "crbot-contrib",
    "OldChat 平台适配器，将 OldChat 服务器接入 AstrBot",
    "1.0.0",
    "https://github.com/crbot-contrib/astrbot_plugin_oldchat",
)
class OldChatAdapterPlugin(Star):
    """壳插件：仅用于触发平台适配器的注册。"""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        # 导入子包，触发 @register_platform_adapter 装饰器
        from .oldchat.adapter import OldChatAdapter  # noqa: F401
