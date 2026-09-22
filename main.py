"""ClineDesktop2API — CLI entry point with interactive menu (mirrors WorkBuddy2API)."""
import argparse
import json
import sys
import urllib.error
import urllib.request


def print_menu():
    print()
    print("  ┌─────────────────────────────────┐")
    print("  │  1. Status                       │")
    print("  │  2. Start server                 │")
    print("  │  3. List models                  │")
    print("  │  4. Test chat                    │")
    print("  │  5. Login / add account          │")
    print("  │  6. Quit                         │")
    print("  └─────────────────────────────────┘")
    print()


def _short(key):
    """Key as shown in listings: only its tail, never the full secret."""
    return f"…{key[-6:]}" if len(key) > 6 else key


def select_account(cfg):
    """Let the user pick a registered api key. Returns (key, path) or None."""
    keys = sorted(cfg.api_keys)
    print()
    if not keys:
        print("  ✗ No accounts registered yet.")
        print("    Run option 5 (or `python main.py --login`) to log in first.")
        print()
        return None
    for i, k in enumerate(keys, 1):
        print(f"    {i}. {cfg.api_keys[k].name}  (key {_short(k)})")
    print()
    raw = input("  Account #: ").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= len(keys):
        print("\n  Invalid account. Try again.\n")
        return None
    key = keys[int(raw) - 1]
    return key, cfg.api_keys[key]


def show_status(cfg):
    sel = select_account(cfg)
    if not sel:
        return
    key, path = sel
    from auth import load_tokens
    tok = load_tokens(path)
    print()
    if not tok:
        print(f"  ✗ No credentials in {path}")
        print()
        return
    from datetime import datetime, timezone
    exp = tok.get("expiresAt", 0)
    if exp:
        exp_str = datetime.fromtimestamp(exp / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        expired = datetime.now(timezone.utc).timestamp() * 1000 > exp
    else:
        exp_str = "unknown"
        expired = False
    print("  ✓ Authenticated")
    print(f"    Key     : {_short(key)}")
    print(f"    File    : {path}")
    print(f"    User ID : {tok.get('accountId') or 'N/A'}")
    print(f"    Token   : {len(tok['access'])} chars")
    print(f"    Expires : {exp_str}{' (EXPIRED)' if expired else ''}")
    print(f"    Refresh : {'yes' if tok.get('refresh') else 'no'}")
    print()


def list_models(port, key):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/models",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        r = urllib.request.urlopen(req, timeout=30)
        d = json.loads(r.read())
        models = d.get("data", [])
        print(f"\n  {len(models)} models available:")
        for m in models[:30]:
            print(f"    {m['id']}")
        if len(models) > 30:
            print(f"    ... and {len(models) - 30} more")
        print()
    except Exception as e:
        print(f"\n  ✗ Failed: {e}\n")


def test_chat(port, key):
    model = input("  Model [~openai/gpt-luna-latest]: ").strip() or "~openai/gpt-luna-latest"
    msg = input("  Message [Hello]: ").strip() or "Hello"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": msg}],
        "max_tokens": 200,
    }).encode()
    print()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            method="POST",
        )
        r = urllib.request.urlopen(req, timeout=120)
        d = json.loads(r.read())
        content = d.get("data", d).get("choices", [{}])[0].get("message", {}).get("content", "")
        print(f"  ✓ {content}\n")
    except urllib.error.HTTPError as e:
        print(f"  ✗ {e.code}: {e.read().decode('utf-8', 'replace')[:200]}\n")
    except Exception as e:
        print(f"  ✗ {e}\n")


def login_account(cfg):
    """Device-code login; registers the account into cfg and reports its key and file."""
    from auth import login
    key, path = login(cfg)
    print(f"\n  ✓ Logged in: {key} -> {path}\n")


def run_menu(cfg):
    while True:
        print_menu()
        choice = input("  > ").strip()
        if choice == "1":
            show_status(cfg)
        elif choice == "2":
            from banner import print_banner
            print_banner(cfg)
            from server import serve
            serve(cfg)  # blocks until Ctrl-C
        elif choice == "3":
            sel = select_account(cfg)
            if sel:
                list_models(cfg.port, sel[0])
        elif choice == "4":
            sel = select_account(cfg)
            if sel:
                test_chat(cfg.port, sel[0])
        elif choice == "5":
            login_account(cfg)
        elif choice == "6":
            print("\n  Bye!\n")
            break
        else:
            print("\n  Invalid choice. Try again.\n")


def main():
    p = argparse.ArgumentParser(description="OpenAI-compatible proxy for Cline Desktop")
    p.add_argument("--port", type=int, default=None, help="listen port (overrides config.json)")
    p.add_argument("--bind", default=None, help="bind address (overrides config.json)")
    p.add_argument("--log", default=None, help="path to request log file (overrides config.json)")
    p.add_argument("--rate-limit", default=None,
                   help="min interval between requests per IP, e.g. 2s (overrides config.json)")
    p.add_argument("--desensitize", action="store_true", default=None,
                   help="rewrite moderation triggers in prompts (overrides config.json)")
    p.add_argument("--login", action="store_true",
                   help="run device-code OAuth login, register the account, then exit")
    p.add_argument("--no-menu", action="store_true",
                   help="start server directly instead of the interactive menu")
    args = p.parse_args()

    if args.login:
        from config import initialize_config, load_config
        initialize_config()
        cfg = load_config()
        login_account(cfg)
        return

    from config import load_config
    try:
        cfg = load_config(args)
    except FileNotFoundError as e:
        print(f"\n  ✗ {e}\n")
        sys.exit(2)

    # Interactive menu by default (like WorkBuddy2API); --no-menu starts server directly.
    if not args.no_menu:
        run_menu(cfg)
        return

    from banner import print_banner
    print_banner(cfg)
    from server import serve
    serve(cfg)


if __name__ == "__main__":
    main()
