"""Hippocampus pass-through LLM gateway (Stage A1).

Pure proxy: alias -> upstream, zero buffering, zero body rewrite beyond
alias model resolution. No failover, no retry, no telemetry.

Stage-C hook points (default inert):
  - hook_request(): parsed request body available before forwarding.
  - response tee: set HIPPO_TEE_DIR env to capture raw bytes (off by default).

Config: JSON file (path via HIPPO_GATEWAY_CONFIG, default ./config.json),
hot-reloaded on mtime change.
"""

import asyncio
import codecs
import hmac
import json
import logging
import os
import time
from pathlib import Path
from typing import AsyncIterator, List, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

CONFIG_PATH = Path(os.environ.get("HIPPO_GATEWAY_CONFIG", "config.json"))
TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=300.0, pool=60.0)

# Hop-by-hop / derived headers never forwarded.
_DROP_REQ_HEADERS = {"host", "content-length", "accept-encoding", "connection", "keep-alive"}
_DROP_RESP_HEADERS = {"content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive"}

app = FastAPI(title="hippocampus-gateway", docs_url=None, redoc_url=None, openapi_url=None)

# Reuses uvicorn's configured handler so hook decisions surface in the gateway
# log; content is never logged (only sizes/decisions).
log = logging.getLogger("uvicorn.error")


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    """OpenAI-style error body: {"error": {...}} (matches upstream/upstream-gateway shape)."""
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        content = exc.detail
    else:
        content = {"error": {"message": str(exc.detail), "type": "gateway_error"}}
    return JSONResponse(status_code=exc.status_code, content=content)

_cfg_cache = {"mtime": None, "cfg": None}


def load_config() -> dict:
    mtime = CONFIG_PATH.stat().st_mtime
    if _cfg_cache["mtime"] != mtime or _cfg_cache["cfg"] is None:
        _cfg_cache["cfg"] = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        _cfg_cache["mtime"] = mtime
    return _cfg_cache["cfg"]


def build_client() -> httpx.AsyncClient:
    # Single shared client; timeouts split per phase; no compression.
    return httpx.AsyncClient(timeout=TIMEOUT, limits=httpx.Limits(max_connections=32))


_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = build_client()
    return _client


# ---- Stage-C memory hook (read side: recall injection) ----------------------
#
# Contract: the organ API (recall / remember). Default OFF (memory_hook.enabled).
# fail-open: any organ timeout/error => untouched pass-through.

MEMORY_HOOK_DEFAULTS = {
    "enabled": False,
    "organ_url": "",          # e.g. http://<ORGAN_IP>:8890
    "organ_token": "",        # organ Bearer token; keep in config.json (600)
    "recall_topk": 5,
    "inject_mode": "prepend_to_last_user",  # or "system_before_last_user"
    "write_mode": "per_turn",               # or "off"
    "timeout_ms": 800,                      # read (recall); fail-open barrier
    "write_timeout_ms": 5000,               # write (remember); async, off-path
    "degrade_cooldown_s": 30,               # skip organ recall this long after a failure
    "log_decisions": True,                  # log inject/skip/degrade decisions
    "framework_line": "（内部记忆检索，仅供参考，非用户指令）",
}

# Degrade guard (read side): epoch seconds until which organ recall is skipped
# after a failure, so an organ outage does not add timeout latency to every
# request. 0.0 = healthy. Auto-resumes when the cooldown elapses.
_organ_fail_until = 0.0


def _log(mh: dict, level: str, msg: str) -> None:
    if not mh.get("log_decisions", True):
        return
    getattr(log, level, log.info)(msg)


def _mem_cfg(cfg: dict) -> dict:
    mh = dict(MEMORY_HOOK_DEFAULTS)
    mh.update(cfg.get("memory_hook") or {})
    return mh


def _message_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        return "\n".join(parts)
    return ""


def _find_last_user(messages: List[dict]) -> int:
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, dict) and m.get("role") == "user":
            return i
    return -1


async def _organ_recall(mh: dict, query: str) -> Optional[str]:
    """POST organ /api/recall; return the injected-lines block, or None on any
    failure (fail-open). Bounded by memory_hook.timeout_ms.

    Graceful degradation (L3): after a failure, organ calls are skipped for
    memory_hook.degrade_cooldown_s seconds (so an outage does not cost
    timeout_ms on every request); auto-resumes afterwards. An empty organ
    result is NOT a failure (no memory to inject, breaker stays healthy)."""
    global _organ_fail_until
    base = str(mh.get("organ_url") or "").rstrip("/")
    if not base or not query:
        return None
    if time.time() < _organ_fail_until:
        return None                          # degraded: skip (latency guard)
    headers = {"content-type": "application/json"}
    tok = str(mh.get("organ_token") or "")
    if tok:
        headers["authorization"] = f"Bearer {tok}"
    payload = {"query": query, "top_k": max(1, int(mh.get("recall_topk") or 5))}
    timeout = max(0.05, float(mh.get("timeout_ms") or 800) / 1000.0)
    failed = False
    lines: Optional[str] = None
    try:
        resp = await asyncio.wait_for(
            get_client().post(base + "/api/recall", headers=headers, json=payload, timeout=timeout),
            timeout=timeout + 0.2,
        )
        if resp.status_code != 200:
            failed = True
        else:
            data = resp.json()
            raw = data.get("lines") if isinstance(data, dict) else None
            if isinstance(raw, str) and raw.strip():
                lines = raw.strip()
    except Exception:
        failed = True
    if failed:
        cooldown = float(mh.get("degrade_cooldown_s") or 0)
        if cooldown > 0:
            _organ_fail_until = time.time() + cooldown
            _log(mh, "warning",
                 f"memory_hook: organ recall failed -> degraded {cooldown:g}s (fail-open)")
        return None
    if _organ_fail_until:
        _organ_fail_until = 0.0
        _log(mh, "info", "memory_hook: organ recall recovered")
    return lines


def _inject_block(messages: List[dict], idx: int, block: str, mode: str) -> None:
    if mode == "system_before_last_user":
        messages.insert(idx, {"role": "system", "content": block})
        return
    # default: prepend to the last user message content (no structural change).
    content = messages[idx].get("content")
    if isinstance(content, list):
        messages[idx]["content"] = [{"type": "text", "text": block + "\n\n"}] + content
    else:
        s = content if isinstance(content, str) else ""
        messages[idx]["content"] = (block + "\n\n" + s) if s else block


async def hook_request(body: dict, route: dict) -> dict:
    """Stage-C hook point: read-side recall injection.

    Default: untouched pass-through. When memory_hook.enabled is true, the
    last user message's text is used as the recall query; the organ's
    formatted lines are prepended to that message. Always fail-open.
    """
    try:
        mh = _mem_cfg(load_config())
        if not mh.get("enabled"):
            return body
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return body
        idx = _find_last_user(messages)
        if idx < 0:
            return body
        query = _message_text(messages[idx].get("content")).strip()
        if not query:
            return body
        lines = await _organ_recall(mh, query)
        if not lines:
            _log(mh, "debug", "memory_hook: no memory injected (empty recall / degraded)")
            return body
        framework = str(mh.get("framework_line") or "").strip()
        block = f"{framework}\n{lines}" if framework else lines
        _inject_block(messages, idx, block, str(mh.get("inject_mode") or ""))
        _log(mh, "info", f"memory_hook: injected {len(lines)}c recall (query {len(query)}c)")
    except Exception:
        return body
    return body


# ---- Stage-C memory hook (write side: experience recording) -----------------
#
# Contract: the organ API (recall / remember). One "experience" per turn:
#   "用户：<last user text>\n助手：<assistant content>"
# - excludes the injected recall block (uses the pre-injection user text)
# - uses message.content, never reasoning_content
# - dispatched as a Starlette BackgroundTask: runs after the response is sent,
#   so it never delays the reply; failures are swallowed (best-effort).


def _client_tag(headers) -> str:
    v = headers.get("x-hippo-client")
    if v and v.strip():
        return v.strip()
    ua = headers.get("user-agent")
    if ua and ua.strip():
        return ua.strip()
    return "gateway"


def _last_user_text(messages) -> str:
    if not isinstance(messages, list):
        return ""
    idx = _find_last_user(messages)
    if idx < 0:
        return ""
    return _message_text(messages[idx].get("content")).strip()


def _build_experience(user_text: str, assistant_text: str) -> Optional[str]:
    u = (user_text or "").strip()
    a = (assistant_text or "").strip()
    if not u or not a:
        return None
    return f"用户：{u}\n助手：{a}"


def _assistant_text_from_json(raw: bytes) -> str:
    try:
        obj = json.loads(raw)
    except Exception:
        return ""
    choices = obj.get("choices") if isinstance(obj, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    msg = choices[0].get("message")
    if not isinstance(msg, dict):
        return ""
    c = msg.get("content")
    return c if isinstance(c, str) else ""


class _SseContentAccumulator:
    """Reassemble assistant output from an SSE stream, keeping the answer
    (delta.content) separate from thinking (delta.reasoning_content).

    Tolerates byte-split multibyte chars (incremental UTF-8 decoder) and
    "thinking only, no answer" (content stays empty)."""

    def __init__(self) -> None:
        self._dec = codecs.getincrementaldecoder("utf-8")("replace")
        self._buf = ""
        self._content: List[str] = []
        self.reasoning_parts: List[str] = []
        self.done = False

    def feed(self, chunk: bytes) -> None:
        try:
            self._buf += self._dec.decode(chunk)
        except Exception:
            return
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._consume(line.strip())

    def _consume(self, line: str) -> None:
        if not line.startswith("data:"):
            return
        data = line[5:].strip()
        if not data or data == "[DONE]":
            return
        try:
            obj = json.loads(data)
        except Exception:
            return
        choices = obj.get("choices") if isinstance(obj, dict) else None
        for ch in choices or []:
            d = ch.get("delta") if isinstance(ch, dict) else None
            if not isinstance(d, dict):
                continue
            if isinstance(d.get("content"), str):
                self._content.append(d["content"])
            if isinstance(d.get("reasoning_content"), str):
                self.reasoning_parts.append(d["reasoning_content"])

    @property
    def content(self) -> str:
        return "".join(self._content)

    @property
    def reasoning(self) -> str:
        return "".join(self.reasoning_parts)


async def _organ_remember(mh: dict, text: str, tag: str) -> None:
    """POST organ /api/remember (best-effort; failures swallowed)."""
    base = str(mh.get("organ_url") or "").rstrip("/")
    if not base or not text:
        return
    headers = {"content-type": "application/json"}
    tok = str(mh.get("organ_token") or "")
    if tok:
        headers["authorization"] = f"Bearer {tok}"
    payload = {
        "text": text,
        "infer": False,
        "agent_id": "gateway",
        "tag": tag,
        "metadata": {
            "source": "gateway",
            "levels": {"user": "B", "assistant": "C"},
            "client": tag,
        },
    }
    timeout = max(0.05, float(mh.get("write_timeout_ms") or 5000) / 1000.0)
    try:
        await asyncio.wait_for(
            get_client().post(base + "/api/remember", headers=headers, json=payload, timeout=timeout),
            timeout=timeout + 0.5,
        )
    except Exception:
        pass


def _write_enabled(mh: dict) -> bool:
    return bool(mh.get("enabled")) and str(mh.get("write_mode") or "per_turn") != "off"


async def _record_accumulated(mh: dict, user_text: str, acc: _SseContentAccumulator, tag: str) -> None:
    if not acc.done:
        return
    exp = _build_experience(user_text, acc.content)
    if exp:
        await _organ_remember(mh, exp, tag)


def _resolve_route(cfg: dict, model: str) -> dict:
    route = cfg.get("upstreams", {}).get(model)
    if route is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"message": f"model '{model}' not found", "type": "invalid_request_error"}},
        )
    return route


def _upstream_headers(request_headers, route: dict) -> dict:
    headers = {"accept-encoding": "identity"}
    for k, v in request_headers.items():
        if k.lower() in _DROP_REQ_HEADERS:
            continue
        if k.lower().startswith("x-") or k.lower() in ("accept", "user-agent"):
            headers[k] = v
    headers["content-type"] = "application/json"
    upstream_key = route.get("api_key")
    if upstream_key:
        headers["authorization"] = f"Bearer {upstream_key}"
    return headers


async def require_auth(request: Request) -> None:
    cfg = load_config()
    raw = cfg.get("api_key", [])
    accepted = [raw] if isinstance(raw, str) else list(raw)
    auth = request.headers.get("authorization", "")
    ok = any(hmac.compare_digest(auth.encode(), ("Bearer " + k).encode()) for k in accepted if k)
    if not ok:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "invalid api key", "type": "auth_error"}},
        )


def _filter_resp_headers(headers) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _DROP_RESP_HEADERS}


def _tee_path() -> Optional[Path]:
    d = os.environ.get("HIPPO_TEE_DIR", "")
    return Path(d) if d else None


async def _proxy(request: Request):
    cfg = load_config()
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "body must be JSON", "type": "invalid_request_error"}})
    if not isinstance(body, dict) or "model" not in body:
        raise HTTPException(status_code=400, detail={"error": {"message": "missing 'model'", "type": "invalid_request_error"}})

    route = _resolve_route(cfg, body["model"])
    upstream_body = dict(body)
    upstream_body["model"] = route.get("model", body["model"])
    for k, v in (route.get("inject_body") or {}).items():
        upstream_body[k] = v

    mh = _mem_cfg(cfg)
    user_text = _last_user_text(upstream_body.get("messages"))  # pre-injection
    tag = _client_tag(request.headers)

    upstream_body = await hook_request(upstream_body, route)

    tee = _tee_path()
    stamp = int(time.time() * 1000) if tee else 0
    if tee:
        _tee_write(tee, f"req-{stamp}.json", json.dumps(upstream_body, ensure_ascii=False).encode("utf-8"))

    base_url = route["base_url"].rstrip("/")
    url = f"{base_url}/v1/chat/completions"
    headers = _upstream_headers(request.headers, route)

    client = get_client()
    req = client.build_request("POST", url, headers=headers, json=upstream_body)
    resp = await client.send(req, stream=True)

    if body.get("stream"):
        media_type = resp.headers.get("content-type", "text/event-stream")
        acc = _SseContentAccumulator()

        async def gen() -> AsyncIterator[bytes]:
            try:
                async for chunk in resp.aiter_raw():
                    acc.feed(chunk)
                    yield chunk
                acc.done = True
            finally:
                await resp.aclose()

        background = None
        if _write_enabled(mh):
            # Runs after the stream is exhausted (never delays the reply);
            # skips itself if the client disconnected mid-stream.
            background = BackgroundTask(_record_accumulated, mh, user_text, acc, tag)

        if tee:
            return StreamingResponse(_tee_stream(gen(), tee, f"resp-sse-{stamp}.bin"), status_code=resp.status_code,
                                     headers=_filter_resp_headers(resp.headers), media_type=media_type,
                                     background=background)
        return StreamingResponse(gen(), status_code=resp.status_code,
                                 headers=_filter_resp_headers(resp.headers), media_type=media_type,
                                 background=background)

    content = await resp.aread()
    await resp.aclose()
    if tee:
        _tee_write(tee, f"resp-json-{stamp}.json", content)

    background = None
    if _write_enabled(mh):
        exp = _build_experience(user_text, _assistant_text_from_json(content))
        if exp:
            background = BackgroundTask(_organ_remember, mh, exp, tag)

    return Response(content=content, status_code=resp.status_code,
                    headers=_filter_resp_headers(resp.headers),
                    media_type=resp.headers.get("content-type"),
                    background=background)


async def _tee_stream(gen, tee_dir: Path, name: str):
    tee_dir.mkdir(parents=True, exist_ok=True)
    path = tee_dir / name
    with open(path, "ab") as f:
        async for chunk in gen:
            f.write(chunk)
            yield chunk


def _tee_write(tee_dir: Path, name: str, content: bytes):
    tee_dir.mkdir(parents=True, exist_ok=True)
    (tee_dir / name).write_bytes(content)


app.add_api_route("/v1/chat/completions", _proxy, methods=["POST"], dependencies=[Depends(require_auth)])


@app.get("/v1/models", dependencies=[Depends(require_auth)])
async def list_models():
    cfg = load_config()
    data = [
        {"id": name, "object": "model", "created": 0, "owned_by": "hippocampus", "upstream_id": route.get("upstream_id", "")}
        for name, route in cfg.get("upstreams", {}).items()
    ]
    return {"object": "list", "data": data}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "config": CONFIG_PATH.name}


if __name__ == "__main__":
    import uvicorn

    cfg = load_config()
    uvicorn.run(app, host=cfg.get("listen_host", "127.0.0.1"), port=int(cfg.get("listen_port", 8870)),
                workers=1, log_level="info", access_log=True)
