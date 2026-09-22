"""Cline auth — self-contained OAuth lifecycle, one credential file per account.

A credential file holds the WorkOS token pair of a single Cline account; the
root config.json maps each client API key to one such file. Every endpoint and
timeout comes from ``cfg.auth`` (see config.example.json), so the same code
serves any number of accounts: the Cline app is just another client.
"""
import base64
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

WORKOS_PREFIX = "workos:"

_CTX = ssl.create_default_context()

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path):
    """One lock per canonical credential file, so accounts refresh independently."""
    key = os.path.normcase(str(Path(path).resolve()))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def _post(url, payload, settings, form=False):
    if form:
        data = urllib.parse.urlencode(payload).encode()
        ctype = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(payload).encode()
        ctype = "application/json"
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": ctype,
        "User-Agent": settings["user_agent"],
        "X-CLIENT-TYPE": "cline-desktop",
    })
    try:
        r = urllib.request.urlopen(req, timeout=settings["request_timeout_seconds"], context=_CTX)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"error": raw.decode("utf-8", "replace")}


# ---- device flow -------------------------------------------------------

def device_start(settings):
    """Begin WorkOS device authorization. Returns the user-facing payload."""
    st, d = _post(f"{settings['workos_base_url']}/user_management/authorize/device",
                  {"client_id": settings["client_id"]}, settings)
    if st != 200:
        raise RuntimeError(f"device authorize failed: {st} {d}")
    return d


def device_poll(device_code, settings, interval, expires_in):
    """Poll until the user confirms in their browser. Returns WorkOS tokens."""
    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
        st, d = _post(f"{settings['workos_base_url']}/user_management/authenticate", {
            "client_id": settings["client_id"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
        }, settings)
        if st == 200 and d.get("access_token"):
            return d
        err = (d.get("error") or "").lower()
        if err and "authorization_pending" not in err and "slow_down" not in err:
            raise RuntimeError(f"device poll failed: {st} {d}")
    raise TimeoutError("device authorization expired")


# ---- cline registration ------------------------------------------------

def register_with_cline(access, refresh, settings):
    """Register a WorkOS token pair with Cline. Returns the account payload."""
    st, d = _post(f"{settings['cline_base_url']}/api/v1/auth/register",
                  {"accessToken": access, "refreshToken": refresh}, settings)
    if st != 200 or "data" not in d:
        raise RuntimeError(f"cline register failed: {st} {d}")
    return d["data"]


def refresh_workos(refresh_token, settings):
    """Rotate the WorkOS token pair. Returns the raw WorkOS response."""
    st, d = _post(f"{settings['workos_base_url']}/user_management/authenticate", {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": settings["client_id"],
    }, settings, form=True)
    if st != 200 or not d.get("access_token"):
        raise RuntimeError(f"workos refresh failed: {st} {d}")
    return d


# ---- token file --------------------------------------------------------

def _expiry_ms(value):
    if isinstance(value, (int, float)):
        return int(value)
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def _jwt_claims(token):
    """Claims carried by a JWT access token; {} when it is not a decodable JWT."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}


def _access_expiry_ms(access):
    """Absolute expiry in ms, from the access token's JWT exp claim."""
    exp = _jwt_claims(access).get("exp")
    return int(exp) * 1000 if exp else 0


def _read_store(path):
    """Parse one credential file into the canonical token shape, or None.

    Both key shapes are accepted: ``access``/``refresh`` as written by
    save_tokens(), and ``accessToken``/``refreshToken`` as used by the Cline
    desktop app. A leading ``workos:`` prefix is stripped, so a file copied
    from either side is readable as-is.
    """
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    tok = data.get("accessToken") or data.get("access") or ""
    if not tok:
        return None
    return {
        "access": tok[len(WORKOS_PREFIX):] if tok.startswith(WORKOS_PREFIX) else tok,
        "refresh": data.get("refreshToken") or data.get("refresh") or "",
        "expiresAt": _expiry_ms(data.get("expiresAt")),
        "accountId": data.get("accountId") or "",
        "registration_pending": bool(data.get("registration_pending")),
    }


def load_tokens(path):
    """Canonical tokens stored in this account file, or None if it does not exist."""
    return _read_store(path)


def save_tokens(tokens, path):
    """Write one account's tokens to its own file; the pending flag only when set."""
    data = {
        "access": tokens["access"],
        "refresh": tokens["refresh"],
        "expiresAt": tokens["expiresAt"],
        "accountId": tokens.get("accountId", ""),
    }
    if tokens.get("registration_pending"):
        data["registration_pending"] = True
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ---- main entry --------------------------------------------------------

def _registered(tokens, path, settings):
    """Finish a registration a refresh left pending, then persist the result.

    Registration is the only step that knows the Cline account id, and a
    refresh is only complete once its rotated pair is on disk; a failure here
    therefore propagates instead of leaving the caller with a half-rotated file.
    """
    reg = register_with_cline(tokens["access"], tokens["refresh"], settings)
    access = reg["accessToken"]
    done = {
        "access": access,
        "refresh": reg["refreshToken"],
        "expiresAt": _access_expiry_ms(access),
        "accountId": (reg.get("userInfo") or {}).get("id") or tokens.get("accountId", ""),
    }
    save_tokens(done, path)
    return done


def get_valid_token(path, settings, force=False):
    """Live access token for one account file, refreshing it in place when needed.

    Runs under that file's own lock and reloads inside it, so concurrent
    callers rotate the refresh token exactly once per account and accounts
    never block one another. Anything that prevents a live token raises; a
    stale token is never served as a fallback.
    """
    with _lock_for(path):
        tok = load_tokens(path)
        if not tok or not tok["access"]:
            raise RuntimeError(f"no usable credential in {path}")
        if tok["registration_pending"]:
            tok = _registered(tok, path, settings)
        if not force and tok["expiresAt"] and time.time() * 1000 < tok["expiresAt"] - settings["refresh_buffer_ms"]:
            return tok["access"]
        if not tok["refresh"]:
            raise RuntimeError(f"cannot refresh {path}: no refresh token")
        rotated = refresh_workos(tok["refresh"], settings)
        pending = {
            "access": rotated["access_token"],
            "refresh": rotated["refresh_token"],
            "expiresAt": _access_expiry_ms(rotated["access_token"]),
            "accountId": tok["accountId"],
            "registration_pending": True,
        }
        save_tokens(pending, path)
        return _registered(pending, path, settings)["access"]


def install_account(tokens, cfg):
    """Store one account's tokens under cfg.root/apikey and register its API key.

    The file name comes from the token's stable JWT ``sub`` claim, so logging
    the same account in again reuses both the file and the key already mapped
    to it.
    """
    sub = _jwt_claims(tokens["access"]).get("sub")
    if not sub:
        raise RuntimeError("access token has no sub claim; cannot name the account file")
    path = cfg.root / "apikey" / f"{sub}.json"
    with _lock_for(path):
        save_tokens(tokens, path)
    return cfg.register_account(path), path


def login(cfg):
    """Full interactive login. Prints the URL, blocks until confirmed."""
    settings = cfg.auth
    d = device_start(settings)
    url = d.get("verification_uri_complete") or d.get("verification_uri")
    print(f"\n  Open this URL and confirm:\n  {url}\n")
    print(f"  Code: {d.get('user_code')}\n")
    w = device_poll(d["device_code"], settings, d.get("interval", 5), d.get("expires_in", 300))
    reg = register_with_cline(w["access_token"], w["refresh_token"], settings)
    access = reg["accessToken"]
    return install_account({
        "access": access,
        "refresh": reg["refreshToken"],
        "expiresAt": _access_expiry_ms(access),
        "accountId": (reg.get("userInfo") or {}).get("id", ""),
    }, cfg)
