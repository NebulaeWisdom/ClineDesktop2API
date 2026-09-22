"""HTTP server: Cline passthrough + OpenAI-compatible endpoints."""
import http.server
import json
import sys
import threading
import time
from auth import get_valid_token
from upstream import do_request
from ratelimit import RateLimiter
from anthropic import anthropic_to_openai, anthropic_response, anthropic_stream_response
from reqlog import enabled, log_line, log_block, new_correlation_id
from desensitize import desensitize_payload

# Cline's own API surface — passed straight through to api.cline.bot.
CLINE_PASSTHROUGH_PREFIX = "/api/v1/"


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ClineDesktop2API"

    cfg = None
    limiter = None

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- routing -------------------------------------------------------

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def do_PATCH(self):
        self._dispatch("PATCH")

    def _dispatch(self, method):
        cid = new_correlation_id()

        if self.path.startswith("/healthz"):
            self._json(200, {"status": "ok"})
            return

        if self.limiter is not None and self.path.startswith("/v1/"):
            ip = self.client_address[0]
            ok, wait = self.limiter.allow(ip)
            if not ok:
                self.send_response(429)
                self.send_header("Retry-After", str(max(1, int(wait + 0.999))))
                self._json_body(429, {"error": {"message": "rate limit exceeded, try again later",
                                                "type": "rate_limit_error"}})
                return

        # Every non-/healthz request is routed by the key it presents.
        account = self._resolve_account()
        if account is None:
            self._json(401, {"error": {"message": "invalid API key", "type": "authentication_error"}})
            return
        try:
            token = get_valid_token(account, self.cfg.auth)
        except Exception as e:
            log_line(cid, f"account auth failed for {account}: {e}")
            self._json(502, {"error": {"message": f"account auth failed: {e}", "type": "account_auth_error"}})
            return

        body = self._read_body()

        if self.path.startswith(CLINE_PASSTHROUGH_PREFIX):
            self._passthrough(method, body, cid, token)
            return

        if self.path.startswith("/v1/models"):
            self._models(cid, token)
            return

        if self.path.startswith("/v1/chat/completions"):
            self._chat(method, body, cid, token)
            return

        if self.path.startswith("/v1/messages"):
            self._anthropic(method, body, cid, token)
            return

        # Unknown paths fall through to upstream verbatim (transparent proxy).
        self._passthrough(method, body, cid, token)

    # ---- handlers ------------------------------------------------------

    def _resolve_account(self):
        """Map the key this request presents to its oauth file, or None.

        Authorization: Bearer wins; x-api-key is only consulted when no bearer
        credential was presented. An unknown or missing key resolves to None —
        there is no default account.
        """
        h = self.headers.get("Authorization", "")
        if h.lower().startswith("bearer "):
            key = h[7:].strip()
        else:
            key = (self.headers.get("x-api-key") or "").strip()
        if not key:
            return None
        return self.cfg.resolve_account(key)

    def _passthrough(self, method, body, cid, token):
        """Forward Cline Desktop's own API calls verbatim."""
        fwd = {k: v for k, v in self.headers.items() if k.lower() not in ("host","connection","content-length","transfer-encoding")}
        log_line(cid, f"{method} {self.path} fwd={dict(fwd)}")
        if enabled() and body:
            log_block(cid, f"CLINE {method} {self.path}", body.decode("utf-8", "replace"))
        status, headers, response = do_request(method, self.path, body, dict(self.headers),
                                               proxy_token=token)
        with response:
            if self._is_stream(headers):
                self._raw_stream(status, headers, self._read_chunks(response))
            else:
                self._raw(status, headers, response.read())

    def _models(self, cid, token):
        status, headers, response = do_request("GET", "/api/v1/models", None, dict(self.headers),
                                               proxy_token=token)
        with response:
            rbody = response.read()
        if status != 200:
            self._raw(status, headers, rbody)
            return
        try:
            data = json.loads(rbody)
            models = data.get("data") or data.get("models") or []
            seen = set()
            out_models = []
            # merge in free-tier model IDs so clients (9router import) can
            # see and use them — they're otherwise only in /recommended-models
            from upstream import _free_model_ids
            free_ids = _free_model_ids()
            for m in models:
                mid = m.get("id")
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                out_models.append({"id": mid, "object": "model", "owned_by": "cline"})
            for fid in free_ids:
                if fid not in seen:
                    seen.add(fid)
                    out_models.append({"id": fid, "object": "model", "owned_by": "cline"})
            out = {"object": "list", "data": out_models}
        except Exception as e:
            self._json(502, {"error": {"message": f"model list parse: {e}", "type": "proxy_error"}})
            return
        self._json(200, out)

    def _chat(self, method, body, cid, token):
        if method != "POST":
            self._json(405, {"error": {"message": "use POST", "type": "invalid_request_error"}})
            return
        if enabled() and body:
            log_block(cid, "OPENAI /v1/chat/completions", body.decode("utf-8", "replace"))
        if self.cfg and self.cfg.desensitize and body:
            body = desensitize_payload(body)
        status, headers, response = do_request("POST", "/api/v1/chat/completions", body, dict(self.headers),
                                               proxy_token=token)
        with response:
            if self._is_stream(headers):
                self._raw_stream(status, headers, self._read_chunks(response))
                return
            rbody = response.read()
        # OpenAI clients expect the response at top level; api.cline.bot wraps in {"data":{...}}.
        if status == 200:
            try:
                j = json.loads(rbody)
                if "data" in j and isinstance(j.get("data"), dict):
                    rbody = json.dumps(j["data"]).encode()
            except Exception:
                pass
        self._raw(status, headers, rbody)

    @staticmethod
    def _is_stream(headers):
        for k, v in headers.items():
            if k.lower() == "content-type":
                return v.startswith("text/event-stream")
        return False

    def _anthropic(self, method, body, cid, token):
        if method != "POST":
            self._json(405, {"error": {"message": "use POST", "type": "invalid_request_error"}})
            return
        try:
            ar = json.loads(body)
        except Exception as e:
            self._json(400, {"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})
            return
        oai = anthropic_to_openai(ar)
        payload = json.dumps(oai).encode()
        if enabled():
            log_block(cid, "ANTHROPIC /v1/messages -> OPENAI", payload.decode())
        status, headers, response = do_request("POST", "/api/v1/chat/completions", payload, dict(self.headers),
                                               proxy_token=token)
        with response:
            if status != 200:
                self._raw(status, headers, response.read())
                return
            model = ar.get("model", "")
            if ar.get("stream"):
                self._sse_stream(anthropic_stream_response(self._read_chunks(response), model))
            else:
                self._json(200, anthropic_response(response.read(), model))

    # ---- helpers -------------------------------------------------------

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n > 0 else None

    def _raw(self, status, headers, body):
        self.send_response(status)
        for k, v in headers.items():
            lk = k.lower()
            if lk in ("content-length", "transfer-encoding", "connection"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, obj):
        self._json_body(status, obj)

    def _json_body(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _read_chunks(response):
        while chunk := response.read1():
            yield chunk

    def _raw_stream(self, status, headers, chunks):
        self.close_connection = True
        self.send_response(status)
        for key, value in headers.items():
            if key.lower() not in ("content-length", "transfer-encoding", "connection"):
                self.send_header(key, value)
        if not any(key.lower() == "cache-control" for key in headers):
            self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(chunk)
            self.wfile.flush()

    def _sse_stream(self, events):
        self._raw_stream(200, {"Content-Type": "text/event-stream"},
                         (event.encode("utf-8") for event in events))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def log_message(self, fmt, *args):
        pass


def serve(cfg):
    from reqlog import enable_logging
    enable_logging(cfg.log_path)
    Handler.cfg = cfg
    Handler.limiter = RateLimiter(cfg.rate_limit) if cfg.rate_limit else None
    if Handler.limiter is not None:
        Handler.limiter.start_cleanup()
    srv = http.server.ThreadingHTTPServer((cfg.bind, cfg.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        srv.server_close()
