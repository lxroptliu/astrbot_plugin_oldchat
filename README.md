# astrbot_plugin_oldchat

OldChat 平台适配器插件，将 [OldChat](https://github.com/crbot-contrib) 服务器接入 [AstrBot](https://github.com/AstrBotDevs/AstrBot)。

本插件以 AstrBot Star 形式分发，加载时通过 `@register_platform_adapter` 装饰器向 AstrBot 平台注册表注入名为 **`oldchat`** 的适配器。

> **当前版本：2.0.0（阶段 A）**
> 支持 v1 协议稳定运行、v1/v2/auto 版本切换骨架、全部实验性功能配置项声明。
> v2 协议实现与实验性功能的具体行为将在后续版本交付，详见文末「路线图」。

---

## 特性

### 已实现（2.0.0 阶段 A）

- **v1 协议完整可用**：登录、群聊/私聊收发、图片消息、HTTP 轮询、加密 WebSocket
- **协议版本切换**：`api_version` 支持 `v1` / `v2` / `auto`，运行前在 WebUI 选择
- **加密 WebSocket**：ECDH（SECP256R1）会话密钥协商 + AES-256-CBC + HMAC-SHA256
- **应用层心跳**：间隔可配（`ws_ping_interval`），默认 25 秒
- **自动重连**：指数退避 1→60 秒 ±20% 抖动，可关闭退回固定 3 秒
- **Token 过期自动重登**：401 时先 refresh，可选内存密码回退
- **登录失败退避重试**：网络抖动不会让平台永久离线
- **富文本转换**：文本、图片、@提及、引用回复
- **消息去重**：保序保留最近 2000 条 ID
- **日志脱敏**：密码、Token、完整账号**绝不明文输出**
- **全量配置项**：26 项，全部可在 WebUI 修改，实验性功能默认关闭

### 待后续版本（见路线图）

- v2 协议客户端实现（`/v2/*` 端点）
- v2 增量同步（`/v2/updates/difference`）
- 文件资源上传、已读回执、撤回/typing 事件处理
- `/v2/gateway` 折叠网关
- 媒体 URL 多候选回退

---

## 安装

将整个目录置于 AstrBot 的插件目录（默认 `<AstrBot 数据目录>/plugins/`）下，或克隆本仓库到该位置。AstrBot 启动时自动加载。

依赖在插件加载时自动安装：

```
httpx>=0.24
websockets>=13
aiohttp>=3.9
cryptography>=41
```

> `cryptography` 为 v1/v2 加密通道共用，不可移除。
> `websockets` 需 ≥13，旧版参数名不兼容。

---

## 配置

在 AstrBot WebUI → 平台管理 → 添加平台，选择 **`oldchat`**。

### 基础连接

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `id` | string | `oldchat` | 适配器实例 ID（唯一） |
| `base_url` | string | `http://your-server:8080` | OldChat 服务器地址，**不含** `/v1/` 后缀 |
| `media_base_url` | string | `""` | 媒体基础 URL，留空同 `base_url` |
| `identifier` | string | `""` | 登录用户名（v2 内部映射为 `username`） |
| `password` | string | `""` | 登录密码 |
| `use_websocket` | bool | `true` | `true` 用 WS 实时，`false` 用 HTTP 轮询 |
| `poll_interval` | int | `5` | 轮询间隔（秒），仅 `use_websocket=false` 生效 |
| `api_version` | string | `auto` | `v1` / `v2` / `auto` |

### 心跳与重连

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `ws_ping_interval` | int | `25` | WS 应用层心跳间隔（秒），最小 5 |
| `ws_reconnect_backoff` | bool | `true` | 指数退避重连；关闭则固定 3 秒 |
| `login_retry_interval` | int | `10` | 登录失败重试间隔（秒） |

### 协议兼容

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `auth_path_mode` | string | `auto` | 握手路径：`auto` / `v2` / `v1`（自动回退阶段 B 实现） |
| `password_fallback` | bool | `false` | 【实验性】401 时 refresh 失败后用内存密码重登 |
| `sync_on_reconnect` | bool | `true` | 重连后按 `pts` 拉增量（**仅 v2 生效**） |
| `timestamp_auto_scale` | bool | `true` | 时间戳秒/毫秒自动识别 |

### 实验性：媒体（默认关闭）

| 字段 | 类型 | 默认 | 生效版本 | 说明 |
|---|---|---|---|---|
| `media_url_fallback` | bool | `false` | v1/v2 | 媒体 URL 多候选回退 |
| `media_url_candidates` | string | `""` | v1/v2 | 逗号分隔自定义候选 |
| `enable_file_upload` | bool | `false` | 仅 v2 | 文件资源上传 |
| `file_upload_chunk_size` | int | `1048576` | 仅 v2 | 分块大小（字节） |
| `file_upload_max_size` | int | `1073741824` | 仅 v2 | 上传上限（字节），默认 1GB |

### 实验性：事件（默认关闭）

| 字段 | 类型 | 默认 | 生效版本 | 说明 |
|---|---|---|---|---|
| `enable_read_receipt` | bool | `false` | 仅 v2 | 自动已读回执 |
| `enable_recall_handling` | bool | `false` | v1/v2 | 处理 WS `recall` 事件 |
| `enable_typing_presence` | bool | `false` | v1/v2 | 处理 `typing` / `presence` 事件 |

### 实验性：网关（默认关闭）

| 字段 | 类型 | 默认 | 生效版本 | 说明 |
|---|---|---|---|---|
| `enable_gateway` | bool | `false` | 仅 v2 | 走 `/v2/gateway` 折叠网关 |

> **关于实验性功能**：所有 `【实验性】` 配置项默认关闭，开启前请确认对应协议版本已实现。当前阶段 A 中，非 v2 版本下开启 v2 独有开关会打 warning 并忽略。

---

## 协议版本切换

| `api_version` | 行为 |
|---|---|
| `v1` | 强制 v1，行为与旧版一致（仅修 bug） |
| `v2` | 强制 v2。**阶段 A 尚未实现，启动会报「协议版本未就绪」并退出** |
| `auto` | 先试 v2；v2 未实现或端点不存在（404/405/501）→ 回退 v1；401/403 不回退 |

**运维约束**：

1. 切换 `api_version` 后**必须重新加载平台实例**，运行期不热切。
2. `auto` 回退**仅发生在登录阶段**；认证失败不会被版本回退掩盖。
3. v1 与 v2 的 `session_id` / `pts` 不互通；切换版本后去重缓存、昵称缓存清空。
4. 启动日志首行打印：`[OldChat] 使用协议版本: v1` / `v2` / `auto→v1` / `auto→v2`。

---

## 安全说明

适配器对以下信息严格脱敏，**绝不写入日志**：

- 登录密码
- `access_token` / `refresh_token`
- `session_id` / WS URL 中的 `sid`、`token` 查询参数
- 完整用户名 / UID（日志中折叠为 `abc***xyz` 形式）

实现方式见 `oldchat/_redact.py`：

- `mask(value)`：半敏感字段折叠
- `redact(text)`：异常信息与接口回显清洗
- `safe_url(url)`：去掉 URL query / fragment

`password_fallback` 开启时，密码**仅存于进程内存**，用于 401 重登，不落盘、不写日志。默认关闭。

---

## 架构

```
astrbot_plugin_oldchat/
├── main.py                 # AstrBot Star 入口（壳插件）
├── metadata.yaml           # 插件元数据
├── requirements.txt        # Python 依赖
├── _conf_schema.json       # 插件配置 schema（空，配置在平台层）
└── oldchat/
    ├── __init__.py         # 导出 OldChatAdapter
    ├── _redact.py          # 日志脱敏（mask / redact / safe_url）
    ├── crypto.py           # ECDH 握手 + AES-CBC/HMAC（v1/v2 共用）
    ├── client_base.py      # OldChatClientBase 抽象接口
    ├── client_v1.py        # v1 实现（当前唯一可用客户端）
    ├── client_v2.py        # v2 骨架（阶段 B 实现）
    ├── client_factory.py   # 按 api_version 构造；AutoClient 包装
    ├── ws.py               # WS 连接/心跳/重连，profile 驱动 v1/v2
    ├── adapter.py          # 适配器主体（版本选择 + 开关裁剪 + 分发）
    └── event.py            # AstrMessageEvent 实现（消息发送）
```

### 消息流

```
OldChat Server
    │
    ├── WebSocket (加密) ──→ ECDHSession.decrypt()
    │                          │
    └── HTTP 轮询 ────────→ client.get_*_unread()
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
                          → client.send_*_message()
```

### v1 / v2 复用关系

| 模块 | v1 | v2 | 说明 |
|---|---|---|---|
| 加密（ECDH/AES/HMAC） | ✅ | ✅ | 共用 `crypto.py` |
| WS 主循环 | ✅ | ✅ | 共用 `ws.py`，profile 驱动差异 |
| HTTP 客户端 | `client_v1.py` | `client_v2.py` | 端点与登录字段不同 |
| 适配器 / 事件 | 共用 | 共用 | 通过 `OldChatClientBase` 无感切换 |

---

## 使用

启动 AstrBot → WebUI 平台管理 → 添加 `oldchat` 适配器 → 填写配置 → 保存。

机器人自动登录 OldChat 并开始接收消息。所有 AstrBot 插件（LLM 对话、指令、知识库等）通过 `oldchat` 适配器响应。

---

## 故障排查

| 现象 | 排查 |
|---|---|
| 启动日志 `请配置 identifier 和 password` | WebUI 未填登录凭证 |
| 启动日志 `协议版本未就绪` | `api_version=v2`，阶段 A 未实现，改 `v1` 或 `auto` |
| 日志反复 `登录失败` | 检查 `base_url` 是否可达、凭证是否正确；日志中异常已脱敏，对照服务端确认 |
| WS 反复重连 | 检查 `ws_ping_interval`；服务端可能要求特定心跳；看日志中 `WS 异常` 的脱敏信息 |
| 图片发送失败 | 检查 `/v1/media` 上传是否可用；服务端日志为准 |
| 群/私聊消息混淆 | 确认服务端消息体含 `group_id` 字段；缺则视为私聊 |

---

## 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| **A（当前）** | 修 bug、脱敏、双版本骨架、全配置项声明 | ✅ 已交付 |
| **B** | v2 协议客户端实现、增量同步、`auto` 自动回退、密码回退落地 | ⏳ 待交付 |
| **C** | 媒体多候选、文件上传、已读回执、撤回/typing 事件、网关 | ⏳ 待交付 |

阶段 B 需先确认 v2 服务端的以下细节（抓包确认）：WS 帧 `type` 字符串、握手路径、登录字段兼容性、图片上传端点、`/v2/updates/difference` 返回结构。

---

## 许可证

MIT License（LICENSE 文件将在阶段 C 补齐）