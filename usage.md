# ClineDesktop2API 使用说明（多账号）

本机反代：客户端请求发到本机端口，代理用 Python OpenSSL 直连 `api.cline.bot`，
绕开 Cline 桌面端 TLS 指纹被 Cloud Armor 判 403 的问题；多个账号同时可用，
每个请求按客户端出示的 apikey 路由到对应账号。适用本机 Windows + 仓库 venv
（Python 3.11），命令在仓库根执行；实现原理见 `docs/PROJECT.md`。

## 一、快速开始（3 步）

### 1. 登录账号

```bat
.venv\Scripts\python.exe main.py --login
```

在浏览器打开打印的验证 URL 并确认。首次登录会用 `config.example.json` 生成
根目录 `config.json`，并写入账号凭据文件 `apikey\<账号>.json` 及其 apikey
（`config.json` 的 `api_keys` 里）。

### 2. 启动服务

```bat
.venv\Scripts\python.exe main.py --no-menu
```

`--no-menu` 直接起服务；不带参数进交互菜单：`1` 状态（选账号后显示凭据文件、
User ID、令牌过期时间）、`2` 启动服务（Ctrl-C 停）、`3` 模型列表、`4` 测试对话、
`5` 登录/加账号、`6` 退出；1/3/4 都会先让你选账号，没有"默认第一个账号"。

监听地址取 `config.json` 的 `port`/`bind`（本机 `8787`/`127.0.0.1`，模板默认
`61022`）。命令行参数仅 `--port`、`--bind`、`--log`、`--rate-limit`、
`--desensitize`、`--login`、`--no-menu`，显式传入才覆盖配置；没有环境变量覆盖。

### 3. 验证

```bat
curl.exe http://127.0.0.1:8787/healthz
```

返回 `200 {"status":"ok"}`；`/healthz` 是唯一不需要 apikey 的路径。

## 二、查看与管理 apikey

apikey 是 64 位十六进制随机串，只存在 `config.json` 的 `api_keys` 映射表里。查看：

```bat
.venv\Scripts\python.exe -c "import json;[print(k,'->',v) for k,v in json.load(open('config.json'))['api_keys'].items()]"
```

`config.json` 字段：`port`、`bind`、`log_path`（请求日志路径，`null` 关）、
`rate_limit`（每 IP 最小请求间隔，`"500ms"`/`"2s"`/`"3m"`，`null` 不限）、
`desensitize`（是否重写提示词审核触发词）、`api_keys`（apikey → 凭据文件路径，
唯一路由表）、`auth`（`workos_base_url`/`cline_base_url`/`client_id`/`user_agent`/
`refresh_buffer_ms`/`request_timeout_seconds`）。

`api_keys` 的相对路径相对 `config.json` 所在目录解析，绝对路径原样使用。改 key
字符串、改它指向的文件、增删条目，都是**重启后生效**，没有热加载。停用一个 key：
从 `api_keys` 删掉对应条目并重启（详见第五节）。

## 三、客户端接入

除 `/healthz` 外所有路径都要已登记 apikey：`Authorization: Bearer <apikey>` 优先，
仅有 `x-api-key: <apikey>` 时也可用（只在没有 Bearer 头时读取）。客户端自带的
其它凭据不会转发给上游。下文 `<你的apikey>` 为占位符。

### OpenAI 兼容

Base URL `http://127.0.0.1:8787/v1`；端点 `GET /v1/models`（合并免费档模型 id）、
`POST /v1/chat/completions`。请求带 `"stream": true` 时上游 SSE **逐块透传**（不整段
缓冲，首块到达即写回）；长度未知，故流式响应用 `Connection: close` 定界、不发
`Content-Length`。代理改了流式行为后需**重启服务**才对已有客户端生效。

客户端侧需自行安装 SDK（本仓库的 venv 只有标准库）：`pip install openai`。

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

```powershell
$env:CLINE_KEY = "<你的apikey>"
curl.exe http://127.0.0.1:8787/v1/models -H "Authorization: Bearer $env:CLINE_KEY"
curl.exe http://127.0.0.1:8787/v1/chat/completions -H "Authorization: Bearer $env:CLINE_KEY" -H "Content-Type: application/json" -d "{\"model\":\"~openai/gpt-luna-latest\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}],\"max_tokens\":200}"
```

（Windows 上 `curl` 是 `Invoke-WebRequest` 别名，须用 `curl.exe`。）

### Anthropic 兼容（Claude Code 等）

Base URL `http://127.0.0.1:8787`；端点 `POST /v1/messages`。代理内部始终向上游请求 SSE：
客户端 `stream: true` 时增量转回 Anthropic SSE，边收边发；`stream: false` 或省略时，
聚合上游文本与工具调用后返回一个 Anthropic JSON message。两种响应格式均可用：

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8787"
$env:ANTHROPIC_AUTH_TOKEN = "<你的apikey>"
```

### Cline 桌面端

**只改 `baseUrl` 接不进来**：桌面端用自己账号的 WorkOS token 发请求，那不是登记
过的 apikey，会被 401。必须给它配一个能自定义 `apiKey` 的 OpenAI 兼容 provider
条目：`baseUrl` 填 `http://127.0.0.1:8787/v1`，`apiKey` 填已登记 apikey。只能设
`baseUrl`、不能设 key 的配置（如内置 `cline` provider）无法用于多账号模式。

## 四、新增账号与更换 key

新增账号再跑一次 `main.py --login`；同一账号重复登录复用同一凭据文件和同一 key。
更换某个客户端在用的 key（不重新登录）：编辑 `config.json` 的 `api_keys`，把该
条目左边的字符串改成新的随机值（右边仍指向原凭据文件），保存后**重启服务**，
之后只有新 key 能用；删除条目则在重启后失效。

## 五、有效期与自动续期

- **apikey 本身没有过期时间，也没有撤销机制**：只是映射表里的随机串，条目还在
  `api_keys` 里就一直有效（映射表在启动时读入）；删掉条目在**重启后**失效，
  删掉/损坏它指向的凭据文件则下一个请求就失效（凭据文件是每次请求实时读取的）。
- **账号 access 令牌约 1 小时有效**（取自 register 返回的 access JWT 的 `exp`）。
- 代理每次请求前检查该账号文件：剩余不足 `auth.refresh_buffer_ms`（默认 `300000`
  毫秒 = 5 分钟）时自动 refresh → register → 写回同一文件，然后照常转发。客户端
  不用管令牌过期，**apikey 可以长期填着不动**。
- 刷新会轮换 refresh token 并立即写回该账号文件；刷新或登记失败时接口返回
  `502 account_auth_error`，不回退旧令牌，也不影响其它账号。
- 账号级失效（Cline/WorkOS 侧被吊销、refresh 失效、凭据文件丢失或损坏）：对该
  账号重新 `--login`，复用同一文件和同一 key。

## 六、报错对照表

| 现象 | 原因 | 处理 |
|---|---|---|
| `401` `{"error":{"message":"invalid API key","type":"authentication_error"}}` | 没带 key 或 key 不在 `api_keys` | 确认 key 完整复制、与 `config.json` 对得上；改过映射要重启 |
| `502` `"type":"account_auth_error"` | key 已登记但该账号取不到活令牌 | 看该账号凭据文件与出网；账号失效就重新 `--login` |
| 上游 `401`/`403` | 账号令牌在上游被拒 | 原样透传，按上一条处理 |
| `429` `"type":"rate_limit_error"`（带 `Retry-After`） | `rate_limit` 生效 | 等 `Retry-After` 秒或调大间隔 |
| `405`/`400` `invalid_request_error` | 方法非 POST、`/v1/messages` 请求体非法 | 改用 POST、修正 JSON |

## 七、排障

- **启动报 `WinError 10013`**：模板默认端口 `61022` 落在本机 Windows 排除端口范围
  内，本机 `config.json` 已改用 `8787`；换端口改 `config.json` 或临时 `--port`。
- **`max_tokens` 很小（如 16）时上游回 500 `{"error":"empty response content"}`**：
  deepseek 这类推理模型先花推理预算，正文为空；调大 `max_tokens` 即可，不是代理故障。
- **流式看起来"卡在推理、跑完才一口气输出"**：先确认用的是**重启后**的代理进程（改动之前
  启动的进程仍是整段返回）；重启后仍是"很久没有第一个字"，那是上游推理耗时——代理只是逐块
  转发，不会消除模型吐第一个 token 之前的等待。
- **一直 401**：key 是否复制完整、`api_keys` 里有没有这一条、改完是否重启。
- **一直 502**：查 `apikey\<账号>.json` 与出网；出网需要代理时给进程设 `HTTPS_PROXY`，
  **不要写进仓库文件**。
- **日志**：`log_path` 或 `--log` 开启后请求日志对凭据脱敏。
- **遗留脚本** `auth_flow.py`、`start_cline.bat` 不在主链路（硬编码本机路径、旧单
  账号写法），不要用它们登录或启动。

## 八、凭据不要提交、不要外泄

- `config.json`（含全部 apikey）和 `apikey/`（账号令牌）都已在 `.gitignore` 中，
  永远不要提交，也不要在文档、issue、截图里贴真实值。
- 怀疑 key 泄露：从 `api_keys` 删掉该条目并重启，key 立即失效；需要时重新登录补新 key。
- 分享配置用 `config.example.json`，并确认 `api_keys` 为 `{}`。
