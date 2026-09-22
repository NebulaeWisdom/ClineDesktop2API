# ClineDesktop2API

Local reverse proxy that fixes Cline Desktop's **"Token registration failed: 403"** error on
`api.cline.bot` — for **several Cline accounts at once**. Every request is routed to the account
whose API key it presents: one key in, one credential file out.

## Problem

Cline Desktop (WebView2 + Bun sidecar) sends TLS handshakes that Google Cloud Armor's **JA3/JA4
fingerprinting** flags as suspicious → HTTP 403 Forbidden.

The affected stacks: Go `crypto/tls`, Windows schannel, Bun `usockets`.
The one that passes: **Python's OpenSSL** (different ClientHello fingerprint).

## Solution

```
client → HTTP → localhost:61022 (this proxy) → HTTPS → api.cline.bot
                Python OpenSSL (passes JA3 check)
```

The proxy owns the whole auth lifecycle for every registered account — it does NOT depend on the
Cline desktop app for tokens:

1. `--login` runs a WorkOS device flow (prints a URL for you to confirm) for one account
2. Registers the account with the Cline API and stores its credential file under `apikey/`
3. Mints an API key for that account and writes it into the root `config.json`
4. Routes each incoming request to the account whose key it presents, injecting that account's token
5. Refreshes the account's token in place when it is near expiry, one lock per credential file

## Files

| File | Purpose |
|---|---|
| `main.py` | CLI entry point (`--port`, `--bind`, `--log`, `--rate-limit`, `--desensitize`, `--login`, `--no-menu`) + interactive menu |
| `config.py` | Root `config.json`: service settings, auth settings, api key → credential file map (`resolve_account` / `register_account`) |
| `config.example.json` | Shipped template, copied to `config.json` by the first explicit login |
| `server.py` | HTTP server: Cline passthrough, `/v1/models`, `/v1/chat/completions`, `/v1/messages`; selects the account per request |
| `upstream.py` | Python OpenSSL upstream client (Cloud Armor bypass, injects the selected account's token with `workos:` prefix) |
| `auth.py` | Per-account OAuth lifecycle: device flow, register, refresh, credential files (`load_tokens`/`save_tokens`/`get_valid_token`/`install_account`/`login`) |
| `anthropic.py` | Anthropic Messages API ↔ OpenAI chat completions translation |
| `ratelimit.py` | Per-IP token-bucket rate limiter |
| `desensitize.py` | Content moderation trigger rewriting |
| `reqlog.py` | Request logging with credential redaction |
| `banner.py` | Startup banner |
| `tests/selfcheck.py` | Offline self-check (no network, no real config) |
| `apikey/` | One credential file per logged-in account — created on first login, git-ignored |
| `auth_flow.py`, `start_cline.bat` | Legacy one-off scripts, see [Legacy scripts](#legacy-scripts) |

## Setup

```bash
# 1. Root config (--login creates it from config.example.json if it is missing)
python main.py --login

# 2. Start the proxy (reads config.json; interactive menu by default)
python main.py
python main.py --no-menu
```

`python main.py --login` prints a device code, waits for you to confirm in the browser, then writes:

* the account's credential file → `apikey/<account>.json`
* its API key → the `api_keys` map in `config.json`

Run it again to add another account; logging the **same** account in again reuses the same file and
the same key.

## Config

`config.json` lives next to `config.py` (a source checkout: the repository root; a PyInstaller
one-file build: the directory holding the executable). `config.example.json` is the account-free
template:

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

* `api_keys` maps each generated key to that account's credential file, e.g.
  `"api_keys": { "6f1c…": "apikey/user_01ABC.json" }`. Relative paths resolve against the directory
  holding `config.json`; absolute paths are used as-is.
* Hand-edit either side (the key, or the file it points at) and restart — the new mapping is in
  effect, no code change needed.
* `rate_limit` takes `"500ms"` / `"2s"` / `"3m"` string form; `log_path`, `rate_limit` and
  `desensitize` are off unless configured.
* There is **no environment-variable override** and no `providers.json` mirroring: `config.json` is
  the single source of truth, and CLI flags only override it when passed explicitly.
* A missing `config.json` is a startup error that tells you to copy the template or run
  `--login`; the proxy never falls back to a default account.

## Routing and error semantics

Every path except `/healthz` requires a registered key:

| Presented credential | Result |
|---|---|
| `Authorization: Bearer <key>` | routed to that key's account file |
| `x-api-key: <key>` | used only when no `Authorization: Bearer` header is present |
| missing, or a key absent from `api_keys` | `401` `{"error":{"message":"invalid API key","type":"authentication_error"}}` |
| key is registered but its file cannot yield a live token | `502` `{"error":{"message":"account auth failed: …","type":"account_auth_error"}}` |
| upstream answers `401`/`403` | passed through unchanged |

There is no "first account" default: an unregistered or absent key is always rejected, and the
client's own token is never forwarded upstream (the proxy replaces `Authorization` with the selected
account's token).

## Clients

All endpoints take the proxied key in `Authorization: Bearer <key>` (or `x-api-key: <key>`).

### OpenAI-compatible

* Base URL: `http://127.0.0.1:61022/v1`
* API key: any key from `config.json`'s `api_keys`
* Endpoints: `/v1/models`, `/v1/chat/completions` (`"stream": true` is relayed chunk by chunk, see
  [Streaming](#streaming))

```bash
curl http://127.0.0.1:61022/v1/models -H "Authorization: Bearer <key>"
curl http://127.0.0.1:61022/v1/chat/completions \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"model":"~openai/gpt-luna-latest","messages":[{"role":"user","content":"Hello"}],"max_tokens":200}'
```

### Anthropic-compatible (Claude Code and friends)

* Base URL: `http://127.0.0.1:61022` (Claude Code: `ANTHROPIC_BASE_URL`)
* API key: any key from `api_keys` (`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`)
* Endpoint: `/v1/messages`. The proxy always requests upstream SSE: with `"stream": true`, it
  converts events as they arrive; with `"stream": false` (or omitted), it aggregates text and
  tool calls into one Anthropic JSON message. See [Streaming](#streaming).

### Cline Desktop app

`baseUrl` alone is **no longer enough**: the proxy rejects every request that does not carry a
registered key with `401`, and the desktop app's own account token is not a registered key. To use
the desktop app through the proxy it must be able to send one of the keys from `config.json` — i.e.
point it at an OpenAI-compatible provider entry with `baseUrl` `http://127.0.0.1:61022/v1` and
`apiKey` set to a registered key. Configurations that can only set `baseUrl` (for example the
built-in `cline` provider, which authenticates with its own WorkOS token) cannot be used with this
proxy in multi-account mode.

## Streaming

`"stream": true` responses — `/v1/chat/completions`, `/v1/messages`, and `/api/v1/*` passthrough — are
relayed incrementally: every chunk the upstream sends is written to the client and flushed as soon as
it arrives, instead of being read to completion first. The stream is not buffered anywhere in the
proxy, so time-to-first-token through the proxy tracks the upstream's own time-to-first-token.

Limits worth knowing:

* The wait while a reasoning model thinks is upstream inference time. The proxy no longer adds its own
  delay on top of it, but it cannot make the model answer earlier.
* A streamed response has no known length when the headers go out, so it is delimited by
  `Connection: close` and carries no `Content-Length`. Clients must accept an EOF-terminated body
  (curl, the OpenAI/Anthropic SDKs, Claude Code and Cline Desktop all do).
* The `Connection: close` relay only takes effect in a newly started proxy: restart the running
  service (`Ctrl-C`, `python main.py --no-menu`) before expecting incremental output.

## Free-tier billing

Cline bills by model ID: `deepseek/deepseek-v4.1-flash` is metered, `cline-free/deepseek-v4.1-flash`
is not. The proxy rewrites paid IDs to their free twins automatically (cached list from
`/api/v1/ai/cline/recommended-models`) and exposes the free IDs in `/v1/models` so
OpenAI-compatible clients can discover them.

## Auth

Cline uses **WorkOS AuthKit** device flow (`client_id` from `config.json`):

1. `python main.py --login` requests a device code from `auth.workos_base_url`
2. You confirm at the printed verification URL
3. The WorkOS tokens are registered with the Cline API (`/api/v1/auth/register`)
4. The credential file is written to `apikey/<jwt sub>.json` and its API key is registered in
   `config.json`
5. Each account's token is refreshed in place, under that file's own lock, before expiry
   (`auth.refresh_buffer_ms`); a rotated refresh token is written back to the same file even when
   the follow-up registration fails, and nothing is shared between accounts

## Port

Default `61022`, from `config.json`. `--port` / `--bind` override it for one run.

## Interactive menu

`python main.py` (no `--no-menu`):

```
  1. Status            pick an account, then show its file, user ID, token and expiry
  2. Start server      start serving, blocking until Ctrl-C
  3. List models       pick an account, list the first 30 models (sends its key)
  4. Test chat         pick an account, send one message (sends its key)
  5. Login / add account
  6. Quit
```

Options 1/3/4 always ask which registered key to use — there is no implicit first account.

## Testing

```bash
python tests/selfcheck.py   # offline self-check (multi-account mapping / refresh isolation)
```

The self-check covers the Anthropic↔OpenAI translation, redaction, desensitize, root-config parsing
(template, overrides, relative-path resolution, hand-edit reload, key registration), credential-file
loading (both key spellings), refresh/registration on a chosen file with mocked network calls,
`login()` with a mocked device flow, HTTP-layer key selection / `401` / `502`, and the streaming
path (live upstream body, per-chunk relay without `Content-Length`, incremental Anthropic event
conversion across arbitrary UTF-8/frame boundaries). It never touches
the network and never reads your real `config.json` or credentials (fixtures are built under
`temp/` and removed on exit).

## Legacy scripts

`auth_flow.py` and `start_cline.bat` predate multi-account routing; neither is imported by the proxy
and neither is packaged into the release binary (`release.yml` builds `main.py`). They still refer to
single-account storage and to the author's machine paths, so they do not work with this
configuration — treat them as historical, and do not use them to start the proxy or log in.

## Why not Go?

Go's `crypto/tls` produces a ClientHello that Cloud Armor blocks.
`refraction-networking/utls` bypasses JA3 but forces HTTP/2 (ALPN=h2 from the Chrome profile), and Go's HTTP/1.1 transport can't read h2 frames. Python is the correct TLS stack for this upstream.
