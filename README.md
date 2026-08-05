# astrbot_plugin_oldchat

OldChat 平台适配器插件，将 [OldChat](https://github.com/crbot-contrib) 服务器接入 [AstrBot](https://github.com/AstrBotDevs/AstrBot)。

本插件以 AstrBot Star 形式分发，加载时通过 `@register_platform_adapter` 装饰器向 AstrBot 平台注册表注入名为 **`oldchat`** 的适配器。

## 特性

- **WebSocket 实时消息**：通过 ECDH 握手 + AES-CBC/HMAC-SHA256 加密通道接收消息
- **HTTP 轮询备选**：可在配置中关闭 WebSocket，使用未读消息轮询
- **群聊与私聊**：支持群消息和私聊消息的双向收发
- **富文本消息**：支持文本、图片、@提及、引用回复的消息链转换
- **媒体上传**：发送图片时自动上传到 OldChat 媒体服务器
- **自动重连**：WebSocket 断线自动重连，Token 过期自动重新登录

## 安装

将整个目录置于 AstrBot 的插件目录（默认是 `<AstrBot 数据目录>/plugins/`）下，或直接克隆本仓库到该位置。AstrBot 启动时会自动加载。

依赖会在插件加载时自动安装：

```
httpx
websockets
cryptography
aiohttp
```

## 配置

在 AstrBot WebUI → 平台管理 → 添加平台，选择 **`oldchat`**，填写：

| 字段 | 说明 |
|---|---|
| `id` | 适配器实例 ID（唯一） |
| `base_url` | OldChat 服务器地址（**不含** `/v1/` 后缀），例如 `http://60.205.94.101:8080` |
| `media_base_url` | 媒体文件基础 URL，留空则与 `base_url` 相同 |
| `identifier` | 登录用户名（对应 OldChat API 的 `identifier` 字段） |
| `password` | 登录密码 |
| `use_websocket` | 是否使用 WebSocket 实时连接（`true` 推荐） |
| `poll_interval` | HTTP 轮询间隔（秒），仅在 `use_websocket=false` 时生效 |

> **注意**：`base_url` 不要带 `/v1/` 后缀，适配器内部会自动拼接 `/v1/...` 路径。

## 架构

```
astrbot_plugin_oldchat/
├── main.py                 # AstrBot Star 入口（壳插件）
├── metadata.yaml           # 插件元数据
├── requirements.txt        # Python 依赖
├── _conf_schema.json       # 插件配置 schema（空，配置在平台层）
└── oldchat/                # 适配器实现包
    ├── __init__.py          # 导出 OldChatAdapter
    ├── adapter.py           # Platform 适配器主体（生命周期/消息转换/分发）
    ├── event.py             # AstrMessageEvent 实现（消息发送）
    ├── client.py            # OldChat HTTP API 客户端
    └── ws.py                # OldChat 加密 WebSocket 客户端
```

### 消息流

```
OldChat Server
    │
    ├── WebSocket (加密) ──→ OldChatWS._decrypt()
    │                          │
    └── HTTP 轮询 ────────→ OldChatClient.get_*_unread()
                               │
                               ▼
                    OldChatAdapter._process_message()
                               │
                               ▼
                    OldChatAdapter.convert_message()
                          → AstrBotMessage
                               │
                               ▼
                    OldChatAdapter._dispatch()
                          → OldChatPlatformEvent
                               │
                               ▼
                    Platform.commit_event()
                          → AstrBot 事件队列
                               │
                               ▼
                    AstrBot Pipeline (LLM / 插件 / ...)
                               │
                               ▼
                    OldChatPlatformEvent.send()
                          → OldChatClient.send_*_message()
```

## 使用

启动 AstrBot 后，在 WebUI 平台管理中添加 `oldchat` 适配器并填写配置。机器人会自动登录 OldChat 并开始接收消息。

所有 AstrBot 插件（LLM 对话、指令、知识库等）都会通过 `oldchat` 适配器响应。

## 许可证

MIT License
