"""Unit smoke tests for the Hippocampus gateway (mock upstream, no network)."""

import itertools
import json
import os

import httpx
import pytest
from starlette.testclient import TestClient

import app as app_module

CONFIG = {
    "listen_host": "127.0.0.1",
    "listen_port": 8870,
    "api_key": "sk-test",
    "upstreams": {
        "alias-a": {
            "upstream_id": "u1",
            "base_url": "http://upstream.test",
            "model": "real-a",
            "api_key": "sk-upstream",
            "inject_body": {"temperature": 0.5},
        },
        "direct-b": {
            "upstream_id": "u2",
            "base_url": "http://upstream.test",
            "model": "direct-b",
            "api_key": None,
            "inject_body": {},
        },
    },
}

SSE_CHUNKS = [b'data: {"id":"c1","choices":[{"delta":{"content":"hi"}}]}\n\n', b"data: [DONE]\n\n"]
captured = {}


async def _sse_stream(chunks=SSE_CHUNKS):
    for chunk in chunks:
        yield chunk


def _last_user(body):
    for m in reversed(body.get("messages") or []):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            return c if isinstance(c, str) else ""
    return ""


def handler(request: httpx.Request) -> httpx.Response:
    captured["url"] = str(request.url)
    if str(request.url).endswith("/api/recall"):
        captured["recall_calls"] = captured.get("recall_calls", 0) + 1
        captured["recall_body"] = json.loads(request.content)
        captured["organ_auth"] = request.headers.get("authorization")
        q = captured["recall_body"].get("query", "")
        if "boom" in q:
            return httpx.Response(500, json={"detail": "organ down"})
        if "emptyrecall" in q:
            return httpx.Response(200, json={"query": q, "kept_count": 0, "lines": "", "items": []})
        return httpx.Response(200, json={"query": q, "kept_count": 1,
                                         "lines": "- [opencode] recalled fact 42",
                                         "items": []})
    if str(request.url).endswith("/api/remember"):
        captured["remember_body"] = json.loads(request.content)
        captured["remember_auth"] = request.headers.get("authorization")
        if "boomwrite" in captured["remember_body"].get("text", ""):
            return httpx.Response(500, json={"detail": "organ down"})
        return httpx.Response(200, json={"results": [{"id": "r1", "memory": "x", "event": "ADD"}]})
    captured["auth"] = request.headers.get("authorization")
    captured["accept_encoding"] = request.headers.get("accept-encoding")
    captured["body"] = json.loads(request.content)
    payload = captured["body"]
    q = _last_user(payload)
    if payload.get("stream"):
        if "reasononly" in q:
            chunks = [b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
                      b'data: {"choices":[{"delta":{"reasoning_content":"thinking only"}}]}\n\n',
                      b"data: [DONE]\n\n"]
        else:
            chunks = SSE_CHUNKS
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse_stream(chunks))
    if "reasononly" in q:
        return httpx.Response(200, json={"id": "m1", "model": payload["model"],
                                         "choices": [{"message": {"role": "assistant",
                                                                  "content": "",
                                                                  "reasoning_content": "thinking only"}}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 2}})
    return httpx.Response(200, json={"id": "m1", "model": payload["model"],
                                     "choices": [{"message": {"role": "assistant",
                                                              "content": "ok",
                                                              "reasoning_content": "thinking..."}}],
                                     "usage": {"prompt_tokens": 1, "completion_tokens": 2}})


@pytest.fixture()
def client(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(CONFIG), encoding="utf-8")
    monkeypatch.setattr(app_module, "_organ_fail_until", 0.0)
    monkeypatch.setattr(app_module, "CONFIG_PATH", cfg_path)
    app_module._cfg_cache["mtime"] = None
    app_module._cfg_cache["cfg"] = None
    app_module._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=app_module.TIMEOUT)
    monkeypatch.setattr(app_module, "get_client", lambda: app_module._client)
    with TestClient(app_module.app) as c:
        yield c
    app_module._client = None


def _auth(token="sk-test"):
    return {"Authorization": f"Bearer {token}"}


def test_models_lists_aliases(client):
    r = client.get("/v1/models", headers=_auth())
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()["data"]]
    assert set(ids) == {"alias-a", "direct-b"}
    assert r.json()["data"][0]["upstream_id"] == "u1"


def test_auth_required(client):
    for r in (client.get("/v1/models"),
              client.post("/v1/chat/completions", json={"model": "alias-a"}),
              client.post("/v1/chat/completions", headers=_auth("sk-wrong"), json={"model": "alias-a"})):
        assert r.status_code == 401
        assert "invalid api key" in r.json()["error"]["message"]


def test_multi_key_accepted(client, tmp_path, monkeypatch):
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    cfg["api_key"] = ["sk-test", "sk-second"]
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + 5))
    assert client.get("/v1/models", headers=_auth("sk-test")).status_code == 200
    assert client.get("/v1/models", headers=_auth("sk-second")).status_code == 200
    assert client.get("/v1/models", headers=_auth("sk-third")).status_code == 401


def test_alias_rewrite_and_body_passthrough(client):
    body = {"model": "alias-a", "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "high", "temperature": 0.7}
    r = client.post("/v1/chat/completions", headers=_auth(), json=body)
    assert r.status_code == 200
    sent = captured["body"]
    assert sent["model"] == "real-a"                    # alias resolved
    assert sent["reasoning_effort"] == "high"           # untouched
    assert sent["temperature"] == 0.5                   # inject_body applied
    assert sent["messages"] == body["messages"]
    assert captured["auth"] == "Bearer sk-upstream"     # upstream key used
    assert captured["accept_encoding"] == "identity"    # no gzip
    # response passes through untouched
    assert r.json()["choices"][0]["message"]["reasoning_content"] == "thinking..."


def test_direct_model_no_rewrite(client):
    body = {"model": "direct-b", "messages": [], "stream": False}
    r = client.post("/v1/chat/completions", headers=_auth(), json=body)
    assert r.status_code == 200
    assert captured["body"]["model"] == "direct-b"
    assert "authorization" not in {k.lower() for k in captured}  # captured only keeps auth header value; checked via auth is None
    assert captured["auth"] is None


def test_stream_passthrough_bytes_identical(client):
    body = {"model": "direct-b", "messages": [], "stream": True}
    with client.stream("POST", "/v1/chat/completions", headers=_auth(), json=body) as r:
        assert r.status_code == 200
        assert r.headers["content-type"] == "text/event-stream"
        data = b"".join(r.iter_bytes())
    assert data == b"".join(SSE_CHUNKS)
    assert captured["body"]["stream"] is True


def test_unknown_model_404(client):
    r = client.post("/v1/chat/completions", headers=_auth(), json={"model": "nope"})
    assert r.status_code == 404
    assert "not found" in r.json()["error"]["message"]


def test_hot_reload_on_mtime(client, tmp_path, monkeypatch):
    r = client.post("/v1/chat/completions", headers=_auth(), json={"model": "alias-a"})
    assert r.status_code == 200
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    cfg["upstreams"]["new-c"] = {"upstream_id": "u3", "base_url": "http://upstream.test",
                                 "model": "real-c", "api_key": None, "inject_body": {}}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + 5))  # force mtime change
    r = client.get("/v1/models", headers=_auth())
    ids = [m["id"] for m in r.json()["data"]]
    assert "new-c" in ids


def test_non_json_body_400(client):
    r = client.post("/v1/chat/completions", headers={**_auth(), "Content-Type": "text/plain"},
                    content=b"not json")
    assert r.status_code == 400


# ---- Stage-C memory hook (read side) ----------------------------------------

def _make_client(tmp_path, monkeypatch, memory_hook):
    cfg = json.loads(json.dumps(CONFIG))
    if memory_hook is not None:
        cfg["memory_hook"] = memory_hook
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(app_module, "_organ_fail_until", 0.0)
    monkeypatch.setattr(app_module, "CONFIG_PATH", cfg_path)
    app_module._cfg_cache["mtime"] = None
    app_module._cfg_cache["cfg"] = None
    app_module._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=app_module.TIMEOUT)
    monkeypatch.setattr(app_module, "get_client", lambda: app_module._client)
    return TestClient(app_module.app)


@pytest.fixture()
def client_hook(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch,
                     {"enabled": True, "organ_url": "http://organ.test", "organ_token": "tok",
                      "recall_topk": 5})
    with c as cc:
        yield cc
    app_module._client = None


def test_hook_disabled_passthrough(client):
    body = {"model": "alias-a", "messages": [{"role": "user", "content": "hi"}], "temperature": 0.7}
    assert client.post("/v1/chat/completions", headers=_auth(), json=body).status_code == 200
    assert captured["body"]["messages"] == body["messages"]


def test_hook_injects_recall_prepend(client_hook):
    body = {"model": "alias-a",
            "messages": [{"role": "system", "content": "s"},
                         {"role": "user", "content": "remember?"}]}
    r = client_hook.post("/v1/chat/completions", headers=_auth(), json=body)
    assert r.status_code == 200
    msgs = captured["body"]["messages"]
    assert len(msgs) == 2                          # no structural change
    assert msgs[0]["content"] == "s"               # system untouched
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"].startswith("（内部记忆检索")
    assert "recalled fact 42" in msgs[1]["content"]
    assert msgs[1]["content"].endswith("remember?")
    assert captured["recall_body"]["query"] == "remember?"
    assert captured["recall_body"]["top_k"] == 5
    assert captured["organ_auth"] == "Bearer tok"


def test_hook_inject_mode_system_before_last_user(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch,
                     {"enabled": True, "organ_url": "http://organ.test", "organ_token": "tok",
                      "inject_mode": "system_before_last_user"})
    with c as cc:
        body = {"model": "alias-a",
                "messages": [{"role": "user", "content": "q1"},
                             {"role": "assistant", "content": "a1"},
                             {"role": "user", "content": "q2"}]}
        assert cc.post("/v1/chat/completions", headers=_auth(), json=body).status_code == 200
    msgs = captured["body"]["messages"]
    assert len(msgs) == 4
    assert msgs[2]["role"] == "system"
    assert "recalled fact 42" in msgs[2]["content"]
    assert msgs[3]["content"] == "q2"              # current user turn untouched
    app_module._client = None


def test_hook_failopen_on_organ_error(client_hook):
    body = {"model": "alias-a", "messages": [{"role": "user", "content": "boom"}]}
    assert client_hook.post("/v1/chat/completions", headers=_auth(), json=body).status_code == 200
    assert captured["body"]["messages"] == body["messages"]   # untouched on organ failure


def test_hook_no_user_message_passthrough(client_hook):
    body = {"model": "alias-a", "messages": [{"role": "system", "content": "s"}]}
    assert client_hook.post("/v1/chat/completions", headers=_auth(), json=body).status_code == 200
    assert captured["body"]["messages"] == body["messages"]


# ---- Stage-C memory hook (write side) ---------------------------------------

def _hook_conf(**over):
    cfg = {"enabled": True, "organ_url": "http://organ.test", "organ_token": "tok",
           "recall_topk": 5, "write_mode": "per_turn", "write_timeout_ms": 3000}
    cfg.update(over)
    return cfg


def _run(tmp_path, monkeypatch, hook, body, headers=None, stream=False):
    captured.clear()
    c = _make_client(tmp_path, monkeypatch, hook)
    hdrs = headers if headers is not None else _auth()
    with c:
        if stream:
            with c.stream("POST", "/v1/chat/completions", headers=hdrs, json=body) as r:
                assert r.status_code == 200
                b"".join(r.iter_bytes())
        else:
            assert c.post("/v1/chat/completions", headers=hdrs, json=body).status_code == 200
    app_module._client = None


def test_build_experience_pure():
    assert app_module._build_experience("q", "a") == "用户：q\n助手：a"
    assert app_module._build_experience(" q ", " a ") == "用户：q\n助手：a"
    assert app_module._build_experience("q", "") is None
    assert app_module._build_experience("", "a") is None


def test_sse_accumulator_splits_content_and_reasoning():
    acc = app_module._SseContentAccumulator()
    acc.feed(b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n')
    acc.feed(b'data: {"choices":[{"delta":{"reasoning_content":"think"}}]}\n\n')
    acc.feed(b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n')
    acc.feed(b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n')
    acc.feed(b"data: [DONE]\n\n")
    assert acc.content == "hello"
    assert acc.reasoning == "think"


def test_sse_accumulator_handles_split_multibyte():
    payload = ("data: " + json.dumps({"choices": [{"delta": {"content": "记忆"}}]},
                                     ensure_ascii=False) + "\n\n").encode("utf-8")
    acc = app_module._SseContentAccumulator()
    for i in range(0, len(payload), 3):        # arbitrary byte splits
        acc.feed(payload[i:i + 3])
    assert acc.content == "记忆"


def test_write_nonstream_records_experience(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, _hook_conf(),
         {"model": "alias-a", "messages": [{"role": "user", "content": "remember this"}]})
    rb = captured.get("remember_body")
    assert rb is not None
    assert rb["text"] == "用户：remember this\n助手：ok"   # content, not reasoning_content
    assert rb["infer"] is False
    assert rb["agent_id"] == "gateway"
    assert rb["metadata"]["source"] == "gateway"
    assert rb["metadata"]["levels"] == {"user": "B", "assistant": "C"}
    assert captured["remember_auth"] == "Bearer tok"


def test_write_uses_original_user_text_not_injected(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, _hook_conf(),
         {"model": "alias-a", "messages": [{"role": "user", "content": "plain question"}]})
    rb = captured["remember_body"]
    assert rb["text"] == "用户：plain question\n助手：ok"
    assert "内部记忆检索" not in rb["text"]
    assert "recalled fact 42" not in rb["text"]


def test_write_stream_records_experience(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, _hook_conf(),
         {"model": "direct-b", "messages": [{"role": "user", "content": "stream q"}], "stream": True},
         stream=True)
    rb = captured.get("remember_body")
    assert rb is not None
    assert rb["text"] == "用户：stream q\n助手：hi"


def test_write_skips_reasoning_only(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, _hook_conf(),
         {"model": "alias-a", "messages": [{"role": "user", "content": "reasononly"}]})
    assert "remember_body" not in captured


def test_write_skips_reasoning_only_stream(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, _hook_conf(),
         {"model": "direct-b", "messages": [{"role": "user", "content": "reasononly"}], "stream": True},
         stream=True)
    assert "remember_body" not in captured


def test_write_mode_off(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, _hook_conf(write_mode="off"),
         {"model": "alias-a", "messages": [{"role": "user", "content": "q"}]})
    assert "remember_body" not in captured


def test_write_failopen_on_organ_error(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, _hook_conf(),
         {"model": "alias-a", "messages": [{"role": "user", "content": "boomwrite"}]})
    assert captured.get("remember_body", {}).get("text")   # attempted


def test_write_tag_from_client_header(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, _hook_conf(),
         {"model": "alias-a", "messages": [{"role": "user", "content": "q"}]},
         headers={**_auth(), "x-hippo-client": "opencode/9"})
    rb = captured["remember_body"]
    assert rb["tag"] == "opencode/9"
    assert rb["metadata"]["client"] == "opencode/9"


def test_write_disabled_no_record(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, None,
         {"model": "alias-a", "messages": [{"role": "user", "content": "q"}]})
    assert "remember_body" not in captured


# ---- Stage-C memory hook (light gate + degradation) -------------------------

def test_light_gate_empty_recall_no_inject(tmp_path, monkeypatch):
    captured.clear()
    c = _make_client(tmp_path, monkeypatch, _hook_conf())
    body = {"model": "alias-a", "messages": [{"role": "user", "content": "emptyrecall z"}]}
    with c:
        assert c.post("/v1/chat/completions", headers=_auth(), json=body).status_code == 200
    app_module._client = None
    assert captured["recall_calls"] == 1
    assert captured["body"]["messages"] == body["messages"]   # untouched


def test_degrade_cooldown_skips_recall(tmp_path, monkeypatch):
    captured.clear()
    c = _make_client(tmp_path, monkeypatch, _hook_conf(degrade_cooldown_s=30))
    body = {"model": "alias-a", "messages": [{"role": "user", "content": "boom"}]}
    with c:
        assert c.post("/v1/chat/completions", headers=_auth(), json=body).status_code == 200
        assert captured["recall_calls"] == 1                  # attempted once (500)
        assert c.post("/v1/chat/completions", headers=_auth(), json=body).status_code == 200
        assert captured["recall_calls"] == 1                  # degraded: not retried
    app_module._client = None


def test_degrade_recovers_after_cooldown(tmp_path, monkeypatch):
    captured.clear()
    c = _make_client(tmp_path, monkeypatch, _hook_conf(degrade_cooldown_s=30))
    with c:
        c.post("/v1/chat/completions", headers=_auth(),
               json={"model": "alias-a", "messages": [{"role": "user", "content": "boom"}]})
        assert captured["recall_calls"] == 1
        monkeypatch.setattr(app_module, "_organ_fail_until", 0.0)   # cooldown elapsed
        c.post("/v1/chat/completions", headers=_auth(),
               json={"model": "alias-a", "messages": [{"role": "user", "content": "hello there"}]})
        assert captured["recall_calls"] == 2                  # retried after cooldown
        assert captured["body"]["messages"][0]["content"].startswith("（内部记忆检索")
    app_module._client = None


def test_degrade_cooldown_disabled(tmp_path, monkeypatch):
    captured.clear()
    c = _make_client(tmp_path, monkeypatch, _hook_conf(degrade_cooldown_s=0))
    body = {"model": "alias-a", "messages": [{"role": "user", "content": "boom"}]}
    with c:
        c.post("/v1/chat/completions", headers=_auth(), json=body)
        c.post("/v1/chat/completions", headers=_auth(), json=body)
        assert captured["recall_calls"] == 2                  # breaker off
    app_module._client = None


def test_empty_recall_does_not_trip_breaker(tmp_path, monkeypatch):
    captured.clear()
    c = _make_client(tmp_path, monkeypatch, _hook_conf(degrade_cooldown_s=30))
    body = {"model": "alias-a", "messages": [{"role": "user", "content": "emptyrecall a"}]}
    with c:
        c.post("/v1/chat/completions", headers=_auth(), json=body)
        c.post("/v1/chat/completions", headers=_auth(), json=body)
        assert captured["recall_calls"] == 2       # empty recall != failure
    app_module._client = None
