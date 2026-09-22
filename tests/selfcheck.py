"""Self-check for ClineDesktop2API. Run: python tests/selfcheck.py

Offline by construction: every network seam (``auth._post``, ``server.do_request``,
``server.get_valid_token``, ``upstream.urllib.request.urlopen``) is stubbed, so no request
leaves the machine, and the
developer's real config.json / credential files are never read — fixtures are built
under the repository ``temp/`` directory and removed on exit.
"""
import atexit
import base64
import email
import io
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEMP = ROOT / "temp"
TEMP.mkdir(exist_ok=True)
WORK = Path(tempfile.mkdtemp(dir=str(TEMP), prefix="selfcheck-"))


def _cleanup():
    shutil.rmtree(WORK, ignore_errors=True)
    try:
        TEMP.rmdir()
    except OSError:
        pass


atexit.register(_cleanup)

failures = []


def check(name, cond, detail=""):
    if cond: print("PASS", name)
    else:
        print("FAIL", name, "-", detail); failures.append(name)


def sse_line(obj):
    return "data: " + json.dumps(obj) + "\n\n"


# --- anthropic -> openai ---
from anthropic import anthropic_to_openai, anthropic_response, anthropic_stream_response

oai = anthropic_to_openai({
    "model":"claude-sonnet","max_tokens":100,
    "system":"You are helpful",
    "messages":[{"role":"user","content":"hi"},{"role":"user","content":[{"type":"text","text":"block"}]}],
    "tools":[{"name":"get_weather","description":"w","input_schema":{"type":"object"}}],
})
check("system mapped", oai["messages"][0]=={"role":"system","content":"You are helpful"})
check("string content", any(m.get("content")=="hi" for m in oai["messages"]))
check("block content", any(m.get("content")=="block" for m in oai["messages"]))
check("model+stream", oai["model"]=="claude-sonnet" and oai["stream"] is True)
check("tools mapped", oai["tools"][0]["function"]["name"]=="get_weather")

# --- openai SSE -> anthropic response ---
sse = ""
sse += sse_line({"id":"cmpl-1","choices":[{"index":0,"delta":{"content":"Hi"}}]})
sse += sse_line({"choices":[{"index":0,"delta":{"tool_calls":[{"id":"c1","function":{"name":"f","arguments":"{\"x\":1}"}}]},"finish_reason":None}]})
sse += sse_line({"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]})
sse += "data: [DONE]\n\n"
resp = anthropic_response(sse.encode(), "m")
check("anthropic text", any(c.get("type")=="text" and c["text"]=="Hi" for c in resp["content"]))
tool = [c for c in resp["content"] if c.get("type")=="tool_use"]
check("anthropic tool", len(tool)==1 and tool[0]["name"]=="f", str(tool))
check("tool input json", len(tool)==1 and tool[0]["input"]=={"x":1})
check("stop_reason", resp["stop_reason"]=="tool_use")

# --- redaction ---
from reqlog import redact
r = redact('Bearer abcdefghijklmnop accessToken:"xyz12345xyz" api_key=12345678ab')
check("redact bearer", "abcdefghijklmnop" not in r, r)
check("redact accessToken", "xyz12345xyz" not in r, r)
check("redact api_key", "12345678ab" not in r, r)

# --- desensitize ---
from desensitize import desensitize_text
d = desensitize_text("You are an AI agent, use function calling to bypass")
check("desensitize", "helpful assistant" in d and "tool use" in d and "work around" in d, d)

# ======================================================================
# root config.json: template, overrides, key -> account map
# ======================================================================
from config import initialize_config, load_config

TEMPLATE = ROOT / "config.example.json"

def new_config_dir(name, with_config=True):
    d = WORK / name
    d.mkdir(parents=True)
    shutil.copy(TEMPLATE, d / "config.example.json")
    return d

def write_mapping(path, mapping):
    data = json.loads(path.read_text(encoding="utf-8"))
    data["api_keys"] = mapping
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

cdir = new_config_dir("cfg")
cpath = cdir / "config.json"

check("initialize_config copies the template",
      initialize_config(cpath) == cpath and cpath.exists(), str(cpath))
check("initialize_config keeps an existing config",
      initialize_config(cpath) == cpath and cpath.read_text() == (cdir / "config.example.json").read_text())

cfg = load_config(path=cpath)
check("config defaults from template",
      cfg.port == 61022 and cfg.bind == "127.0.0.1" and cfg.api_keys == {} and cfg.rate_limit is None,
      f"port={cfg.port} keys={cfg.api_keys}")
check("config carries the auth settings",
      set(cfg.auth) == {"workos_base_url", "cline_base_url", "client_id", "user_agent",
                        "refresh_buffer_ms", "request_timeout_seconds"}, str(sorted(cfg.auth)))
check("config root is its own directory", cfg.root == cdir and cfg.path == cpath,
      f"root={cfg.root}")

try:
    load_config(path=WORK / "absent" / "config.json")
    check("missing config raises FileNotFoundError", False, "no error raised")
except FileNotFoundError:
    check("missing config raises FileNotFoundError", True)

bdir = new_config_dir("cfg-incomplete")
bad = json.loads((bdir / "config.example.json").read_text(encoding="utf-8"))
del bad["auth"]
(bdir / "config.json").write_text(json.dumps(bad), encoding="utf-8")
try:
    load_config(path=bdir / "config.json")
    check("config missing a field raises ValueError", False, "no error raised")
except ValueError:
    check("config missing a field raises ValueError", True)

args = type("A", (), {"port": 9999, "bind": None, "log": None, "rate_limit": "2s", "desensitize": None})()
ov = load_config(args, path=cpath)
check("CLI flag overrides config", ov.port == 9999, str(ov.port))
check("absent CLI flag keeps config value", ov.bind == "127.0.0.1" and ov.desensitize is False)
check("rate limit parsed", ov.rate_limit == 2.0, str(ov.rate_limit))

write_mapping(cpath, {"keyA": "apikey/a.json", "keyB": "apikey/b.json",
                      "keyC": str(WORK / "outside" / "c.json")})
mcfg = load_config(path=cpath)
rel = (cdir / "apikey" / "a.json").resolve()
check("relative oauth path resolves against the config dir",
      mcfg.resolve_account("keyA") == rel and mcfg.resolve_account("keyA").is_absolute(),
      str(mcfg.resolve_account("keyA")))
check("absolute oauth path is kept",
      mcfg.resolve_account("keyC") == (WORK / "outside" / "c.json").resolve(),
      str(mcfg.resolve_account("keyC")))
check("unregistered key resolves to nothing",
      mcfg.resolve_account("nope") is None and mcfg.resolve_account("") is None
      and mcfg.resolve_account(None) is None)

write_mapping(cpath, {"keyA": "apikey/renamed.json"})
reloaded = load_config(path=cpath)
check("hand-edited mapping takes effect on reload",
      reloaded.resolve_account("keyA") == (cdir / "apikey" / "renamed.json").resolve()
      and reloaded.resolve_account("keyB") is None)

p1 = (cdir / "apikey" / "reg1.json").resolve()
k1 = reloaded.register_account(p1)
check("register_account reuses the key of a known file",
      k1 == reloaded.register_account(p1) and isinstance(k1, str) and len(k1) >= 32, str(k1))
p2 = (cdir / "apikey" / "reg2.json").resolve()
k2 = reloaded.register_account(p2)
check("register_account mints a distinct key", k2 != k1)
check("register_account updates the in-memory map",
      reloaded.resolve_account(k1) == p1 and reloaded.resolve_account(k2) == p2)
persisted = load_config(path=cpath)
check("registered keys are persisted and nothing else is lost",
      persisted.resolve_account(k1) == p1 and persisted.resolve_account(k2) == p2
      and persisted.resolve_account("keyA") == (cdir / "apikey" / "renamed.json").resolve())

# ======================================================================
# auth.py: per-account credential files, refresh isolation
# ======================================================================
import auth
from auth import _expiry_ms, get_valid_token, install_account, load_tokens, save_tokens


def b64url(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def make_jwt(sub, exp_ms):
    payload = {"sub": sub, "exp": int(exp_ms // 1000)}
    return f"{b64url(json.dumps({'alg': 'none', 'typ': 'JWT'}).encode())}." \
           f"{b64url(json.dumps(payload).encode())}.sig"


check("expiry ms int", _expiry_ms(1789897831000)==1789897831000)
check("expiry ms iso", _expiry_ms("2026-09-20T10:01:18Z")>1700000000000)
check("expiry ms empty", _expiry_ms("")==0 and _expiry_ms(None)==0)

adir = WORK / "auth"
adir.mkdir()
now = int(time.time() * 1000)

pcanon = adir / "canonical.json"
save_tokens({"access": "acc1", "refresh": "ref1", "expiresAt": now + 3600000, "accountId": "u1"}, pcanon)
check("access/refresh keys round-trip",
      load_tokens(pcanon) == {"access": "acc1", "refresh": "ref1", "expiresAt": now + 3600000,
                              "accountId": "u1", "registration_pending": False},
      str(load_tokens(pcanon)))

pdesk = adir / "desktop.json"
pdesk.write_text(json.dumps({"accessToken": "workos:acc2", "refreshToken": "ref2",
                             "expiresAt": now + 1000, "accountId": ""}), encoding="utf-8")
desk = load_tokens(pdesk)
check("accessToken/refreshToken keys accepted",
      desk["access"] == "acc2" and desk["refresh"] == "ref2", str(desk))
check("workos: prefix stripped", not desk["access"].startswith("workos:"))
check("missing credential file is None", load_tokens(adir / "absent.json") is None)

ppend = adir / "pending.json"
save_tokens({"access": "acc3", "refresh": "ref3", "expiresAt": 0, "accountId": "",
             "registration_pending": True}, ppend)
check("pending flag round-trips",
      load_tokens(ppend)["registration_pending"] is True
      and "registration_pending" in json.loads(ppend.read_text(encoding="utf-8")))
check("jwt claims helper", auth._jwt_claims(make_jwt("sub-1", now))["sub"] == "sub-1")

acfg = load_config(path=cpath)
sub = "user_01TESTSELFSUB"
synthetic = {"access": make_jwt(sub, now + 3600000), "refresh": "refZ",
             "expiresAt": now + 3600000, "accountId": ""}
key, apath = install_account(synthetic, acfg)
check("install_account names the file after the jwt sub",
      apath == (cdir / "apikey" / f"{sub}.json").resolve() and apath.exists(), str(apath))
check("install_account stores readable tokens", load_tokens(apath)["refresh"] == "refZ")
check("install_account registers the api key",
      acfg.resolve_account(key) == apath and len(key) >= 32)
check("install_account persists the mapping",
      load_config(path=cpath).resolve_account(key) == apath)

key_again, apath_again = install_account(synthetic, acfg)
check("logging the same account in again reuses key and file",
      key_again == key and apath_again == apath)
key2, apath2 = install_account({"access": make_jwt("user_01TESTOTHER", now + 3600000),
                                "refresh": "refY", "expiresAt": now + 3600000, "accountId": ""}, acfg)
check("a second account gets its own key and file",
      key2 != key and apath2 != apath and acfg.resolve_account(key2) == apath2
      and acfg.resolve_account(key) == apath)

try:
    install_account({"access": "not-a-jwt", "refresh": "r", "expiresAt": 0, "accountId": ""}, acfg)
    check("install_account rejects a token without sub", False, "no error raised")
except RuntimeError:
    check("install_account rejects a token without sub", True)

settings = load_config(path=cpath).auth

# login(): device flow -> register -> install, with the browser step stubbed out.
login_sub = "user_01TESTLOGIN"
login_access = make_jwt(login_sub, now + 3600000)
device_calls = []


def post_login(url, payload, settings, form=False):
    device_calls.append((url, dict(payload)))
    if url.endswith("/user_management/authorize/device"):
        return 200, {"device_code": "dev-1", "user_code": "AAAA-BBBB",
                     "verification_uri": "https://example.invalid/device",
                     "interval": 0, "expires_in": 1}
    if url.endswith("/user_management/authenticate"):
        return 200, {"access_token": login_access, "refresh_token": "login-ref", "expires_in": 3600}
    if url.endswith("/api/v1/auth/register"):
        return 200, {"data": {"accessToken": login_access, "refreshToken": "login-ref",
                              "expiresAt": now + 3600000, "userInfo": {"id": "acct-login"}}}
    raise AssertionError(f"unexpected url {url}")


with mock.patch.object(auth, "_post", side_effect=post_login):
    lkey, lpath = auth.login(acfg)
with mock.patch.object(auth, "_post", side_effect=post_login):
    lkey_again, lpath_again = auth.login(acfg)
check("login stores the account under apikey/",
      lpath == (cdir / "apikey" / f"{login_sub}.json").resolve() and load_tokens(lpath)["access"] == login_access,
      str(lpath))
check("login registers the api key", acfg.resolve_account(lkey) == lpath and len(lkey) >= 32)
check("logging the same account in again reuses key and file",
      lkey_again == lkey and lpath_again == lpath)
check("login exchanges the device code for a token",
      any(u.endswith("/user_management/authorize/device") for u, _ in device_calls)
      and any(p.get("grant_type") == "urn:ietf:params:oauth:grant-type:device_code" for _, p in device_calls),
      str(device_calls))

pa = adir / "acctA.json"
pb = adir / "acctB.json"
pa.write_text(json.dumps({"access": make_jwt("subA", now + 1000), "refresh": "refA1",
                          "expiresAt": now + 1000, "accountId": "acctA"}), encoding="utf-8")
pb.write_text(json.dumps({"access": make_jwt("subB", now + 1000), "refresh": "refB1",
                          "expiresAt": now + 1000, "accountId": "acctB"}), encoding="utf-8")
access_a2 = make_jwt("subA", now + 7200000)
rlog = []


def post_ok(url, payload, settings, form=False):
    rlog.append(url)
    if url.endswith("/user_management/authenticate"):
        return 200, {"access_token": make_jwt("subA", now + 3600000),
                     "refresh_token": "refA2", "expires_in": 3600}
    if url.endswith("/api/v1/auth/register"):
        return 200, {"data": {"accessToken": access_a2, "refreshToken": "refA3",
                              "expiresAt": now + 7200000, "userInfo": {"id": "acctA2"}}}
    raise AssertionError(f"unexpected url {url}")


with mock.patch.object(auth, "_post", side_effect=post_ok):
    token_a = get_valid_token(pa, settings)
ta = load_tokens(pa)
check("refresh runs against the requested account file",
      rlog == [f"{settings['workos_base_url']}/user_management/authenticate",
               f"{settings['cline_base_url']}/api/v1/auth/register"], str(rlog))
check("refresh returns the registered access token", token_a == access_a2 and ta["access"] == access_a2)
check("rotated refresh token is saved, pending cleared",
      ta["refresh"] == "refA3" and ta["registration_pending"] is False and ta["accountId"] == "acctA2",
      str(ta))
check("refreshing one account leaves the other file untouched",
      load_tokens(pb)["refresh"] == "refB1" and load_tokens(pb)["access"] == make_jwt("subB", now + 1000),
      str(load_tokens(pb)))

with mock.patch.object(auth, "_post", side_effect=RuntimeError("workos down")):
    try:
        get_valid_token(pb, settings)
        check("failing account raises instead of serving a stale token", False, "no error raised")
    except RuntimeError:
        check("failing account raises instead of serving a stale token", True)
check("failing refresh leaves the failed file as it was", load_tokens(pb)["refresh"] == "refB1")
check("failing refresh does not disturb the healthy file", load_tokens(pa)["refresh"] == "refA3")

pc = adir / "acctC.json"
pc.write_text(json.dumps({"access": make_jwt("subC", now + 1000), "refresh": "refC1",
                          "expiresAt": now + 1000, "accountId": ""}), encoding="utf-8")
rotated_c = make_jwt("subC", now + 3600000)


def post_register_fails(url, payload, settings, form=False):
    if url.endswith("/user_management/authenticate"):
        return 200, {"access_token": rotated_c, "refresh_token": "refC2", "expires_in": 3600}
    raise RuntimeError("cline register down")


with mock.patch.object(auth, "_post", side_effect=post_register_fails):
    try:
        get_valid_token(pc, settings)
        check("register failure propagates", False, "no error raised")
    except RuntimeError:
        check("register failure propagates", True)
tc = load_tokens(pc)
check("rotated refresh survives a failed register",
      tc["refresh"] == "refC2" and tc["access"] == rotated_c and tc["registration_pending"] is True,
      str(tc))

pd = adir / "acctD.json"
pd.write_text(json.dumps({"access": make_jwt("subD", now + 3600000), "refresh": "refD1",
                          "expiresAt": now + 3600000, "accountId": "",
                          "registration_pending": True}), encoding="utf-8")
dlog = []
plog = []
access_d2 = make_jwt("subD", now + 7200000)


def post_register_only(url, payload, settings, form=False):
    dlog.append(url)
    plog.append(payload)
    if url.endswith("/api/v1/auth/register"):
        return 200, {"data": {"accessToken": access_d2, "refreshToken": "refD2",
                              "expiresAt": now + 7200000, "userInfo": {"id": "acctD"}}}
    raise AssertionError("a pending registration must not be refreshed first")


with mock.patch.object(auth, "_post", side_effect=post_register_only):
    token_d = get_valid_token(pd, settings)
check("pending registration is completed without another refresh",
      dlog == [f"{settings['cline_base_url']}/api/v1/auth/register"], str(dlog))
check("completed registration clears the pending flag",
      token_d == access_d2 and load_tokens(pd)["registration_pending"] is False
      and load_tokens(pd)["accountId"] == "acctD", str(load_tokens(pd)))
check("register sends the whole token pair",
      plog and set(plog[0]) == {"accessToken", "refreshToken"}
      and plog[0]["refreshToken"] == "refD1", str(plog))

try:
    get_valid_token(adir / "absent.json", settings)
    check("missing credential file raises", False, "no error raised")
except RuntimeError:
    check("missing credential file raises", True)

pe = adir / "acctE.json"
pe.write_text(json.dumps({"access": "accE", "refresh": "", "expiresAt": now + 1000,
                          "accountId": ""}), encoding="utf-8")
try:
    get_valid_token(pe, settings)
    check("missing refresh token raises", False, "no error raised")
except RuntimeError:
    check("missing refresh token raises", True)

# ======================================================================
# server.py: key -> account selection, 401 / 502 semantics
# ======================================================================
import server


def fake_handler(cfg, path, headers):
    h = object.__new__(server.Handler)
    h.cfg = cfg
    h.limiter = None
    h.path = path
    h.command = "GET"
    h.request_version = "HTTP/1.1"
    h.requestline = f"GET {path} HTTP/1.1"
    h.headers = email.message_from_string(
        "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n")
    h.rfile = io.BytesIO(b"")
    h.wfile = io.BytesIO()
    h._headers_buffer = []
    return h


def status_of(h):
    return int(h.wfile.getvalue().split(b"\r\n", 1)[0].split(b" ")[1])


def body_of(h):
    raw = h.wfile.getvalue()
    _, _, body = raw.partition(b"\r\n\r\n")
    return json.loads(body.decode())


check("bearer header selects its account",
      fake_handler(acfg, "/v1/models", {"Authorization": f"Bearer {key}"})._resolve_account() == apath)
check("x-api-key header selects its account",
      fake_handler(acfg, "/v1/models", {"x-api-key": key2})._resolve_account() == apath2)
check("bearer wins and never falls back to x-api-key",
      fake_handler(acfg, "/v1/models",
                   {"Authorization": "Bearer unknown", "x-api-key": key})._resolve_account() is None)
check("no credential selects nothing",
      fake_handler(acfg, "/v1/models", {})._resolve_account() is None)

used = []
with mock.patch.object(server, "get_valid_token", side_effect=lambda *a, **k: used.append(a) or "tok"):
    h = fake_handler(acfg, "/v1/models", {"Authorization": "Bearer nope"})
    h._dispatch("GET")
    check("unknown key -> 401 authentication_error",
          status_of(h) == 401 and body_of(h)["error"]["type"] == "authentication_error", str(h.wfile.getvalue()))
    check("rejected request never touches an account file", used == [], str(used))
    h = fake_handler(acfg, "/api/v1/user", {})
    h._dispatch("GET")
    check("anonymous passthrough -> 401", status_of(h) == 401)

with mock.patch.object(server, "get_valid_token", side_effect=RuntimeError("boom")):
    h = fake_handler(acfg, "/v1/models", {"Authorization": f"Bearer {key}"})
    h._dispatch("GET")
    check("account auth failure -> 502 account_auth_error",
          status_of(h) == 502 and body_of(h)["error"]["type"] == "account_auth_error",
          str(h.wfile.getvalue()))


def route(cfg, path, headers):
    seen = {}

    def fake_token(path_arg, settings, force=False):
        seen["path"] = path_arg
        seen["settings"] = settings
        return f"tok-{Path(path_arg).stem}"

    def fake_request(method, upath, body=None, headers=None, timeout=None, proxy_token=None):
        seen["token"] = proxy_token
        seen["upstream"] = upath
        return 200, {}, io.BytesIO(b"{}")

    with mock.patch.object(server, "get_valid_token", side_effect=fake_token), \
         mock.patch.object(server, "do_request", side_effect=fake_request):
        h = fake_handler(cfg, path, headers)
        h._dispatch("GET")
    return seen


first = route(acfg, "/api/v1/models", {"Authorization": f"Bearer {key}"})
second = route(acfg, "/api/v1/models", {"x-api-key": key2})
check("each key routes to its own credential file",
      first["path"] == apath and second["path"] == apath2, f"{first.get('path')} / {second.get('path')}")
check("each request carries its own account token",
      first["token"] != second["token"] and first["token"] == f"tok-{apath.stem}",
      f"{first.get('token')} / {second.get('token')}")
check("account settings come from the config", first["settings"] == acfg.auth)

# ======================================================================
# streaming: do_request hands back a live body, the server relays it by chunk
# ======================================================================
import urllib.error
import upstream


class LiveBody:
    """Live upstream body: read/read1/close plus a context manager, nothing else."""

    def __init__(self, chunks, log=None, fail_at=None):
        self.chunks = [bytes(c) for c in chunks]
        self.log = log
        self.fail_at = fail_at
        self.sent = 0
        self.reads = 0
        self.closed = False

    def read1(self, n=-1):
        if self.log is not None:
            self.log.append(("read1", self.sent))
        if self.fail_at == self.sent:
            raise OSError("upstream stream broke")
        if self.sent >= len(self.chunks):
            return b""
        chunk = self.chunks[self.sent]
        self.sent += 1
        return chunk

    def read(self, n=-1):
        self.reads += 1
        rest = b"".join(self.chunks[self.sent:])
        self.sent = len(self.chunks)
        return rest

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class TracingWfile(io.BytesIO):
    """Client socket stand-in: records the write/flush order, can hang up."""

    def __init__(self, log=None, fail_after=None):
        super().__init__()
        self.log = log
        self.fail_after = fail_after
        self.writes = 0

    def write(self, data):
        self.writes += 1
        if self.fail_after is not None and self.writes > self.fail_after:
            raise BrokenPipeError("client hung up")
        if self.log is not None:
            self.log.append(("write", bytes(data)))
        return super().write(data)

    def flush(self):
        if self.log is not None:
            self.log.append(("flush",))


def post_handler(cfg, path, headers, payload, log=None, fail_after=None):
    h = fake_handler(cfg, path, headers)
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    h.command = "POST"
    h.requestline = f"POST {path} HTTP/1.1"
    h.rfile = io.BytesIO(body)
    h.headers["Content-Length"] = str(len(body))
    h.wfile = TracingWfile(log, fail_after=fail_after)
    return h


def raw_of(h):
    return h.wfile.getvalue()


def split_response(h):
    head, sep, body = raw_of(h).partition(b"\r\n\r\n")
    return head, body if sep else b""


def relay_gaps(log, chunks):
    """Per chunk: not written, not flushed, or the next read1 came before the flush."""
    bad = []
    for n, chunk in enumerate(chunks):
        written = [i for i, e in enumerate(log) if e == ("write", chunk)]
        if not written:
            bad.append((n, "not written"))
            continue
        flushed = [i for i, e in enumerate(log) if e[0] == "flush" and i > written[0]]
        reads = [i for i, e in enumerate(log) if e[0] == "read1" and i > written[0]]
        if not flushed:
            bad.append((n, "not flushed"))
        elif reads and reads[0] < flushed[0]:
            bad.append((n, "next read1 before flush"))
    return bad


RAW_CHUNKS = [
    sse_line({"id": "cmpl-1", "choices": [{"index": 0, "delta": {"content": "一"}}]}).encode(),
    sse_line({"choices": [{"index": 0, "delta": {"content": "二"}}]}).encode(),
    sse_line({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}).encode(),
    b"data: [DONE]\n\n",
]

# --- upstream.do_request is the only seam that opens the upstream body ---

captured = {}
live = LiveBody([b"data: {}\n\n"])
live.status = 200
live.headers = {"Content-Type": "text/event-stream"}


def urlopen_ok(req, timeout=None, context=None):
    captured["req"] = req
    captured["context"] = context
    return live


with mock.patch.object(upstream.urllib.request, "urlopen", side_effect=urlopen_ok):
    status, headers, resp = upstream.do_request("POST", "/api/v1/chat/completions",
                                                b'{"model":"~openai/gpt-luna-latest"}',
                                                {"x-api-key": "leak", "Accept-Encoding": "identity", "Content-Length": "999999"},
                                                proxy_token="tok-1")
check("do_request hands back the live body without reading it",
      status == 200 and resp is live and live.reads == 0 and live.sent == 0,
      f"status={status} reads={live.reads} sent={live.sent}")
fwd = {k.lower(): v for k, v in captured["req"].header_items()}
check("do_request signs the request with the account token",
      fwd.get("authorization") == "Bearer workos:tok-1", str(fwd.get("authorization")))
check("do_request keeps the desktop headers and drops the client credential",
      fwd.get("x-client-type") == "cline-desktop" and "x-api-key" not in fwd, str(sorted(fwd)))
check("rewritten requests do not forward a stale Content-Length",
      "content-length" not in fwd, str(fwd.get("content-length")))
check("do_request keeps the upstream base, path and TLS context",
      captured["req"].full_url == "https://api.cline.bot/api/v1/chat/completions"
      and captured["context"] is upstream.CTX, captured["req"].full_url)
check("do_request returns headers for stream detection",
      headers.get("Content-Type") == "text/event-stream", str(headers))
with resp:
    pass
check("the caller's with-block closes the live upstream body", live.closed)

err_fp = LiveBody([b'{"error":"upstream says no"}'])
err = urllib.error.HTTPError("https://api.cline.bot/api/v1/chat/completions", 401, "Unauthorized",
                            email.message_from_string("Content-Type: application/json\r\n"), err_fp)
with mock.patch.object(upstream.urllib.request, "urlopen", side_effect=err):
    status, headers, resp = upstream.do_request("POST", "/api/v1/chat/completions", b"{}", {})
check("an upstream HTTPError is returned as the readable body, still unread",
      status == 401 and resp is err and err_fp.reads == 0, f"status={status} reads={err_fp.reads}")
with resp:
    err_body = resp.read()
check("the error body reads through verbatim and the caller closes it",
      err_body == b'{"error":"upstream says no"}' and err_fp.closed, str(err_body))

with mock.patch.object(upstream.urllib.request, "urlopen", side_effect=OSError("connect refused")):
    status, headers, resp = upstream.do_request("POST", "/api/v1/chat/completions", b"{}", {})
with resp:
    dead_body = resp.read()
check("a connection failure is still a 502 proxy_error body",
      status == 502 and isinstance(resp, io.BytesIO)
      and json.loads(dead_body)["error"]["type"] == "proxy_error",
      f"status={status} type={type(resp).__name__}")

# --- handler level: streamed upstream body -> incremental relay ---

log = []
live = LiveBody(RAW_CHUNKS, log=log)
with mock.patch.object(server, "get_valid_token", return_value="tok"), \
     mock.patch.object(server, "do_request",
                       return_value=(200, {"Content-Type": "text/event-stream"}, live)):
    h = post_handler(acfg, "/v1/chat/completions", {"Authorization": f"Bearer {key}"},
                     {"model": "~openai/gpt-luna-latest", "stream": True,
                      "messages": [{"role": "user", "content": "hi"}]}, log=log)
    h._dispatch("POST")
head, body = split_response(h)
check("streamed chat relays the upstream SSE bytes verbatim",
      body == b"".join(RAW_CHUNKS), str(body[:120]))
check("streamed chat sends no Content-Length or Transfer-Encoding, delimits with Connection: close",
      b"content-length" not in head.lower() and b"transfer-encoding" not in head.lower()
      and b"connection: close" in head.lower(), str(head))
check("streamed chat asks the server to close the connection",
      getattr(h, "close_connection", False) is True)
check("streamed chat relays through read1 only, never a whole-body read",
      live.reads == 0 and live.sent == len(RAW_CHUNKS) and live.closed,
      f"whole_reads={live.reads} sent={live.sent} closed={live.closed}")
check("every relayed chunk is written and flushed before the next upstream read",
      relay_gaps(log, RAW_CHUNKS) == [], str(relay_gaps(log, RAW_CHUNKS)))

log = []
live = LiveBody(RAW_CHUNKS, log=log)
with mock.patch.object(server, "get_valid_token", return_value="tok"), \
     mock.patch.object(server, "do_request",
                       return_value=(200, {"Content-Type": "text/event-stream"}, live)):
    h = post_handler(acfg, "/api/v1/user/stream", {"Authorization": f"Bearer {key}"},
                     {"hello": "world"}, log=log)
    h._dispatch("POST")
head, body = split_response(h)
check("passthrough relays an upstream SSE body incrementally too",
      body == b"".join(RAW_CHUNKS) and b"content-length" not in head.lower()
      and live.closed, str(head))

log = []
live = LiveBody(RAW_CHUNKS, log=log)
with mock.patch.object(server, "get_valid_token", return_value="tok"), \
     mock.patch.object(server, "do_request",
                       return_value=(200, {"Content-Type": "text/event-stream"}, live)):
    h = post_handler(acfg, "/v1/chat/completions", {"Authorization": f"Bearer {key}"},
                     {"model": "m", "stream": True, "messages": []}, log=log, fail_after=2)
    try:
        h._dispatch("POST")
    except (BrokenPipeError, ConnectionResetError):
        pass
head, body = split_response(h)
check("a client that hangs up mid-stream stops the relay and closes upstream",
      live.closed and live.sent < len(RAW_CHUNKS) and b"proxy_error" not in body,
      f"closed={live.closed} sent={live.sent}")
check("a hung-up client never gets a second HTTP status",
      raw_of(h).count(b"HTTP/1.1 ") == 1, str(raw_of(h)[:80]))

log = []
live = LiveBody(RAW_CHUNKS, log=log, fail_at=1)
with mock.patch.object(server, "get_valid_token", return_value="tok"), \
     mock.patch.object(server, "do_request",
                       return_value=(200, {"Content-Type": "text/event-stream"}, live)):
    h = post_handler(acfg, "/v1/chat/completions", {"Authorization": f"Bearer {key}"},
                     {"model": "m", "stream": True, "messages": []}, log=log)
    try:
        h._dispatch("POST")
    except OSError:
        pass
head, body = split_response(h)
check("an upstream failure after the headers only truncates the stream",
      body == RAW_CHUNKS[0] and live.closed and b"proxy_error" not in body,
      str(body[:120]))
check("a broken upstream stream never produces a second HTTP status",
      raw_of(h).count(b"HTTP/1.1 ") == 1, str(raw_of(h)[:80]))

# --- handler level: the non-streaming paths keep buffering and unwrapping ---

upstream_json = json.dumps({"data": {"id": "chatcmpl-1", "object": "chat.completion",
                                     "choices": [{"message": {"role": "assistant", "content": "hi"}}]}}).encode()
live = LiveBody([upstream_json])
with mock.patch.object(server, "get_valid_token", return_value="tok"), \
     mock.patch.object(server, "do_request",
                       return_value=(200, {"Content-Type": "application/json"}, live)):
    h = post_handler(acfg, "/v1/chat/completions", {"Authorization": f"Bearer {key}"},
                     {"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    h._dispatch("POST")
head, body = split_response(h)
check("a non-streamed chat response is still unwrapped from {\"data\": ...}",
      json.loads(body)["id"] == "chatcmpl-1" and "choices" in json.loads(body), str(body[:120]))
check("a non-streamed response keeps Content-Length and closes upstream",
      b"Content-Length:" in head and live.reads == 1 and live.closed, str(head))

err_body = json.dumps({"error": {"message": "upstream says no", "type": "invalid_request_error"}}).encode()
live = LiveBody([err_body])
with mock.patch.object(server, "get_valid_token", return_value="tok"), \
     mock.patch.object(server, "do_request",
                       return_value=(401, {"Content-Type": "application/json"}, live)):
    h = post_handler(acfg, "/v1/chat/completions", {"Authorization": f"Bearer {key}"},
                     {"model": "m", "messages": []})
    h._dispatch("POST")
head, body = split_response(h)
check("a non-200 upstream keeps its status and body",
      status_of(h) == 401 and body == err_body and b"Content-Length:" in head, str(head))
check("a non-200 upstream body is read once and closed", live.reads == 1 and live.closed)

log = []
live = LiveBody([b"event: error\ndata: {}\n\n"], log=log)
with mock.patch.object(server, "get_valid_token", return_value="tok"), \
     mock.patch.object(server, "do_request",
                       return_value=(429, {"Content-Type": "text/event-stream"}, live)):
    h = post_handler(acfg, "/v1/chat/completions", {"Authorization": f"Bearer {key}"},
                     {"model": "m", "stream": True, "messages": []}, log=log)
    h._dispatch("POST")
head, body = split_response(h)
check("an event-stream body is relayed even when the status is not 200",
      status_of(h) == 429 and body == b"event: error\ndata: {}\n\n"
      and b"content-length" not in head.lower() and live.closed, str(head))

# --- /v1/messages: OpenAI SSE -> Anthropic events, still incremental ---

def u_line(obj):
    """SSE line with the non-ASCII left as real UTF-8 bytes."""
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


MSG_CHUNKS = [
    u_line({"id": "cmpl-9", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]}).encode(),
    u_line({"choices": [{"index": 0, "delta": {"content": "天气"}}]}).encode(),
    u_line({"choices": [{"index": 0, "delta": {"content": "☀️好"}}]}).encode(),
    u_line({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}).encode(),
    b"data: [DONE]\n\n",
]

log = []
live = LiveBody(MSG_CHUNKS, log=log)
with mock.patch.object(server, "get_valid_token", return_value="tok"), \
     mock.patch.object(server, "do_request",
                       return_value=(200, {"Content-Type": "text/event-stream"}, live)):
    h = post_handler(acfg, "/v1/messages", {"Authorization": f"Bearer {key}"},
                     {"model": "claude-sonnet", "stream": True, "max_tokens": 64,
                      "messages": [{"role": "user", "content": "hi"}]}, log=log)
    h._dispatch("POST")
head, body = split_response(h)
text = body.decode()
reads = [i for i, e in enumerate(log) if e[0] == "read1"]
flushes = [i for i, e in enumerate(log) if e[0] == "flush"]
check("/v1/messages streams Anthropic events with no Content-Length and Connection: close",
      b"content-length" not in head.lower() and b"connection: close" in head.lower()
      and text.startswith("event: message_start"), str(head))
check("/v1/messages emits the whole Anthropic event sequence",
      "event: message_start" in text and text.rstrip().endswith('event: message_stop\ndata: {"type": "message_stop"}'),
      str(text[-80:]))
check("the first Anthropic event reaches the client before the next upstream chunk is read",
      len(reads) > 1 and flushes and flushes[0] < reads[1], f"reads={reads[:3]} flushes={flushes[:3]}")
check("/v1/messages closes the upstream body when the stream ends", live.closed)

live = LiveBody([b"".join(MSG_CHUNKS)])
with mock.patch.object(server, "get_valid_token", return_value="tok"), \
     mock.patch.object(server, "do_request",
                       return_value=(200, {"Content-Type": "text/event-stream"}, live)) as nonstream_request:
    h = post_handler(acfg, "/v1/messages", {"Authorization": f"Bearer {key}"},
                     {"model": "claude-sonnet", "stream": False, "max_tokens": 64,
                      "messages": [{"role": "user", "content": "hi"}]})
    h._dispatch("POST")
check("Anthropic stream:false still requests an upstream SSE response",
      json.loads(nonstream_request.call_args.args[2])["stream"] is True)
head, body = split_response(h)
non_stream_msg = json.loads(body)
check("a non-streamed /v1/messages still answers with one Anthropic message",
      non_stream_msg["type"] == "message"
      and non_stream_msg["content"][0]["text"] == "天气☀️好"
      and b"Content-Length:" in head, str(body[:120]))
check("a non-streamed /v1/messages reads the body once and closes upstream",
      live.reads == 1 and live.closed)

# --- anthropic_stream_response: same events from any chunking of the same bytes ---

def sse_events(text):
    """[(event name, parsed data)] for a complete Anthropic SSE blob."""
    out = []
    for block in text.split("\n\n"):
        if not block:
            continue
        name, data = None, []
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data.append(line[6:])
        out.append((name, json.loads("".join(data))))
    return out


def events_of(chunks, model="claude-test"):
    return sse_events("".join(anthropic_stream_response(chunks, model)))


def text_of(events):
    return "".join(d["delta"]["text"] for e, d in events
                   if e == "content_block_delta" and d["delta"].get("type") == "text_delta")


msg_raw = b"".join(MSG_CHUNKS)
base = events_of([msg_raw])
per_event = events_of([c for c in MSG_CHUNKS])
bytewise = events_of([msg_raw[i:i + 1] for i in range(len(msg_raw))])
offsets = [k for k in range(1, len(msg_raw)) if events_of([msg_raw[:k], msg_raw[k:]]) != base]
check("one event per chunk gives the whole-input events", per_event == base)
check("one byte per chunk gives the whole-input events", bytewise == base)
check("splitting at any single byte gives the whole-input events", offsets == [], str(offsets[:5]))
check("CRLF frame boundaries give the whole-input events",
      events_of([msg_raw.replace(b"\n", b"\r\n")]) == base)
check("multiple events in one chunk are all converted", per_event == base and len(base) > 3)
done_mid = (u_line({"id": "cmpl-d", "choices": [{"index": 0, "delta": {"content": "好"}}]}).encode()
            + b"data: [DONE]\n\n"
            + u_line({"choices": [{"index": 0, "delta": {"content": "的"}}]}).encode()
            + b"data: [DONE]\n\n")
check("[DONE] ends the stream and is never converted into an event",
      text_of(events_of([done_mid])) == "好" and "DONE" not in json.dumps(events_of([done_mid])),
      str(events_of([done_mid])))
tail_chunks = [u_line({"id": "cmpl-c", "choices": [{"index": 0, "delta": {"content": "好"}}]}).encode(),
               b"data: [DONE]\n\n",
               u_line({"choices": [{"index": 0, "delta": {"content": "的"}}]}).encode()]
pulled = []


def counting(chunks):
    for chunk in chunks:
        pulled.append(chunk)
        yield chunk


check("[DONE] stops pulling the upstream stream",
      text_of(events_of(counting(tail_chunks))) == "好" and len(pulled) == 2, str(len(pulled)))
garbled = (u_line({"id": "cmpl-g", "choices": [{"index": 0, "delta": {"content": "好"}}]}).encode()
           + b"data: \xff\xfe not json\n\n"
           + u_line({"choices": [{"index": 0, "delta": {"content": "的"}}]}).encode()
           + b"data: [DONE]\n\n")
check("a malformed frame is skipped without losing the frames around it",
      text_of(events_of([garbled])) == "好的", str(events_of([garbled])))
check("the text block runs start -> delta -> stop -> message_delta -> message_stop",
      [e for e, _ in base] == ["message_start", "content_block_start", "content_block_delta",
                               "content_block_delta", "content_block_stop", "message_delta",
                               "message_stop"], str([e for e, _ in base]))
check("the text block is opened empty and closed under its own index",
      base[1][1]["index"] == 0 and base[1][1]["content_block"] == {"type": "text", "text": ""}
      and base[4][1]["index"] == 0, f"{base[1][1]} {base[4][1]}")
check("text deltas carry the exact upstream text", text_of(base) == "天气☀️好", text_of(base))
check("message_start carries the model, the upstream id and a usage frame",
      base[0][1]["message"]["model"] == "claude-test"
      and "cmpl-9" in base[0][1]["message"]["id"] and "usage" in base[0][1]["message"],
      str(base[0][1]["message"]))
check("exactly one finish and one usage frame are emitted, at the end",
      [e for e, _ in base].count("message_stop") == 1
      and [e for e, _ in base].count("message_delta") == 1
      and base[-2][1]["delta"]["stop_reason"] == "end_turn"
      and "usage" in base[-2][1], str(base[-2:]))
check("usage is zero rather than invented when upstream reports none",
      base[-2][1]["usage"] == {"input_tokens": 0, "output_tokens": 0}, str(base[-2][1]))
usage_stream = (u_line({"id": "cmpl-u", "choices": [{"index": 0, "delta": {"content": "hi"}}]}).encode()
                + u_line({"choices": [{"index": 0, "delta": {}}],
                          "usage": {"prompt_tokens": 7, "completion_tokens": 3}}).encode()
                + b"data: [DONE]\n\n")
check("the terminal usage frame carries the upstream token counts",
      events_of([usage_stream])[-2][1]["usage"] == {"input_tokens": 7, "output_tokens": 3},
      str(events_of([usage_stream])[-2][1]))
repeat_finish = (u_line({"id": "cmpl-r", "choices": [{"index": 0, "delta": {"content": "x"}}]}).encode()
                 + u_line({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}).encode()
                 + u_line({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 5, "completion_tokens": 2}}).encode()
                 + b"data: [DONE]\n\n")
rep = events_of([repeat_finish])
check("a repeated finish_reason still terminates the stream exactly once",
      [e for e, _ in rep].count("message_stop") == 1
      and [e for e, _ in rep].count("message_delta") == 1
      and rep[-2][1]["usage"] == {"input_tokens": 5, "output_tokens": 2}, str(rep[-2:]))
check("the stop reason still maps length onto max_tokens",
      events_of([u_line({"id": "cmpl-l", "choices": [{"index": 0, "delta": {"content": "x"}}]}).encode()
                 + u_line({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}).encode()
                 + b"data: [DONE]\n\n"])[-2][1]["delta"]["stop_reason"] == "max_tokens")
check("a stream with no finish_reason but a [DONE] still converts its text",
      text_of(events_of([u_line({"id": "cmpl-h", "choices": [{"index": 0, "delta": {"content": "半"}}]}).encode()
                         + b"data: [DONE]\n\n"])) == "半")
truncated = [u_line({"id": "cmpl-x", "choices": [{"index": 0, "delta": {"content": "半"}}]}).encode(),
             u_line({"choices": [{"index": 0, "delta": {"content": "截"}}]}).encode()]
truncated_events = []
truncated_raised = False
try:
    for ev in anthropic_stream_response(truncated, "claude-test"):
        truncated_events.append(ev)
except RuntimeError:
    truncated_raised = True
check("a stream cut off without finish_reason or [DONE] raises instead of faking a stop",
      truncated_raised and "message_stop" not in "".join(truncated_events)
      and text_of(sse_events("".join(truncated_events))) == "半截", str(truncated_events[:2]))

multi_data = ('data: {"id":"cmpl-m","choices":[{"index":0,\n'
              'data: "delta":{"content":"多行"}}]}\n\n'
              "data: [DONE]\n\n").encode("utf-8")
check("an event split over several data lines is converted once",
      text_of(events_of([multi_data])) == "多行", str(events_of([multi_data])))
check("multi-line data survives being split mid-JSON",
      text_of(events_of([multi_data[:20], multi_data[20:]])) == "多行")

TOOL_CHUNKS = [
    u_line({"id": "cmpl-t", "choices": [{"index": 0, "delta": {"content": "想一下"}}]}).encode(),
    u_line({"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": "tu_1", "type": "function",
         "function": {"name": "weather", "arguments": '{"ci'}}]}}]}).encode(),
    u_line({"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": 'ty":"北京"}'}}]}}]}).encode(),
    u_line({"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 1, "id": "tu_2", "type": "function",
         "function": {"name": "time", "arguments": "{}"}}]}}]}).encode(),
    u_line({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}).encode(),
    b"data: [DONE]\n\n",
]
tool_raw = b"".join(TOOL_CHUNKS)
live = LiveBody([tool_raw])
with mock.patch.object(server, "get_valid_token", return_value="tok"), \
     mock.patch.object(server, "do_request",
                       return_value=(200, {"Content-Type": "text/event-stream"}, live)):
    h = post_handler(acfg, "/v1/messages", {"Authorization": f"Bearer {key}"},
                     {"model": "claude-sonnet", "stream": False, "max_tokens": 64,
                      "messages": [{"role": "user", "content": "weather and time"}]})
    h._dispatch("POST")
_, nonstream_tool_body = split_response(h)
nonstream_tool_message = json.loads(nonstream_tool_body)
nonstream_tools = [(block["name"], block["input"])
                   for block in nonstream_tool_message["content"] if block["type"] == "tool_use"]
check("Anthropic stream:false aggregates text and tool arguments from upstream SSE",
      nonstream_tool_message["content"][0] == {"type": "text", "text": "想一下"}
      and nonstream_tools == [("weather", {"city": "北京"}), ("time", {})]
      and nonstream_tool_message["stop_reason"] == "tool_use", str(nonstream_tool_message))
tool_events = events_of([tool_raw])
tool_bytewise = events_of([tool_raw[i:i + 1] for i in range(len(tool_raw))])
tool_offsets = [k for k in range(1, len(tool_raw))
                if events_of([tool_raw[:k], tool_raw[k:]]) != tool_events]
tool_starts = [(d["index"], d["content_block"]["id"], d["content_block"]["name"])
               for e, d in tool_events if e == "content_block_start"
               and d["content_block"]["type"] == "tool_use"]
text_starts = [d["index"] for e, d in tool_events if e == "content_block_start"
               and d["content_block"]["type"] == "text"]
stops = [d["index"] for e, d in tool_events if e == "content_block_stop"]
frags = {}
for e, d in tool_events:
    if e == "content_block_delta" and d["delta"].get("type") == "input_json_delta":
        frags.setdefault(d["index"], []).append(d["delta"]["partial_json"])
by_id = {tid: idx for idx, tid, _ in tool_starts}
joined = {tid: "".join(frags.get(idx, [])) for tid, idx in by_id.items()}
check("each tool call opens exactly one block, in upstream order",
      [(t, n) for _, t, n in tool_starts] == [("tu_1", "weather"), ("tu_2", "time")]
      and [i for i, _, _ in tool_starts] == sorted(i for i, _, _ in tool_starts), str(tool_starts))
check("tool fragments split across chunks are routed to their own block",
      joined == {"tu_1": '{"city":"北京"}', "tu_2": "{}"} and set(frags) == set(by_id.values()),
      f"{joined} {sorted(frags)}")
check("every opened block is stopped exactly once, in index order",
      stops == sorted(stops) and sorted(stops) == sorted(list(by_id.values()) + text_starts)
      and len(stops) == len(set(stops)), f"{stops} tools={tool_starts} text={text_starts}")
check("a tool stream ends once, after closing its blocks, with stop_reason tool_use",
      [e for e, _ in tool_events][-2:] == ["message_delta", "message_stop"]
      and [e for e, _ in tool_events].count("message_stop") == 1
      and tool_events[-2][1]["delta"]["stop_reason"] == "tool_use"
      and stops and max(i for i, (e, _) in enumerate(tool_events)
                        if e == "content_block_stop") == len(tool_events) - 3,
      str(tool_events[-2:]))
check("byte-wise tool chunks give the same events as whole-input", tool_bytewise == tool_events)
check("splitting a tool stream at any single byte gives the same events",
      tool_offsets == [], str(tool_offsets[:5]))

gates = []
delivered = []


def gated_chunks():
    for i, chunk in enumerate(MSG_CHUNKS):
        if i:
            gates.append(len(delivered))
        yield chunk


for ev in anthropic_stream_response(gated_chunks(), "claude-test"):
    delivered.append(ev)
check("the first increment is produced before the rest of the input is consumed",
      len(gates) == len(MSG_CHUNKS) - 1 and min(gates) >= 1, str(gates))
check("the gated run delivers the same events", sse_events("".join(delivered)) == base)

print()
if failures: print("FAILED:", len(failures)); sys.exit(1)
print("ALL PASS")
