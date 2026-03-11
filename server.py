#!/usr/bin/env python3
"""
Claude Local API Server v4
Listens on http://127.0.0.1:8080 by default.
Wraps `claude -p` to expose OpenAI-compatible and Anthropic-compatible endpoints.

Endpoints:
    POST /v1/chat/completions   OpenAI format  (stream / non-stream)
    POST /v1/messages           Anthropic format (stream / non-stream)
    GET  /v1/models
    GET  /v1/stats
    GET  /health
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("claude-server")

# ── Global config (populated in main()) ──────────────────────────────────────
CFG = {
    "host":    "127.0.0.1",
    "port":    8080,
    "timeout": 120,
    "workers": 8,
    "proxy":   "http://127.0.0.1:7890",
    "venv":    "nano_env",
}

# ── Concurrency ───────────────────────────────────────────────────────────────
_sem   = None   # threading.Semaphore, initialised in main()
_stats = {"total": 0, "active": 0, "errors": 0, "queued": 0}
_lock  = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════════
# Claude subprocess
# ══════════════════════════════════════════════════════════════════════════════

def run_claude(prompt: str) -> str:
    """Run `claude -p` inside the configured virtualenv with proxy env vars."""
    env = os.environ.copy()

    # Inject proxy variables
    proxy = CFG["proxy"]
    if proxy:
        for key in ("http_proxy", "https_proxy", "all_proxy",
                    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            env[key] = proxy

    # Build shell command
    escaped    = prompt.replace("'", "'\\''")
    claude_cmd = f"claude -p '{escaped}'"
    venv       = CFG["venv"]
    shell_cmd  = f"source {venv}/bin/activate && {claude_cmd}" if venv else claude_cmd

    # Acquire semaphore slot (queue when at capacity)
    with _lock:
        _stats["queued"] += 1
    _sem.acquire()
    with _lock:
        _stats["queued"] -= 1
        _stats["active"] += 1
        _stats["total"]  += 1

    try:
        result = subprocess.run(
            shell_cmd,
            shell=True,
            executable="/bin/bash",
            capture_output=True,
            text=True,
            timeout=CFG["timeout"],
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"claude timed out (>{CFG['timeout']}s)")
    finally:
        _sem.release()
        with _lock:
            _stats["active"] -= 1

    if result.returncode != 0:
        with _lock:
            _stats["errors"] += 1
        err = result.stderr.strip() or "unknown claude error"
        if "No such file" in err and venv:
            err = f"virtualenv not found: {venv}/bin/activate\n{err}"
        raise RuntimeError(err)

    output = result.stdout.strip()
    return output


# ══════════════════════════════════════════════════════════════════════════════
# Message helpers
# ══════════════════════════════════════════════════════════════════════════════

def messages_to_prompt(messages: list, system: str = "") -> str:
    parts = []
    if system:
        parts.append(f"[System]\n{system}\n")
    for m in messages:
        role    = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") for c in content if c.get("type") == "text"
            )
        parts.append(f"{'User' if role == 'user' else 'Assistant'}: {content}")
    parts.append("Assistant:")
    return "\n".join(parts)


def tok(text: str) -> int:
    return max(1, len(text.split()))


# ══════════════════════════════════════════════════════════════════════════════
# Response builders
# ══════════════════════════════════════════════════════════════════════════════

def openai_resp(content: str, model: str, prompt: str) -> dict:
    p, c = tok(prompt), tok(content)
    return {
        "id":      f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object":  "chat.completion",
        "created": int(time.time()),
        "model":   model,
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage":   {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c},
    }


def anthropic_resp(content: str, model: str, prompt: str) -> dict:
    p, c = tok(prompt), tok(content)
    return {
        "id":            f"msg_{uuid.uuid4().hex[:20]}",
        "type":          "message",
        "role":          "assistant",
        "content":       [{"type": "text", "text": content}],
        "model":         model,
        "stop_reason":   "end_turn",
        "stop_sequence": None,
        "usage":         {"input_tokens": p, "output_tokens": c},
    }


def error_resp(code: int, message: str) -> dict:
    return {"error": {"type": "api_error", "message": message, "code": code}}


# ══════════════════════════════════════════════════════════════════════════════
# SSE streaming
# ══════════════════════════════════════════════════════════════════════════════

def sse(data: dict) -> bytes:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def stream_openai(wfile, content: str, model: str):
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    ts  = int(time.time())

    # Role chunk
    wfile.write(sse({"id": cid, "object": "chat.completion.chunk", "created": ts,
                     "model": model,
                     "choices": [{"index": 0,
                                  "delta": {"role": "assistant", "content": ""},
                                  "finish_reason": None}]}))
    wfile.flush()

    # Content chunks (4 chars each)
    for i in range(0, len(content), 4):
        wfile.write(sse({"id": cid, "object": "chat.completion.chunk", "created": ts,
                         "model": model,
                         "choices": [{"index": 0,
                                      "delta": {"content": content[i:i+4]},
                                      "finish_reason": None}]}))
        wfile.flush()

    # Stop chunk
    wfile.write(sse({"id": cid, "object": "chat.completion.chunk", "created": ts,
                     "model": model,
                     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}))
    wfile.write(b"data: [DONE]\n\n")
    wfile.flush()


def stream_anthropic(wfile, content: str, model: str):
    mid = f"msg_{uuid.uuid4().hex[:20]}"

    def ev(event: str, data: dict):
        wfile.write(
            f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()
        )
        wfile.flush()

    ev("message_start", {
        "type": "message_start",
        "message": {"id": mid, "type": "message", "role": "assistant",
                    "content": [], "model": model, "stop_reason": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0}},
    })
    ev("content_block_start", {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""},
    })
    ev("ping", {"type": "ping"})

    for i in range(0, len(content), 4):
        ev("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": content[i:i+4]},
        })

    ev("content_block_stop",  {"type": "content_block_stop", "index": 0})
    ev("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": tok(content)},
    })
    ev("message_stop", {"type": "message_stop"})


# ══════════════════════════════════════════════════════════════════════════════
# Model list
# ══════════════════════════════════════════════════════════════════════════════

MODELS = {
    "object": "list",
    "data": [
        {"id": m, "object": "model", "created": int(time.time()), "owned_by": "local"}
        for m in ("claude-local", "claude-3-5-sonnet", "claude-opus-4", "claude-sonnet-4")
    ],
}


# ══════════════════════════════════════════════════════════════════════════════
# HTTP request handler
# ══════════════════════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # suppress default access log; we use logging instead

    # ── Routing ───────────────────────────────────────────────────────────────

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            with _lock:
                s = dict(_stats)
            self._json(200, {"status": "ok", "time": int(time.time()),
                             "workers": {"max": CFG["workers"], **s}})
        elif path.rstrip("/") == "/v1/models":
            self._json(200, MODELS)
        elif path == "/v1/stats":
            with _lock:
                s = dict(_stats)
            self._json(200, {"max_workers": CFG["workers"], **s})
        else:
            self._json(404, error_resp(404, f"unknown path: {path}"))

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()
        if body is None:
            return
        if path == "/v1/chat/completions":
            self._handle_openai(body)
        elif path == "/v1/messages":
            self._handle_anthropic(body)
        else:
            self._json(404, error_resp(404, f"unknown path: {path}"))

    # ── OpenAI handler ────────────────────────────────────────────────────────

    def _handle_openai(self, body: dict):
        model  = body.get("model", "claude-local")
        stream = body.get("stream", False)
        msgs   = body.get("messages", [])

        system, filtered = "", []
        for m in msgs:
            if m.get("role") == "system":
                system = m.get("content", "")
            else:
                filtered.append(m)

        if not filtered:
            return self._json(400, error_resp(400, "messages cannot be empty"))

        prompt = messages_to_prompt(filtered, system)
        log.info("OpenAI     stream=%-5s  model=%s  msgs=%d", stream, model, len(filtered))

        try:
            content = run_claude(prompt)
        except Exception as e:
            log.error("Error: %s", e)
            return self._json(500, error_resp(500, str(e)))

        if stream:
            self._sse_headers()
            stream_openai(self.wfile, content, model)
        else:
            self._json(200, openai_resp(content, model, prompt))

    # ── Anthropic handler ─────────────────────────────────────────────────────

    def _handle_anthropic(self, body: dict):
        model  = body.get("model", "claude-local")
        stream = body.get("stream", False)
        msgs   = body.get("messages", [])
        system = body.get("system", "")

        if not msgs:
            return self._json(400, error_resp(400, "messages cannot be empty"))

        prompt = messages_to_prompt(msgs, system)
        log.info("Anthropic  stream=%-5s  model=%s  msgs=%d", stream, model, len(msgs))

        try:
            content = run_claude(prompt)
        except Exception as e:
            log.error("Error: %s", e)
            return self._json(500, error_resp(500, str(e)))

        if stream:
            self._sse_headers()
            stream_anthropic(self.wfile, content, model)
        else:
            self._json(200, anthropic_resp(content, model, prompt))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _read_body(self) -> dict | None:
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n))
        except Exception:
            self._json(400, error_resp(400, "request body must be valid JSON"))
            return None

    def _json(self, code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _sse_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")


# ══════════════════════════════════════════════════════════════════════════════
# Threaded server
# ══════════════════════════════════════════════════════════════════════════════

class ThreadedServer(ThreadingMixIn, HTTPServer):
    """Handles each request in a dedicated thread."""
    daemon_threads     = True
    allow_reuse_address = True


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    global _sem

    parser = argparse.ArgumentParser(description="Claude Local API Server")
    parser.add_argument("--host",    default="127.0.0.1",              help="Bind address")
    parser.add_argument("--port",    type=int, default=8080,            help="Bind port")
    parser.add_argument("--timeout", type=int, default=120,             help="Per-call claude timeout (seconds)")
    parser.add_argument("--workers", type=int, default=8,               help="Max concurrent claude processes")
    parser.add_argument("--proxy",   default="http://127.0.0.1:7890",  help="Proxy URL, empty string to disable")
    parser.add_argument("--venv",    default="nano_env",                help="Virtualenv path, empty string to disable")
    args = parser.parse_args()

    CFG.update(vars(args))
    _sem = threading.Semaphore(CFG["workers"])

    # Ctrl+C / SIGTERM → hard exit
    def on_signal(sig, frame):
        log.info(
            "\n🛑  Signal received — exiting (requests: %d, errors: %d)",
            _stats["total"], _stats["errors"],
        )
        os._exit(0)

    signal.signal(signal.SIGINT,  on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    server = ThreadedServer((CFG["host"], CFG["port"]), Handler)

    log.info("🚀  Claude Local API Server v4")
    log.info("    Address : http://%s:%d", CFG["host"], CFG["port"])
    log.info("    Workers : %d   Timeout: %ds", CFG["workers"], CFG["timeout"])
    log.info("    Proxy   : %s", CFG["proxy"] or "disabled")
    log.info("    Venv    : %s", f"{CFG['venv']}/bin/activate" if CFG["venv"] else "disabled")
    log.info("    POST /v1/chat/completions  (OpenAI)")
    log.info("    POST /v1/messages          (Anthropic)")
    log.info("    GET  /v1/models  /v1/stats  /health")
    log.info("    Press Ctrl+C to quit\n")

    server.serve_forever()


if __name__ == "__main__":
    main()
