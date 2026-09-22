# ClineDesktop2API

本机反向代理：客户端请求发到本机端口，由代理用 Python OpenSSL 直连 `api.cline.bot`，
顺手解决 Cline 桌面端 TLS 指纹被 Cloud Armor 判 403 的问题，并支持**多账号同时使用**——
每个请求按客户端出示的 apikey 路由到对应账号：一个 key 进，一个凭据文件出。

## 它解决什么问题

Cline 桌面端（WebView2 + Bun sidecar）的 TLS 握手被 Google Cloud Armor 的 JA3/JA4
指纹判为可疑 → HTTP 403。

受影响的 TLS 栈：Go `crypto/tls`、Windows schannel、Bun `usockets`。
能通过的那一个：**Python 的 OpenSSL**（ClientHello 指纹不同）。

## 工作方式

```
客户端 → HTTP → localhost:61022（本代理）→ HTTPS → api.cline.bot
                Python OpenSSL（通过 JA3 检查）
```

代理**自持每个已登记账号的完整认证生命周期**，不依赖桌面端应用换取令牌：

1. `--login` 为一个账号跑一次 WorkOS 设备码流程（打印 URL，浏览器确认）；
2. 向 Cline API 登记该账号，凭据文件落到 `apikey/`；
3. 为该账号签发 apikey 并写入根目录 `config.json` 的 `api_keys` 映射；
4. 每个进来的请求按其出示的 key 路由到对应账号，注入该账号的令牌转发上游；
5. 令牌临近过期时原地刷新，每个凭据文件一把自己的锁，账号之间互不阻塞。

## 文件表

| 文件 | 说明 |
|---|---|
| `main.py` | CLI 入口（`--port`、`--bind`、`--log`、`--rate-limit`、`--desensitize`、`--login`、`--no-menu`）+ 交互菜单 |
| `config.py` | 根 `config.json`：服务设置、auth 设置、apikey → 凭据文件映射（`resolve_account` / `register_account`） |
| `config.example.json` | 随仓库分发的无账号模板，首次显式登录时复制为 `config.json` |
| `server.py` | HTTP 服务：Cline 直通、`/v1/models`、`/v1/chat/completions`、`/v1/messages`，按请求选账号 |
| `upstream.py` | Python OpenSSL 上游客户端（绕 Cloud Armor，给选中账号的令牌加 `workos:` 前缀注入） |
| `auth.py` | 按账号的 OAuth 生命周期：设备码、登记、刷新、凭据文件（`load_tokens`/`save_tokens`/`get_valid_token`/`install_account`/`login`） |
| `anthropic.py` | Anthropic Messages API ↔ OpenAI chat completions 转换 |
| `ratelimit.py` | 每 IP 最小请求间隔限流器 |
| `desensitize.py` | 提示词审核触发词重写（可选） |
| `reqlog.py` | 请求日志，凭据脱敏 |
| `banner.py` | 启动横幅 |
| `proxy.py` | 兼容入口：`python proxy.py` 等同 `python main.py` |
| `tests/selfcheck.py` | 离线自检（不发网络、不读真实配置） |
| `apikey/` | 每账号一个凭据文件，首次登录创建，已 gitignore |
| `auth_flow.py`、`start_cline.bat` | 遗留脚本，见 [遗留脚本](#遗留脚本) |

## 安装与快速开始

运行环境：Windows + 仓库 venv（Python 3.11），命令在仓库根执行；运行期零第三方依赖。

### 1. 登录账号

```bat
.venv\Scripts\python.exe main.py --login
```

浏览器打开打印的验证 URL 并确认。首次登录会用 `config.example.json` 生成根目录
`config.json`，写入该账号凭据文件 `apikey\<账号>.json`，并把它的 apikey 登记进
`config.json` 的 `api_keys`。再跑一次加新账号；**同一账号**重复登录复用同一文件和
同一 apikey。

### 2. 启动服务

```bat
.venv\Scripts\python.exe main.py --no-menu
```

不带参数进交互菜单：`1` 状态（选账号后显示凭据文件、User ID、令牌过期时间）、
`2` 启动服务（Ctrl-C 停）、`3` 模型列表（前 30 个）、`4` 测试对话、`5` 登录/加账号、
`6` 退出。1/3/4 都会先让你选已登记的 key——没有"默认第一个账号"。

监听地址取 `config.json` 的 `port`/`bind`（模板默认 `61022`/`127.0.0.1`；本机如落在
Windows 排除端口范围会 `WinError 10013`，换端口即可，本机已改用 `8787`）。

### 3. 验证

```bat
curl.exe http://127.0.0.1:8787/healthz
```

返回 `200 {"status":"ok"}`；`/healthz` 是唯一不需要 apikey 的路径。

## 多账号与 apikey

apikey 是 64 位十六进制随机串，只存在于 `config.json` 的 `api_keys` 映射表里：

```bat
.venv\Scripts\python.exe -c "import json;[print(k,'->',v) for k,v in json.load(open('config.json'))['api_keys'].items()]"
```

- **换 key**（不重新登录）：编辑 `api_keys`，把某条目左边的字符串改成新的随机值（右边仍
  指向原凭据文件），保存后**重启服务**，之后只有新 key 能用。**停用 key**：删掉条目并重启。
- 相对路径相对 `config.json` 所在目录解析，绝对路径原样使用；映射表**启动时读入**，
  改完必须重启，没有热加载。

## 配置

`config.json` 与 `config.py` 同目录（源码树即仓库根；PyInstaller 单文件版即 exe 所在目录）。
`config.example.json` 是无账号模板：

```json
{
  "port": 61022,
  "bind": "127.0.0.1",
  "log_path": null,
  "rate_limit": null,
  "desensitize": false,
  "api_keys": {},
  "auth": {
    "workos_base_url": "https://api.workos.com",
    "cline_base_url": "https://api.cline.bot",
    "client_id": "client_01K3A541FN8TA3EPPHTD2325AR",
    "user_agent": "Cline/0.0.32",
    "refresh_buffer_ms": 300000,
    "request_timeout_seconds": 30
  }
}
```

字段语义：

- `port` / `bind`：监听地址；`log_path`：请求日志路径，`null` 关闭，日志对凭据脱敏。
- `rate_limit`：每 IP 最小请求间隔，`"500ms"` / `"2s"` / `"3m"`，`null` 不限；超限返回 `429 rate_limit_error` 并带 `Retry-After`。
- `desensitize`：是否把提示词里的审核触发词改写成中性表述；`api_keys`：apikey → 凭据文件路径，唯一路由表。
- `auth`：WorkOS/Cline 端点、`client_id`、`user_agent`、`refresh_buffer_ms`（默认 300000 毫秒 = 5 分钟）、`request_timeout_seconds`。
- **没有环境变量覆盖**，也没有第二份配置文件；`config.json` 是唯一事实源。
- CLI 仅 `--port` / `--bind` / `--log` / `--rate-limit` / `--desensitize`，**显式传入才**覆盖配置文件。
- 缺 `config.json` 直接启动报错退出（退出码 2），提示复制模板或 `--login`；**不会**回退到任何默认账号。

## 路由与错误语义

除 `/healthz` 外所有路径都要求已登记的 key：

| 客户端出示的凭据 | 结果 |
|---|---|
| `Authorization: Bearer <key>` | 路由到该 key 对应的账号凭据文件 |
| `x-api-key: <key>` | 仅在没有 `Authorization: Bearer` 头时读取 |
| 缺失，或 key 不在 `api_keys` | `401` `{"error":{"message":"invalid API key","type":"authentication_error"}}` |
| key 已登记但该账号取不到活令牌 | `502` `{"error":{"message":"account auth failed: …","type":"account_auth_error"}}` |
| 上游返回 `401` / `403` | 原样透传 |

没有"第一个账号"这种默认值。客户端自带的凭据（含 `x-api-key`）不转发上游——
`Authorization` 被替换为选中账号的 `workos:<token>`。`OPTIONS` 返回 `204`；未知路径原样
透传到上游。

## 客户端接入

端点都用代理签发的 key：`Authorization: Bearer <你的apikey>`（或 `x-api-key: <你的apikey>`）。

### OpenAI 兼容

Base URL `http://127.0.0.1:8787/v1`；端点 `GET /v1/models`（合并免费档模型 id）、
`POST /v1/chat/completions`（非流式会解开上游的 `{"data":{…}}` 包装，返回顶层对象）。

```bash
curl.exe http://127.0.0.1:8787/v1/models -H "Authorization: Bearer <你的apikey>"
curl.exe http://127.0.0.1:8787/v1/chat/completions -H "Authorization: Bearer <你的apikey>" -H "Content-Type: application/json" -d "{\"model\":\"~openai/gpt-luna-latest\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}],\"max_tokens\":200}"
```

SDK 需自行安装（仓库 venv 只有标准库）：`pip install openai`。

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="<你的apikey>")
r = client.chat.completions.create(
    model="~openai/gpt-luna-latest",
    messages=[{"role": "user", "content": "Hello"}],
    max_tokens=200,
)
print(r.choices[0].message.content)
```

（Windows 上 `curl` 是 `Invoke-WebRequest` 别名，命令行须用 `curl.exe`。）

### Anthropic 兼容（Claude Code 等）

Base URL `http://127.0.0.1:8787`；端点 `POST /v1/messages`：

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8787"
$env:ANTHROPIC_AUTH_TOKEN = "<你的apikey>"
```

代理内部**始终**向上游请求 SSE，客户端拿到什么格式只由请求体的 `stream` 标志决定，见
[流式行为](#流式行为)。

### Cline 桌面端

**只改 `baseUrl` 接不进来**：桌面端用自己账号的 WorkOS token 发请求，那不是登记过的
apikey，会被 401。必须配一个能自定义 `apiKey` 的 OpenAI 兼容 provider 条目：`baseUrl`
填 `http://127.0.0.1:8787/v1`，`apiKey` 填已登记 apikey。只能设 `baseUrl`、不能设 key
的配置（如内置 `cline` provider）无法用于多账号模式。

## 流式行为

- 上游响应**逐块转发**（`stream: true` 的 `/v1/chat/completions`、`/v1/messages` 以及
  `/api/v1/*` 直通）：每块到达即写回并 flush，不整段缓冲，首块到达时间跟随上游自身。
- 流式响应发出头时长度未知，因此以 `Connection: close` 定界、**不带 `Content-Length`**；
  客户端必须接受 EOF 结束的响应体（curl、OpenAI/Anthropic SDK、Claude Code、Cline 桌面端均可）。
  这种转发只在新启动的代理进程里生效，改动后需重启（Ctrl-C 后 `python main.py --no-menu`）。
- `/v1/messages` 内部**始终向上游请求 SSE**，客户端侧格式由 `stream` 决定：
  - `stream: true` → 增量 Anthropic SSE 事件（`message_start` / `content_block_start` /
    `content_block_delta` / `content_block_stop` / `message_delta` / `message_stop`），
    边收边转，转换可跨任意 UTF-8/分帧边界；
  - `stream: false` 或省略 → 聚合上游文本与工具调用，返回一个 Anthropic JSON `message`。

## 有效期与自动续期

- **apikey 本身没有过期时间，也没有撤销机制**：条目还在 `api_keys` 里就一直有效（映射表启动时读入，改完需重启）；删掉条目重启后失效，删掉/损坏它指向的凭据文件则下一个请求即失效（凭据文件每次请求实时读取）。
- **账号 access 令牌约 1 小时有效**（取自登记返回的 access JWT 的 `exp`）。
- 每次请求前检查该账号文件：剩余不足 `auth.refresh_buffer_ms`（默认 5 分钟）时自动 refresh → register → 写回同一文件，然后照常转发。客户端不用管令牌过期，**apikey 可以长期填着不动**。
- 刷新会轮换 refresh token 并立即写回该账号文件；刷新或登记失败返回 `502 account_auth_error`，**不回退旧令牌**，也不影响其它账号。
- 账号级失效（被吊销、refresh 失效、凭据文件丢失或损坏）：对该账号重新 `--login`，复用同一文件和同一 key。

## 免费档计费改写

Cline 按模型 id 计费：`deepseek/deepseek-v4.1-flash` 计费，`cline-free/deepseek-v4.1-flash`
不计费。代理自动把付费 id 改写成其 `cline-free/` 等价物（列表取自
`/api/v1/ai/cline/recommended-models`，缓存 10 分钟），对任何带 `model` 的请求生效；
免费 id 同时合入 `/v1/models`，OpenAI 兼容客户端可直接发现。上游请求一律注入桌面客户端
标识头（`X-CLIENT-TYPE` 等），保证免费档定价被应用。

## 测试与 CI

```bat
.venv\Scripts\python.exe tests\selfcheck.py
```

离线自检，覆盖 Anthropic↔OpenAI 转换、脱敏、审核词重写、根配置解析（模板、覆盖、相对
路径、手改重载、key 登记）、凭据文件加载（两种 key 写法）、选中文件上的刷新/登记
（mocked 网络）、`login()`（mocked 设备码流程）、HTTP 层 key 选择 / `401` / `502`，以及
流式路径（真实上游响应体、逐块转发不带 `Content-Length`、跨任意边界的增量 Anthropic 事件
转换）。全程不访问网络，不读真实 `config.json` 或凭据（夹具建在 `temp/`，退出即清理）。
CI（`.github/workflows/ci.yml`）在 push master 与 PR 时于 Windows + Python 3.11 上跑同一
自检；`release.yml` 把 `main.py` 打成单文件 exe。

## 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| `401 authentication_error` | 没带 key，或 key 不在 `api_keys` | 确认 key 完整复制、与 `config.json` 对得上；改过映射要重启 |
| `502 account_auth_error` | key 已登记但该账号取不到活令牌 | 查该账号凭据文件与出网；账号失效就重新 `--login` |
| 上游 `401` / `403` | 账号令牌在上游被拒 | 原样透传，按上一条处理 |
| `429 rate_limit_error`（带 `Retry-After`） | `rate_limit` 生效 | 等 `Retry-After` 秒或调大间隔 |
| `405` / `400 invalid_request_error` | 方法非 POST、`/v1/messages` 请求体非法 | 改用 POST、修正 JSON |

- **启动报 `WinError 10013`**：模板默认端口 `61022` 落在本机 Windows 排除端口范围内；换端口改 `config.json` 或临时 `--port`。
- **`max_tokens` 很小（如 16）时上游回 500 `{"error":"empty response content"}`**：deepseek 这类推理模型先花推理预算，正文为空；调大 `max_tokens` 即可，不是代理故障。
- **流式看起来"卡在推理、跑完才一口气输出"**：先确认用的是**重启后**的代理进程（改动前启动的进程仍是整段返回）；重启后仍是"很久没有第一个字"，那是上游推理耗时——代理只逐块转发，不会消除模型吐出第一个 token 之前的等待。
- **一直 401**：key 是否复制完整、`api_keys` 里有没有这一条、改完是否重启。
- **一直 502**：查 `apikey\<账号>.json` 与出网；出网需要代理时给进程设 `HTTPS_PROXY`，**不要写进仓库文件**。
- **日志**：`log_path` 或 `--log` 开启后请求日志对凭据脱敏。

## 凭据安全

`config.json`（含全部 apikey）与 `apikey/`（账号令牌）都在 `.gitignore` 中，永不提交，也不要在
文档、issue、截图里贴真实值。怀疑 key 泄露：从 `api_keys` 删掉该条目并重启，key 即失效；需要时
重新登录补新 key。分享配置用 `config.example.json`，并确认 `api_keys` 为 `{}`。

## 遗留脚本

`auth_flow.py` 和 `start_cline.bat` 早于多账号路由：二者都不被代理导入，也不打进发布二进制
（`release.yml` 只打 `main.py`）；它们仍指向单账号存储和作者本机路径，与当前配置不兼容，
仅作历史保留——**不要**用它们启动代理或登录。

## 为什么是 Python

Go 的 `crypto/tls` 产出的 ClientHello 会被 Cloud Armor 拦；`refraction-networking/utls`
能绕过 JA3，但会强制 HTTP/2（Chrome profile 的 ALPN=h2），而 Go 的 HTTP/1.1 transport 读不了
h2 帧。对这个上游来说，Python 才是正确的 TLS 栈。
