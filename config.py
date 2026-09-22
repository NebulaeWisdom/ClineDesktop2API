"""Configuration: root config.json, service settings and the api key -> account map."""
import json
import os
import re
import secrets
import sys
from pathlib import Path


def _base_dir():
    """Directory holding config.json: the source tree, or the exe directory when frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


CONFIG_PATH = _base_dir() / "config.json"
TEMPLATE_PATH = CONFIG_PATH.with_name("config.example.json")

_FIELDS = ("port", "bind", "log_path", "rate_limit", "desensitize", "api_keys", "auth")
_AUTH_FIELDS = ("workos_base_url", "cline_base_url", "client_id", "user_agent",
                "refresh_buffer_ms", "request_timeout_seconds")


def _parse_interval(s):
    s = (s or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d+(?:\.\d+)?)(ms|s|m)?$", s)
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2) or "s"
    return n * {"ms": 0.001, "s": 1, "m": 60}[unit]


def _read_config(path):
    """Read config.json, failing loudly when it (or a field) is absent."""
    if not path.exists():
        raise FileNotFoundError(
            f"config file not found: {path}\n"
            f"  copy {path.with_name('config.example.json')} to {path} and edit it,\n"
            f"  or run: python main.py --login"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in _FIELDS:
        if key not in data:
            raise ValueError(f"{path}: missing key '{key}'")
    for key in _AUTH_FIELDS:
        if key not in data["auth"]:
            raise ValueError(f"{path}: missing key 'auth.{key}'")
    return data


def _account_path(value, root):
    """Oauth file for one mapping entry; relative values resolve against the config dir."""
    path = Path(value)
    return (path if path.is_absolute() else root / path).resolve()


def _portable(path, root):
    """Path as written into config.json: relative to the config dir when inside it."""
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)


class Config:
    """Runtime view of config.json."""

    def __init__(self, data, path):
        self.path = path
        self.root = path.parent
        self.port = int(data["port"])
        self.bind = data["bind"]
        self.log_path = data["log_path"]
        self.rate_limit = _parse_interval(data["rate_limit"])
        self.desensitize = bool(data["desensitize"])
        self.auth = dict(data["auth"])
        self.api_keys = {key: _account_path(value, self.root)
                         for key, value in data["api_keys"].items()}

    def resolve_account(self, key):
        """Oauth file registered for this client key, or None when unregistered."""
        return self.api_keys.get(key)

    def register_account(self, path):
        """Return the key registered for path, minting and persisting one otherwise."""
        path = Path(path).resolve()
        for key, existing in self.api_keys.items():
            if os.path.normcase(str(existing)) == os.path.normcase(str(path)):
                return key
        key = secrets.token_hex(32)
        data = _read_config(self.path)
        data["api_keys"][key] = _portable(path, self.root)
        self.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        self.api_keys[key] = path
        return key


def initialize_config(path=CONFIG_PATH):
    """Create config.json from the shipped root template on the first explicit login."""
    path = Path(path).resolve()
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"\n  Created {path} from {TEMPLATE_PATH.name} (no accounts registered yet).")
    print("  Edit it to change port / bind / log_path / rate_limit, then log in.\n")
    return path


def load_config(args=None, path=CONFIG_PATH):
    """Load config.json, then apply any explicit (non-None) CLI override."""
    path = Path(path).resolve()
    cfg = Config(_read_config(path), path)
    if args is None:
        return cfg
    if getattr(args, "port", None) is not None:
        cfg.port = int(args.port)
    if getattr(args, "bind", None) is not None:
        cfg.bind = args.bind
    if getattr(args, "log", None) is not None:
        cfg.log_path = args.log
    if getattr(args, "rate_limit", None) is not None:
        cfg.rate_limit = _parse_interval(args.rate_limit)
    if getattr(args, "desensitize", None):
        cfg.desensitize = True
    return cfg
