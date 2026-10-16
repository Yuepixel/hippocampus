#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hippocampus 脑干消费者 C-S2（空闲信息收集，极小版）。

章程 `docs/脑干设计-20261014.md` §3-3（无外网、不给工具）+ §5「C-S2」：
  订阅脑干 S2（服务器空闲）信号 → 只读采集 102 本机材料 → 经
  dream-engine-tunnel（loopback 127.0.0.1:18888，无 key、绕开网关 hook）
  调**恰好一次**本地 LLM → 产出 `reports/collect-YYYY-MM-DD.md`。

硬约束（章程 §2 / §5 护栏）：
  - 只读：signals / state / logs / git-memory（`git log` 只读）；只写：独立报告目录 + 自身日志/状态；
  - 不写 mem0 池 / 权威 git-memory / 请求路径；不 import 脑干 / 网关 / 器官内部实现；
  - 无外网：唯一网络出口 = loopback 隧道；**不给模型任何工具**（payload 无 tools 键）；
  - 预算 cap：材料字符上限 / max_tokens / 超时上限；步数 cap：恰好 1 次 LLM 调用，不重试；
  - 限频：距上次运行 < `--min-interval-hours` 则跳过（状态落 `state/consumer-s2.json`）；
  - 恰好一次：一次运行 = 一份报告节 + 删信号重申；fail-soft：引擎不可达只落日志、不轰炸。

用法：
  collect_report.py [--root /opt/hippocampus/brainstem]
                    [--out  /opt/hippocampus/brainstem-consumers/reports]
                    [--gm   /opt/hippocampus/git-memory]
                    [--state /opt/hippocampus/brainstem-consumers/state]
                    [--log  /opt/hippocampus/brainstem-consumers/logs/consumer-collect.log]
                    [--base-url http://127.0.0.1:18888/v1] [--model GLM-5.3-Flash-EXL3]
                    [--max-material-chars 6000] [--item-chars 1200] [--max-tokens 700]
                    [--timeout 120] [--min-interval-hours 6]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SIGNAL = "s2"
# 唯一允许的网络出口：102 本机 loopback 上的 dream-engine-tunnel 末端。
DEFAULT_BASE_URL = "http://127.0.0.1:18888/v1"
DEFAULT_MODEL = "GLM-5.3-Flash-EXL3"

DEFAULTS = {
    "base_url": DEFAULT_BASE_URL,
    "model": DEFAULT_MODEL,
    "max_material_chars": 6000,
    "item_chars": 1200,
    "max_tokens": 700,
    "timeout": 120.0,
    "min_interval_hours": 6.0,
}

PROMPT_TMPL = """你是 102（worker-node，Hippocampus 外置记忆体）上的后台「信息收集」助手。
下面是从这台机器**只读**采集的材料快照（脑干状态/日志、健康报告、记忆仓近期条目、系统负载）。

请用简体中文产出一份简明报告，结构固定：
1) 现状摘要（系统 / 脑干 / 记忆仓，各 1-2 句）
2) 值得注意的点（异常、趋势、或「无异常」）
3) 建议（可选，最多 3 条；没有就写「无」）

硬约束：只依据下面材料，**不得臆造**未出现的任何事实、数字、日期、路径；材料未覆盖就写「材料未覆盖」。
不要输出 markdown 代码块。总长 ≤ 400 字。

=== 材料开始 ===
{material}
=== 材料结束 ===
"""


def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


def parse_ts(ts) -> dt.datetime:
    """解析脑干信号 / 状态的 ISO 时间；失败则回退当前本地时间。"""
    try:
        return dt.datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return now_local()


def tail_text(path, limit: int) -> str:
    """读文本文件**尾部**至多 limit 字符；不可读返回 ''（fail-soft）。"""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    txt = data.decode("utf-8", errors="replace").strip()
    if len(txt) <= limit:
        return txt
    return "...(截断)\n" + txt[-limit:]


def head_text(path, limit: int) -> str:
    """读文本文件**头部**至多 limit 字符；不可读返回 ''。"""
    try:
        txt = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return txt if len(txt) <= limit else txt[:limit] + "\n...(截断)"


def sys_brief() -> str:
    """系统负载 / 内存概要（只读 /proc；非 Linux 则降级）。"""
    parts = []
    ga = getattr(os, "getloadavg", None)
    if ga is not None:
        try:
            parts.append("loadavg=" + " ".join(f"{v:.2f}" for v in ga()))
        except OSError:
            parts.append("loadavg=n/a")
    else:
        parts.append("loadavg=n/a")
    try:
        mi = Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace")
        for key in ("MemTotal", "MemAvailable"):
            m = re.search(rf"^{key}:\s+(\d+)", mi, re.M)
            if m:
                parts.append(f"{key}={int(m.group(1)) // 1024}MB")
    except OSError:
        pass
    return "  ".join(parts)


def git_recent(gm: Path, n: int = 8) -> str:
    """记忆仓最近 n 条提交标题（只读 `git log`）。"""
    try:
        r = subprocess.run(["git", "-C", str(gm), "log", "--oneline", f"-{int(n)}"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        return f"(不可读: {type(e).__name__})"
    out = (r.stdout or "").strip()
    return out or "(空)"


def latest_file(d, pattern: str):
    d = Path(d)
    if not d.is_dir():
        return None
    try:
        fs = sorted((p for p in d.glob(pattern) if p.is_file()),
                    key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    return fs[-1] if fs else None


def gather_material(root: Path, gm: Path, item_chars: int) -> list:
    """只读采集 102 本机材料 → [{'title':.., 'body':..}]（缺项不报错，fail-soft）。"""
    root, gm = Path(root), Path(gm)
    items = []

    st_path = root / "state" / "brainstem.json"
    try:
        st = json.loads(st_path.read_text(encoding="utf-8"))
        items.append({"title": "脑干 state", "body": json.dumps(st, ensure_ascii=False)})
    except (OSError, ValueError):
        items.append({"title": "脑干 state", "body": "(不可读)"})

    items.append({"title": "脑干日志尾",
                  "body": tail_text(root / "logs" / "brainstem.log", item_chars)})

    rep_dir = root.parent / "brainstem-consumers" / "reports"
    hp = latest_file(rep_dir, "health-*.md")
    items.append({"title": f"健康报告尾（{hp.name if hp else '无'}）",
                  "body": tail_text(hp, item_chars) if hp else "(无)"})

    items.append({"title": "记忆仓近期提交（git-memory）",
                  "body": git_recent(gm, 8)})
    items.append({"title": "MEMORY.md 头部",
                  "body": head_text(gm / "MEMORY.md", item_chars)})
    dp = latest_file(gm / "dreams", "*.md")
    items.append({"title": f"最近做梦报告（{dp.name if dp else '无'}）",
                  "body": tail_text(dp, item_chars) if dp else "(无)"})

    items.append({"title": "系统负载", "body": sys_brief()})
    return items


def assemble_material(items: list, max_chars: int) -> str:
    """把材料拼成有界文本（总长 ≤ max_chars，超出即截断并标注）。"""
    chunks = [f"## {it['title']}\n{it['body']}" for it in items]
    text = "\n\n".join(chunks)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...(材料已按上限截断)"
    return text


def build_prompt(material: str) -> str:
    return PROMPT_TMPL.format(material=material)


def llm_chat(base_url: str, prompt: str, model: str = "", timeout: float = 120.0,
             max_tokens: int = 700) -> dict:
    """单次皮层调用（**思考显式关闭**：2026-10-16 实测引擎思考若不关会烧光 max_tokens
    并在 reasoning_content 里返回 content=null / finish_reason=length）。**不重试**。"""
    payload = {"messages": [{"role": "user", "content": prompt}], "temperature": 0,
               "max_tokens": int(max_tokens), "stream": False,
               "chat_template_kwargs": {"enable_thinking": False}}
    if model:
        payload["model"] = model
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8", errors="replace"))
        ch = (body.get("choices") or [{}])[0]
        msg = ch.get("message", {}) or {}
        return {"ok": True, "content": msg.get("content") or "",
                "finish_reason": ch.get("finish_reason", ""), "usage": body.get("usage", {})}
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError) as e:
        return {"ok": False, "content": "", "finish_reason": "",
                "detail": f"{type(e).__name__}: {e}"}


def report_path(out_dir, day: str) -> Path:
    return Path(out_dir) / f"collect-{day}.md"


def ensure_header(path: Path, day: str) -> None:
    """报告不存在时写标题块（只写一次）。"""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# 空闲信息收集报告 — {day}（北京时间）\n\n"
        "> 消费者 C-S2（brainstem-consumers）｜触发：脑干 S2 空闲信号\n"
        "> 材料：只读 102 本机（脑干 state/logs + 消费者健康报告 + git-memory 近期条目 + 系统负载）\n"
        "> 端点：dream-engine-tunnel（loopback，无 key、无外网、无工具）\n"
        "> 只读聚合；不写记忆池 / git-memory。产出仅供人复核。\n\n",
        encoding="utf-8")


def append_section(path: Path, day: str, section: str) -> None:
    ensure_header(path, day)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(section)


def trigger_line(sig: dict) -> str:
    ts = parse_ts(sig.get("ts", "")).strftime("%Y-%m-%d %H:%M:%S")
    return (f"- 触发：脑干 S2 idle_seconds={sig.get('idle_seconds', '?')} "
            f"loadavg={sig.get('loadavg', '?')}（信号 ts {ts}）")


def skip_section(when: dt.datetime, sig: dict, reason: str) -> str:
    return (f"## 运行 {when.strftime('%H:%M:%S')} — 跳过\n\n"
            f"{trigger_line(sig)}\n"
            f"- 原因：{reason}\n\n")


def ok_section(when: dt.datetime, sig: dict, opts: dict, n_items: int,
               n_chars: int, res: dict, elapsed: float) -> str:
    usage = res.get("usage") or {}
    toks = usage.get("completion_tokens", "?")
    return (f"## 运行 {when.strftime('%H:%M:%S')}\n\n"
            f"{trigger_line(sig)}\n"
            f"- 材料：{n_items} 段 / {n_chars} 字符（cap {opts['max_material_chars']}）\n"
            f"- 端点：{opts['base_url']}  model={opts['model'] or '(默认)'}  "
            f"max_tokens={opts['max_tokens']}\n"
            f"- 结果：ok  tokens={toks}  finish={res.get('finish_reason', '?')}  "
            f"用时 {elapsed:.1f}s\n\n"
            f"{(res.get('content') or '').strip()}\n\n---\n\n")


def read_state(state_file: Path) -> dict:
    try:
        obj = json.loads(Path(state_file).read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(state_file: Path, state: dict) -> None:
    p = Path(state_file)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def within_interval(state: dict, hours: float, now: dt.datetime) -> bool:
    """距上次运行是否未满 hours（限频）。"""
    last = state.get("last_run_ts")
    if not last or hours <= 0:
        return False
    try:
        t = dt.datetime.fromisoformat(str(last))
    except ValueError:
        return False
    return (now - t).total_seconds() < float(hours) * 3600.0


def clear_signal(cur: Path, logger) -> None:
    try:
        cur.unlink()
    except OSError as e:
        logger(f"WARN {SIGNAL}: cannot clear signal ({e!r})")


def run(root, out_dir, gm, state_dir, opts: dict, logger, chat=llm_chat, now=None) -> int:
    """处理一次 S2 信号：采集 → 一次 LLM → 报告节 → 删信号。返回 0/1（消费条数）。"""
    now = now or now_local()
    root, out_dir = Path(root), Path(out_dir)
    cur = root / "signals" / SIGNAL / "current.json"
    if not cur.is_file():
        return 0
    try:
        sig = json.loads(cur.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:                 # 坏信号：落日志 + 清掉（防 .path 卡死）
        logger(f"WARN {SIGNAL}: unreadable signal ({e!r}); clearing")
        clear_signal(cur, logger)
        return 0

    day = parse_ts(sig.get("ts", "")).strftime("%Y-%m-%d")
    out = report_path(out_dir, day)
    state_file = Path(state_dir) / "consumer-s2.json"
    state = read_state(state_file)

    if within_interval(state, opts["min_interval_hours"], now):     # 限频：不调 LLM
        logger(f"{SIGNAL}: rate-limited (<{opts['min_interval_hours']}h since last run); skip")
        append_section(out, day, skip_section(
            now, sig, f"限频：距上次运行不足 {opts['min_interval_hours']} 小时"))
        clear_signal(cur, logger)
        return 0

    items = gather_material(root, gm, opts["item_chars"])
    material = assemble_material(items, opts["max_material_chars"])
    prompt = build_prompt(material)

    t0 = time.time()
    res = chat(opts["base_url"], prompt, opts["model"], opts["timeout"], opts["max_tokens"])
    elapsed = time.time() - t0

    state["last_run_ts"] = now.isoformat()
    state["runs"] = int(state.get("runs", 0)) + 1
    state["last_result"] = "ok" if (res.get("ok") and (res.get("content") or "").strip()) else "skip"
    write_state(state_file, state)

    if not res.get("ok"):
        logger(f"WARN {SIGNAL}: engine unreachable ({res.get('detail')}); no retry")
        append_section(out, day, skip_section(now, sig, f"引擎不可达：{res.get('detail')}"))
        clear_signal(cur, logger)
        return 0
    if not (res.get("content") or "").strip():
        logger(f"WARN {SIGNAL}: empty model reply (finish={res.get('finish_reason')}); no retry")
        append_section(out, day, skip_section(
            now, sig, f"模型返回空（finish={res.get('finish_reason')}）"))
        clear_signal(cur, logger)
        return 0

    append_section(out, day, ok_section(now, sig, opts, len(items), len(material), res, elapsed))
    clear_signal(cur, logger)
    logger(f"{SIGNAL}: ok  model={opts['model'] or '(default)'}  "
           f"material={len(material)}ch  {elapsed:.1f}s")
    return 1


def main(argv=None) -> int:
    d = DEFAULTS
    ap = argparse.ArgumentParser(description="Hippocampus brainstem consumer C-S2 (idle info collection)")
    ap.add_argument("--root", default="/opt/hippocampus/brainstem")
    ap.add_argument("--out", default="/opt/hippocampus/brainstem-consumers/reports")
    ap.add_argument("--gm", default="/opt/hippocampus/git-memory")
    ap.add_argument("--state", default="/opt/hippocampus/brainstem-consumers/state")
    ap.add_argument("--log", default=None, help="optional consumer log file")
    ap.add_argument("--base-url", default=d["base_url"])
    ap.add_argument("--model", default=d["model"])
    ap.add_argument("--max-material-chars", type=int, default=d["max_material_chars"])
    ap.add_argument("--item-chars", type=int, default=d["item_chars"])
    ap.add_argument("--max-tokens", type=int, default=d["max_tokens"])
    ap.add_argument("--timeout", type=float, default=d["timeout"])
    ap.add_argument("--min-interval-hours", type=float, default=d["min_interval_hours"])
    args = ap.parse_args(argv)

    def logger(msg: str) -> None:
        line = f"[{now_local().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line, flush=True)                        # -> journald
        if args.log:
            try:
                p = Path(args.log)
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass

    opts = {"base_url": args.base_url, "model": args.model,
            "max_material_chars": args.max_material_chars, "item_chars": args.item_chars,
            "max_tokens": args.max_tokens, "timeout": args.timeout,
            "min_interval_hours": args.min_interval_hours}
    n = run(Path(args.root), Path(args.out), Path(args.gm), Path(args.state), opts, logger)
    logger(f"done: {n} signal(s) consumed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
