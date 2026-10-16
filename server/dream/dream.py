#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hippocampus dream job -- nightly memory consolidation (stages 4c-1..4c-3).

See docs/整体计划.md  (this file deploys to /opt/hippocampus/dream/dream.py)
Stdlib only.

Guarantees:
  * dry-run  : repo is READ-ONLY (verified by a git status/HEAD self-check at the end)
  * apply    : only whitelisted ops (merge / archive); refuses a dirty repo; AT MOST one
               `dream:` commit per run (nothing to write -> no commit); the report lands in the
               repo at dreams/YYYY-MM-DD.md and rides that same commit
  * engine unreachable -> fail-soft: log it, run deterministic layer anyway, exit 0
  * no retries, no writes outside --out/--log and (apply only) the repo
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

LIMITS = {"memory_max_lines": 60, "memory_max_bytes": 6144}
SKIP_DIRS = {".git", "archive", "dreams"}  # retired entries / dream reports are not entries
CORE_FILES = {"MEMORY.md", "CONVENTIONS.md", "dream-prompt.md"}
# spec/format docs describe the entry format; their bullets are NOT memory entries
NON_ENTRY_FILES = {"CONVENTIONS.md", "dream-prompt.md"}
PLACEHOLDER_LINKS = {"链接", "双链", "链接名", "LINK"}
LINK_RE = re.compile(r"\[\[([^\[\]|]+?)\]\]")
SOURCE_RE = re.compile(r"\[source:")
BULLET_RE = re.compile(r"^\s*-\s+\S")
FENCE_RE = re.compile(r"```.*?```", re.S)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
MAX_LISTED = 20

# 4c-3 apply layer: the dream commit is authored by the dreamer, exactly once per run.
DREAM_AUTHOR_NAME = "hippocampus-dreamer"
DREAM_AUTHOR_EMAIL = "dream@hippocampus.local"
APPLY_ACTIONS = ("merge", "archive")  # whitelist; flag is proposal-only (never written)

# --- 4c-3 follow-up (A): candidate denoise + inline duplicate detection -------------
# The citation trailer ([source: ...; added: ...]) is metadata, not content. Including it made
# unrelated entries look ~0.45 similar to each other; measured on the live repo 2026-10-16 every
# false positive came from those few boilerplate tokens (source/added/2026/10/13). Compare content only.
TRAILER_RES = (
    re.compile(r"\[source:[^\[\]]*\]"),
    re.compile(r"\[added:[^\[\]]*\]"),
    re.compile(r"[（(]相关度[^）)]*[）)]"),
    re.compile(r"\[(?:手动|自主|推断|未验证)\]"),
)
MIN_SHARED_TOKENS = 2      # true restatements share >=2 content tokens; trailer-only FP pairs share <=1
BOILERPLATE_TOKENS = {"source", "added", "ses", "refs", "交接", "plans"}
SIG_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]{2,}|\d[\d.]{1,}|[\u4e00-\u9fff]{2,}")
INLINE_MIN_CLAUSE = 12     # ignore short fragments (list items, ids, tier markers)
INLINE_SIM = 0.85          # near-verbatim within one entry (fuzzy paraphrase is the cortex's job)


def strip_code(text: str) -> str:
    """Drop fenced blocks and inline code spans: [[...]] inside them are examples, not links."""
    return INLINE_CODE_RE.sub(" ", FENCE_RE.sub(" ", text))



# ---------------------------------------------------------------- deterministic layer
def iter_md(repo: Path):
    for p in sorted(repo.rglob("*.md")):
        rel = p.relative_to(repo)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        yield p, rel


def scan(repo: Path) -> dict:
    files, links, no_source, bullets = [], [], [], 0
    for p, rel in iter_md(repo):
        text = p.read_text(encoding="utf-8", errors="replace")
        rel_s = rel.as_posix()
        files.append(rel_s)
        for m in LINK_RE.finditer(strip_code(text)):
            target = m.group(1).strip()
            if target in PLACEHOLDER_LINKS:
                continue
            links.append((rel_s, target))
        if rel_s not in NON_ENTRY_FILES:
            for i, line in enumerate(text.splitlines(), 1):
                if BULLET_RE.match(line):
                    bullets += 1
                    if not SOURCE_RE.search(line):
                        no_source.append(f"{rel_s}:{i}")
    return {"files": files, "links": links, "no_source": no_source, "bullets": bullets}


def resolve_link(repo: Path, target: str) -> bool:
    t = target.strip()
    if not t or t.startswith(("http://", "https://")):
        return True
    for cand in (repo / t, repo / (t + ".md"), repo / t / "README.md"):
        if cand.exists():
            return True
    return False


def check_links(repo: Path, links) -> list:
    return [f"{src} -> [[{tgt}]]" for src, tgt in links if not resolve_link(repo, tgt)]


def check_memory(repo: Path) -> dict:
    f = repo / "MEMORY.md"
    if not f.exists():
        return {"exists": False, "lines": 0, "bytes": 0, "ok": False}
    raw = f.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    lines = text.count("\n") + (0 if text.endswith("\n") else 1)
    return {
        "exists": True,
        "lines": lines,
        "bytes": len(raw),
        "ok": lines <= LIMITS["memory_max_lines"] and len(raw) <= LIMITS["memory_max_bytes"],
    }


def find_orphans(repo: Path, files, links) -> list:
    """Topic files never referenced by any [[link]] and not named in MEMORY.md."""
    targets = set()
    for _, t in links:
        t = t.strip()
        targets.add(t)
        targets.add(t.removesuffix(".md"))
    memory = ""
    mp = repo / "MEMORY.md"
    if mp.exists():
        memory = mp.read_text(encoding="utf-8", errors="replace")
    orphans = []
    for rel in files:
        if rel in CORE_FILES:
            continue
        stem = rel[:-3] if rel.endswith(".md") else rel
        if stem in targets or rel in targets:
            continue
        if rel in memory or stem in memory:
            continue
        orphans.append(rel)
    return orphans


def iter_entries(repo: Path) -> list:
    """Memory entries = bullets in entry files (spec docs excluded)."""
    out = []
    for p, rel in iter_md(repo):
        rel_s = rel.as_posix()
        if rel_s in NON_ENTRY_FILES:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            if BULLET_RE.match(line):
                out.append({"file": rel_s, "line": i, "raw": line.strip(),
                            "text": re.sub(r"^-\s+", "", line.strip())})
    return out


def _norm(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", s.lower())


def strip_meta(text: str) -> str:
    """Content only: drop `[source: ...]` / `[added: ...]` / `（相关度…）` / tier markers.

    Measured 2026-10-16: those trailers alone pushed unrelated entries to sim 0.40-0.47 and
    produced 100% of the candidate false positives (plus they are pure noise for duplicates).
    """
    s = text or ""
    for rx in TRAILER_RES:
        s = rx.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def sig_tokens(text: str) -> set:
    """Significant content tokens (ascii ids / numbers / CJK runs), boilerplate removed."""
    out = set()
    for m in SIG_TOKEN_RE.findall(text or ""):
        t = m.lower()
        if len(t) < 2 or t in BOILERPLATE_TOKENS:
            continue
        out.add(t)
    return out


def _entry_raw(e) -> str:
    return e.get("raw") or e.get("text") or ""


def link_targets(raw: str) -> set:
    return {t.strip() for t in LINK_RE.findall(raw or "")}


def is_pointer_detail(a, b) -> bool:
    """True for `short entry + [[link]] to the other entry's file` = the intended MEMORY.md
    shape (CONVENTIONS: one place states it, everywhere else links). Not a duplicate."""
    for x, y in ((a, b), (b, a)):
        f = y.get("file", "") or ""
        stem = f[:-3] if f.endswith(".md") else f
        if not stem:
            continue
        for t in link_targets(_entry_raw(x)):
            if t and (t == stem or stem.endswith(t)):
                return True
    return False


def _is_citation_clause(s: str) -> bool:
    """A clause whose content is essentially a path / citation.

    Measured 2026-10-16 on the live repo: entries echo their own handover/refs paths, and those
    echoes scored 0.61-0.90 against each other - pure noise. Drop code spans and path runs, then
    see what is left; if almost nothing is, it is a citation, not a duplicated statement.
    """
    r = INLINE_CODE_RE.sub(" ", s or "")
    r = re.sub(r"[A-Za-z0-9_.\-]*[\\/][^\s，。；、)）]*", " ", r)
    res = re.sub(r"[\s\W_]+", "", r)
    orig = re.sub(r"[\s\W_]+", "", INLINE_CODE_RE.sub(" ", s or ""))
    return len(res) < 8 or len(res) < 0.5 * max(1, len(orig))


def inline_duplicates(entries) -> list:
    """Within-entry near-verbatim clauses (the 4a 'MEMORY.md:7 inline dedup' class).

    Deliberately separate from candidate_pairs and *report-only*: these findings never reach the
    cortex and never become write actions (the apply whitelist stays merge/archive). A clause pair
    is reported when both clauses are >=12 chars of real content (not citations), and they are
    near-verbatim (sim >= 0.85) - fuzzy paraphrase inside one entry is the cortex's job, not this.
    """
    import difflib
    sent_re = re.compile(r"[^。；;!?！？\n]+")
    findings = []
    for e in entries:
        body = strip_meta(e["text"])
        parts = [s.strip() for s in sent_re.findall(body) if len(s.strip()) >= INLINE_MIN_CLAUSE]
        hits = []
        for i in range(len(parts)):
            for j in range(i + 1, len(parts)):
                if _is_citation_clause(parts[i]) or _is_citation_clause(parts[j]):
                    continue
                r = difflib.SequenceMatcher(None, _norm(parts[i]), _norm(parts[j])).ratio()
                if r >= INLINE_SIM:
                    hits.append({"sim": round(r, 3), "left": parts[i], "right": parts[j]})
        if hits:
            findings.append({"file": e["file"], "line": e["line"], "clauses": len(parts), "hits": hits})
    return findings


def candidate_pairs(entries, threshold: float = 0.45, top: int = 8,
                    min_shared: int = MIN_SHARED_TOKENS) -> list:
    """Deterministic candidate generation: near-duplicate / restated entry pairs.

    4c-3 follow-up denoise (evidence: measured on the live git-memory repo, 2026-10-16):
      * compare content only (strip_meta) - the `[source: ...; added: ...]` trailer alone made
        unrelated MEMORY.md entries score 0.40-0.47 and caused every false positive;
      * require >= min_shared significant tokens - similar *shape* on different facts is not a dup;
      * skip pointer<->detail pairs (short entry + [[link]] to the detail file) - the shape we want.
    After the fix the strongest surviving pair on the live repo scores 0.377 (it is pointer/detail),
    i.e. the default 0.45 yields zero candidates - which is the honest answer for a tidy repo.
    """
    import difflib
    pairs = []
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            a, b = entries[i], entries[j]
            if a["file"] == b["file"] and abs(a["line"] - b["line"]) <= 1:
                continue
            if is_pointer_detail(a, b):
                continue
            ca, cb = strip_meta(a["text"]), strip_meta(b["text"])
            shared = sig_tokens(ca) & sig_tokens(cb)
            if len(shared) < min_shared:
                continue
            r = difflib.SequenceMatcher(None, _norm(ca), _norm(cb)).ratio()
            if r >= threshold:
                pairs.append({"a": a, "b": b, "sim": round(r, 3), "shared": len(shared)})
    pairs.sort(key=lambda x: -x["sim"])
    return pairs[:top]


# ---------------------------------------------------------------- engine (cortex) probe
def engine_probe(base_url: str, timeout: float = 5.0) -> dict:
    url = base_url.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read(4096).decode("utf-8", errors="replace")
        try:
            ids = [m.get("id") for m in json.loads(body).get("data", [])]
        except Exception:
            ids = []
        return {"ok": True, "url": url, "models": ids, "detail": "http 200"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "url": url, "models": [], "detail": f"{type(e).__name__}: {e}"}


ADJUDICATE_PROMPT = """你是记忆仓库的整理裁决器。仓库规约（节选自 CONVENTIONS.md）：
- 同一事实只在一处陈述，别处用 [[链接]] 引用
- 条目以 `- ` 开头，带 [source: ...] 来源标注；不得删除或改动来源标注
- 被取代的过期条目移入 archive/（不物理删除）
- 不确定 → 只标记，交人裁决

候选条目（可能重复 / 互相取代 / 矛盾）：
A=[{a_file}:{a_line}] {a_text}

B=[{b_file}:{b_line}] {b_text}

只输出一个 JSON 对象（不要解释、不要 markdown 代码块）：
{{"action":"merge|archive|flag","keep":"A|B|none","merged":"<仅 action=merge 时填合并后条目>","archive":"A|B|none","rationale":"<一句理由>"}}

硬约束：merged 只能由 A/B 的原文片段重组，**禁止新增任何事实、数字、日期、路径、来源**；不得改动 [source: ...]。"""


def llm_chat(base_url: str, prompt: str, model: str = "", timeout: float = 300.0,
             max_tokens: int = 900, enable_thinking: bool = False) -> dict:
    """Cortex call. Thinking stays OFF by default: on 2026-10-16 the engine burned the whole
    max_tokens budget in reasoning_content and returned content=null (finish_reason=length)."""
    payload = {"messages": [{"role": "user", "content": prompt}], "temperature": 0,
               "max_tokens": max_tokens, "stream": False,
               "chat_template_kwargs": {"enable_thinking": bool(enable_thinking)}}
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
                "reasoning": msg.get("reasoning_content") or "",
                "finish_reason": ch.get("finish_reason", ""), "usage": body.get("usage", {})}
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError) as e:
        return {"ok": False, "content": "", "reasoning": "", "finish_reason": "",
                "detail": f"{type(e).__name__}: {e}"}


def parse_action(content: str) -> dict:
    """Tolerant JSON extraction from the model reply."""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {"ok": False, "detail": "no JSON object in reply"}
    try:
        obj = json.loads(m.group(0))
    except ValueError as e:
        return {"ok": False, "detail": f"bad JSON: {e}"}
    if obj.get("action") not in {"merge", "archive", "flag"}:
        return {"ok": False, "detail": f"bad action: {obj.get('action')!r}"}
    return {"ok": True, "obj": obj}


def merge_guard(merged: str, sources: list) -> list:
    """No-new-facts guard: every sentence of a merged entry must occur in the sources.
    Returns the list of offending sentences (empty = clean)."""
    src = _norm(" ".join(sources))
    clean = _norm(merged)
    if not clean:
        return []
    violations = []
    for sent in [s for s in re.split(r"[。；;！!？?，,、]\s*", merged) if _norm(s)]:
        if _norm(sent) not in src:  # every clause must exist verbatim in the sources
            violations.append(sent.strip()[:80])
    return violations


def adjudicate(candidates, engine: dict, base_url: str, log, max_calls: int = 8) -> dict:
    """4c-2 adjudication layer: ask the cortex to merge / archive / flag each candidate pair.
    Never touches the repo; the result is proposal-only (and guard-checked)."""
    if not engine.get("ok"):
        return {"implemented": True, "ran": False, "items": [],
                "note": "engine unreachable -> adjudication skipped (fail-soft)"}
    model = (engine.get("models") or [""])[0]
    items, stats = [], {"merge": 0, "archive": 0, "flag": 0, "guard_rejected": 0, "errors": 0}
    for c in candidates[:max_calls]:
        prompt = ADJUDICATE_PROMPT.format(a_file=c["a"]["file"], a_line=c["a"]["line"],
                                          a_text=c["a"]["text"], b_file=c["b"]["file"],
                                          b_line=c["b"]["line"], b_text=c["b"]["text"])
        res = llm_chat(base_url, prompt, model=model)
        item = {"pair": f"{c['a']['file']}:{c['a']['line']} <-> {c['b']['file']}:{c['b']['line']}",
                "sim": c["sim"], "engine_ok": res["ok"], "a": c["a"], "b": c["b"]}
        if not res["ok"]:
            item.update({"action": "error", "rationale": res["detail"]})
            stats["errors"] += 1
            log(f"adjudicate engine error: {res['detail']}")
        elif not res["content"].strip():
            usage = res.get("usage", {}) or {}
            item.update({"action": "error",
                         "rationale": f"empty content (finish={res.get('finish_reason')}, "
                                      f"reasoning_tokens={usage.get('completion_tokens_details', {}).get('reasoning_tokens')})"})
            stats["errors"] += 1
            log(f"adjudicate empty content: {item['rationale']}")
        else:
            parsed = parse_action(res["content"])
            if not parsed["ok"]:
                item.update({"action": "error", "rationale": f"unparsable: {parsed['detail']}"})
                stats["errors"] += 1
            else:
                obj = parsed["obj"]
                action = obj["action"]
                guard = []
                if action == "merge":
                    guard = merge_guard(obj.get("merged", ""),
                                        [c["a"]["text"], c["b"]["text"]])
                    if guard:
                        action = "flag"
                        obj["rationale"] = (obj.get("rationale", "") +
                                            " | 守门拒绝：合并文本含源文外内容，已降级为标记")
                        stats["guard_rejected"] += 1
                item.update({"action": action, "keep": obj.get("keep", ""),
                             "archive": obj.get("archive", "none"),
                             "merged": obj.get("merged", ""), "rationale": obj.get("rationale", ""),
                             "guard_violations": guard,
                             "tokens": res.get("usage", {})})
                stats[action] = stats.get(action, 0) + 1
        items.append(item)
        log(f"adjudicate {item['pair']} -> {item['action']}")
    return {"implemented": True, "ran": True, "model": model, "items": items, "stats": stats,
            "note": f"candidates={len(candidates)} judged={len(items)} (cap {max_calls})"}


# ---------------------------------------------------------------- repo self-check (git)
def git_status(repo: Path) -> dict:
    """Read-only repo state check. safe.directory is set because the repo is root-owned
    while the job runs as an unprivileged user (same trick the PC-side mirror uses)."""
    def run(*args):
        try:
            out = subprocess.run(["git", "-c", f"safe.directory={repo}", "-C", str(repo), *args],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=30)
            return out.returncode, out.stdout.strip()
        except Exception as e:  # noqa: BLE001
            return 1, f"{type(e).__name__}: {e}"
    rc_h, head = run("rev-parse", "HEAD")
    rc_s, status = run("status", "--porcelain")
    return {"is_git": rc_h == 0, "head": head if rc_h == 0 else "", "status": status if rc_s == 0 else ""}


# ---------------------------------------------------------------- apply layer (4c-3)
def plan_ops(adjud: dict) -> list:
    """Turn guard-clean adjudicated items into whitelisted apply ops (merge / archive).

    Only actions the cortex returned with confidence + a passing no-new-facts guard qualify.
    `flag` (and everything else) stays proposal-only -> never written. Deterministic, no LLM.
    """
    ops = []
    for it in adjud.get("items", []) or []:
        a, b = it.get("a"), it.get("b")
        if not a or not b:
            continue
        action = it.get("action")
        if action == "merge" and it.get("merged"):
            keep = (it.get("keep") or "").strip().upper()
            if keep not in ("A", "B"):
                continue
            kept, dropped = (a, b) if keep == "A" else (b, a)
            if (kept["file"], kept["line"]) == (dropped["file"], dropped["line"]):
                continue
            merged = it["merged"].strip()
            if not merged:
                continue
            arch = (it.get("archive") or "").strip().upper()
            archive_dropped = (arch == "A" and dropped is a) or (arch == "B" and dropped is b)
            ops.append({"type": "merge", "pair": it.get("pair", ""),
                        "file": kept["file"], "line": int(kept["line"]),
                        "new": "- " + merged.lstrip("-").strip(),
                        "drop_file": dropped["file"], "drop_line": int(dropped["line"]),
                        "archive_dropped": archive_dropped, "merged": merged})
        elif action == "archive":
            which = (it.get("archive") or "").strip().upper()
            target = a if which == "A" else b if which == "B" else None
            if target is None:
                continue
            ops.append({"type": "archive", "pair": it.get("pair", ""),
                        "file": target["file"], "line": int(target["line"])})
    return ops


def execute_ops(repo: Path, ops: list, log) -> dict:
    """Apply merge / archive ops to the worktree (no git yet).

    Entries archived go to `archive/<original path>` (history preserved, per CONVENTIONS).
    Line edits are applied bottom-up so earlier edits never shift later line numbers.
    """
    per_file = {}
    applied = []
    for op in ops:
        if op["type"] == "merge":
            per_file.setdefault(op["file"], []).append((op["line"], op["new"]))
            if op["archive_dropped"]:
                per_file.setdefault(op["drop_file"], []).append((op["drop_line"], "ARCHIVE"))
            else:
                per_file.setdefault(op["drop_file"], []).append((op["drop_line"], None))
            applied.append({"op": "merge", "pair": op["pair"],
                            "kept": f"{op['file']}:{op['line']}", "merged": op["merged"],
                            "dropped": f"{op['drop_file']}:{op['drop_line']}",
                            "dropped_disposition": "archived" if op["archive_dropped"] else "removed"})
        elif op["type"] == "archive":
            per_file.setdefault(op["file"], []).append((op["line"], "ARCHIVE"))
            applied.append({"op": "archive", "pair": op["pair"], "entry": f"{op['file']}:{op['line']}",
                            "dropped_disposition": "archived"})

    changed, archive_files, skipped = [], [], 0
    for rel, edits in per_file.items():
        p = repo / rel
        if not p.exists():
            log(f"apply WARN: {rel} missing, op skipped")
            skipped += 1
            continue
        lines = p.read_text(encoding="utf-8").splitlines()
        archived_raw = []
        for line, new in sorted(edits, key=lambda x: -x[0]):
            if not (1 <= line <= len(lines)):
                log(f"apply WARN: {rel}:{line} out of range, skipped")
                skipped += 1
                continue
            if new is None:
                lines.pop(line - 1)
            elif new == "ARCHIVE":
                archived_raw.append(lines.pop(line - 1))
            else:
                lines[line - 1] = new
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        changed.append(rel)
        if archived_raw:
            ap = repo / "archive" / rel
            ap.parent.mkdir(parents=True, exist_ok=True)
            old = ap.read_text(encoding="utf-8") if ap.exists() else ""
            add = [r for r in archived_raw if r.strip() and r not in old]
            if add:
                with ap.open("a", encoding="utf-8") as fh:
                    if not ap.exists() or not old:
                        fh.write(f"# archive -- {rel}\n\n> entries retired by the dream job "
                                 f"(moved here to preserve history)\n\n")
                    fh.write("\n".join(add) + "\n")
                archive_files.append((Path("archive") / rel).as_posix())
    return {"applied": applied, "changed_files": changed, "archive_files": archive_files,
            "skipped": skipped}


def build_subject(day: str, ops: list) -> str:
    m = sum(1 for o in ops if o["type"] == "merge")
    a = sum(1 for o in ops if o["type"] == "archive")
    if m or a:
        parts = []
        if m:
            parts.append(f"合并 {m} 条")
        if a:
            parts.append(f"归档 {a} 条")
        return f"dream: {' / '.join(parts)}（报告 dreams/{day}.md）"
    return f"dream: 仓库健康（报告 dreams/{day}.md）"


def git_commit(repo: Path, message: str, log) -> dict:
    """Stage everything and create AT MOST ONE commit. No-op when the tree already matches HEAD."""
    def run(*args):
        try:
            return subprocess.run(["git", "-c", f"safe.directory={repo}",
                                   "-c", f"user.name={DREAM_AUTHOR_NAME}",
                                   "-c", f"user.email={DREAM_AUTHOR_EMAIL}",
                                   "-C", str(repo), *args],
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=60)
        except Exception as e:  # noqa: BLE001
            return subprocess.CompletedProcess(args, 1, "", f"{type(e).__name__}: {e}")
    run("add", "-A")
    staged = run("diff", "--cached", "--name-only").stdout.strip()
    if not staged:
        return {"committed": False, "reason": "nothing to commit (no-op)"}
    c = run("commit", "-m", message)
    if c.returncode != 0:
        return {"committed": False, "reason": (c.stderr or c.stdout).strip()}
    sha = run("rev-parse", "HEAD").stdout.strip()
    return {"committed": True, "sha": sha, "subject": message, "files": staged.splitlines()}


# ---------------------------------------------------------------- report
def render_report(mode, day, repo, scan_res, broken, memory, orphans, engine, adjud, before, after,
                  ops=None, applied=None, subject="", inline=None):
    L = []
    L.append(f"# Dream 报告（{mode}） {day}")
    L.append("")
    L.append(f"- 模式：`{mode}`" + ("（只读，仓库零改动）" if mode == "dry-run" else "（写库）"))
    L.append(f"- 仓库：`{repo}`")
    L.append(f"- 引擎：`{engine['url']}` → " + ("可达 " + str(engine["models"]) if engine["ok"] else "**不可达（降级为仅确定性层）** " + engine["detail"]))
    L.append("")
    L.append("## 1. 仓库概览")
    L.append(f"- markdown 文件：{len(scan_res['files'])}")
    L.append(f"- 条目（`- ` 开头）：{scan_res['bullets']}")
    L.append(f"- `[[链接]]`：{len(scan_res['links'])}")
    L.append("")
    L.append("## 2. 确定性检查")
    if memory["exists"]:
        L.append(f"- MEMORY.md：{memory['lines']} 行 / {memory['bytes']} B（限值 ≤{LIMITS['memory_max_lines']} 行 / ≤{LIMITS['memory_max_bytes']} B）→ " + ("✅ 合规" if memory["ok"] else "❌ 超限"))
    else:
        L.append("- MEMORY.md：❌ 缺失")
    L.append(f"- 失效链接：{len(broken)}" + ("" if not broken else " → " + "; ".join(broken[:MAX_LISTED])))
    L.append(f"- 无来源标注条目：{len(scan_res['no_source'])}" + ("" if not scan_res["no_source"] else " → " + "; ".join(scan_res["no_source"][:MAX_LISTED])))
    L.append(f"- 孤条目（无链接、未列入 MEMORY.md）：{len(orphans)}" + ("" if not orphans else " → " + "; ".join(orphans[:MAX_LISTED])))
    L.append("")
    L.append("### 2.1 行内近重复子句（确定性层，供人工裁决）")
    inline = inline or []
    if not inline:
        L.append(f"- 未发现（判据：子句 ≥{INLINE_MIN_CLAUSE} 字符、近逐字 sim ≥{INLINE_SIM}、排除纯路径/引用子句）")
    else:
        L.append(f"- 命中 {len(inline)} 条条目 / {sum(len(f['hits']) for f in inline)} 处——**仅供人工裁决**，本 job 不改写")
        L.append("")
        L.append("| 位置 | 子句数 | sim | 子句 A | 子句 B |")
        L.append("|---|---|---|---|---|")
        for f in inline:
            for h in f["hits"]:
                L.append(f"| {f['file']}:{f['line']} | {f['clauses']} | {h['sim']} | "
                         f"{(h['left'] or '').replace('|','\\|')[:90]} | {(h['right'] or '').replace('|','\\|')[:90]} |")
    L.append("")
    L.append("## 3. 裁决层（调皮层）")
    if not adjud.get("implemented"):
        L.append(f"- 未启用：{adjud.get('note', '')}")
    elif not adjud.get("ran"):
        L.append(f"- **未运行**：{adjud.get('note', '')}")
    else:
        st = adjud.get("stats", {})
        L.append(f"- 引擎：可达，模型 `{adjud.get('model', '')}`；{adjud.get('note', '')}")
        L.append(f"- 统计：merge={st.get('merge',0)} / archive={st.get('archive',0)} / flag={st.get('flag',0)}"
                 f" / 守门拒绝={st.get('guard_rejected',0)} / 错误={st.get('errors',0)}")
        if mode == "apply":
            L.append("- `merge`/`archive` 为白名单动作（本轮落库）；`flag` 仅建议、从不写库；合并类建议已过「无新造事实」守门")
        else:
            L.append("- 全部为**建议**：本轮 dry-run **未落库**、未改动任何文件；合并类建议已过「无新造事实」守门")
        if adjud.get("items"):
            L.append("")
            L.append("| # | 候选对 | sim | 模型建议 | 守门 | 理由 / 合并文本 |")
            L.append("|---|---|---|---|---|---|")
            for i, it in enumerate(adjud["items"], 1):
                note = (it.get("rationale") or "").replace("|", "\\|")[:120]
                if it.get("action") == "merge" and it.get("merged"):
                    note += f" ｜ 合并文本：「{(it['merged'] or '').replace('|','\\|')[:200]}」"
                if it.get("guard_violations"):
                    note += f" ｜ 越界片段：{'; '.join(it['guard_violations'])[:120]}"
                guard = "—" if it.get("action") != "flag" or not it.get("guard_violations") else "**拒绝→标记**"
                L.append(f"| {i} | {it.get('pair','').replace('|','\\|')} | {it.get('sim','')} | "
                         f"{it.get('action','')} | {guard} | {note} |")
    L.append("")
    if mode == "apply":
        L.append("## 4. 落库（apply）")
        L.append(f"- 白名单动作：`merge`（守门通过）/ `archive`（移入 `archive/<原文件>` 保历史）；`flag` 仅建议")
        L.append(f"- 计划动作数：{len(ops or [])}")
        if applied:
            L.append("")
            L.append("| # | 动作 | 保留/归档位置 | 处置 | 合并文本 |")
            L.append("|---|---|---|---|---|")
            for i, ap in enumerate(applied, 1):
                loc = ap.get("kept") or ap.get("entry") or ""
                if ap.get("dropped"):
                    loc += f" ← {ap['dropped']}"
                merged = (ap.get("merged") or "").replace("|", "\\|")[:160]
                L.append(f"| {i} | {ap.get('op','')} | {loc} | {ap.get('dropped_disposition','')} "
                         f"| {merged} |")
        else:
            L.append("- 无落库动作（候选均为建议 / 守门拒绝 / 引擎不可达）——仓库健康，仅提交本报告")
        L.append(f"- 本次提交：`{subject}`（报告随该 commit 入库）")
    else:
        L.append("## 4. 仓库零改动自检")
        if before["is_git"]:
            same = before["head"] == after["head"] and before["status"] == after["status"]
            L.append(f"- HEAD：`{after['head'][:12]}`（跑前 `{before['head'][:12]}`）")
            L.append(f"- `git status --porcelain`：{'（空）' if not after['status'] else after['status']}")
            L.append(f"- 结论：{'✅ PASS（仓库未改动）' if same else '❌ FAIL（仓库发生变化）'}")
        else:
            L.append("- 非 git 仓库，跳过")
    L.append("")
    L.append("## 5. 结论")
    issues = (1 if not memory.get("ok", False) else 0) + len(broken) + len(scan_res["no_source"]) + len(orphans)
    L.append(f"- 待处理项：{issues}" + ("（仓库健康）" if issues == 0 else ""))
    if mode == "apply":
        L.append(f"- 本轮 apply：白名单落库 {len(applied or [])} 项 + 报告入仓；余下待处理项（失效链接 / 无来源 / 孤条目 / MEMORY.md 超限）仅记录，交人裁决。")
    else:
        L.append("- 报告由确定性层（4c-1）+ 裁决层（4c-2）生成；本轮**未做任何仓库改动**（dry-run），落库属 4c-3。")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Hippocampus dream job (4c-3 dry-run/apply)")
    ap.add_argument("--mode", choices=["dry-run", "apply"], default="dry-run")
    ap.add_argument("--repo", default="/opt/hippocampus/git-memory")
    ap.add_argument("--engine", default="http://127.0.0.1:18888/v1")
    ap.add_argument("--out", default="/opt/hippocampus/dream/reports")
    ap.add_argument("--log", default="/opt/hippocampus/dream/logs")
    ap.add_argument("--date", default=None, help="override date (YYYY-MM-DD), for tests")
    ap.add_argument("--sim-threshold", type=float, default=0.45, help="candidate-pair similarity floor")
    ap.add_argument("--max-candidates", type=int, default=8, help="max candidate pairs to judge per run")
    ap.add_argument("--min-shared", type=int, default=MIN_SHARED_TOKENS,
                    help="min shared significant tokens for a candidate pair (denoise)")
    args = ap.parse_args(argv)

    repo, out, logd = Path(args.repo), Path(args.out), Path(args.log)
    day = args.date or dt.datetime.now().astimezone().strftime("%Y-%m-%d")
    out.mkdir(parents=True, exist_ok=True)
    logd.mkdir(parents=True, exist_ok=True)
    logf = logd / f"dream-{day}.log"

    def log(msg):
        stamp = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{stamp}] {msg}"
        print(line)
        with logf.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    log(f"dream job start mode={args.mode} repo={repo}")
    if not (repo / "MEMORY.md").exists():
        log(f"ERROR: repo not found or incomplete: {repo}")
        return 1

    before = git_status(repo)
    if args.mode == "apply":
        if not before["is_git"]:
            log("ERROR: apply mode requires a git repo (rollback relies on git). Exiting 1.")
            return 1
        if before["status"]:
            log(f"ERROR: repo is dirty before apply ({before['status'][:200]!r}); refusing to write (fail-soft).")
            return 0

    engine = engine_probe(args.engine)
    log(f"engine probe: ok={engine['ok']} {engine['detail']}")

    scan_res = scan(repo)
    broken = check_links(repo, scan_res["links"])
    memory = check_memory(repo)
    orphans = find_orphans(repo, scan_res["files"], scan_res["links"])
    entries = iter_entries(repo)
    candidates = candidate_pairs(entries, args.sim_threshold, top=args.max_candidates,
                                 min_shared=args.min_shared)
    log(f"entries={len(entries)} candidate_pairs={len(candidates)} "
        f"(threshold={args.sim_threshold}, min_shared={args.min_shared})")
    inline = inline_duplicates(entries)
    log(f"inline duplicate clauses: entries={len(inline)} hits={sum(len(f['hits']) for f in inline)}")
    adjud = adjudicate(candidates, engine, args.engine, log, max_calls=args.max_candidates)

    if args.mode == "dry-run":
        after = git_status(repo)
        report = render_report(args.mode, day, repo, scan_res, broken, memory, orphans, engine, adjud,
                               before, after, inline=inline)
        rep = out / f"{day}.md"
        rep.write_text(report, encoding="utf-8")
        log(f"report written: {rep}")
        if before["is_git"] and (before["head"] != after["head"] or before["status"] != after["status"]):
            log("ERROR: repo changed during dry-run (self-check FAIL)")
            return 2
        log(f"dream job done (dry-run) files={len(scan_res['files'])} links={len(scan_res['links'])} "
            f"broken={len(broken)} no_source={len(scan_res['no_source'])} orphans={len(orphans)}")
        return 0

    # ---- apply (4c-3): whitelisted ops + report in-repo + AT MOST ONE commit ----
    ops = plan_ops(adjud)
    log(f"apply: planned ops={len(ops)} ({', '.join(sorted({o['type'] for o in ops})) or 'none'})")
    exec_res = execute_ops(repo, ops, log) if ops else \
        {"applied": [], "changed_files": [], "archive_files": [], "skipped": 0}
    log(f"apply: executor applied {len(exec_res['applied'])} applied, skipped {exec_res['skipped']}, "
        f"files={exec_res['changed_files']}")

    subject = build_subject(day, ops)
    after_edit = git_status(repo)
    report = render_report("apply", day, repo, scan_res, broken, memory, orphans, engine, adjud,
                           before, after_edit, ops=ops, applied=exec_res["applied"], subject=subject,
                           inline=inline)
    rep = repo / "dreams" / f"{day}.md"
    rep.parent.mkdir(parents=True, exist_ok=True)
    rep.write_text(report, encoding="utf-8")
    (out / f"{day}.md").write_text(report, encoding="utf-8")  # observability copy (outside the repo)
    log(f"apply: report written {rep} (+ copy {out / (day + '.md')})")

    commit = git_commit(repo, subject, log)
    if commit.get("committed"):
        log(f"apply: commit {commit['sha'][:12]} -- {subject} (files={commit['files']})")
    else:
        log(f"apply: NO commit -- {commit.get('reason')}")

    after = git_status(repo)
    if commit.get("committed") and before["head"]:
        rc = subprocess.run(["git", "-c", f"safe.directory={repo}", "-C", str(repo),
                             "rev-list", "--count", f"{before['head']}..{after['head']}"],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=30)
        cnt = rc.stdout.strip()
        log(f"apply: commits since before = {cnt} (expect exactly 1)")
        if cnt != "1":
            log("ERROR: expected exactly one commit (self-check FAIL)")
            return 2
    if after["status"]:
        log(f"ERROR: worktree not clean after apply: {after['status'][:200]!r}")
        return 2
    log(f"apply done: head={after['head'][:12]} ops={len(ops)} applied={len(exec_res['applied'])} "
        f"committed={commit.get('committed')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
