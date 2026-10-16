"""Memory gateway（阶段 5a）：recall 管线 + remember + REST 兜底 + MCP /mcp。

recall 管线逐函数移植自 opencode 插件 ~/.config/opencode/plugin/mem0-gb10.ts
（rankedItems / rerankScores / applySupersede / formatInjectedLines / labelOf 等），
阈值与权重以插件现役默认为准。检索走进程内 get_memory_instance()（不绕 HTTP 自调），
remember 复用 main.add_memory（带 ingest_guard 去重/低价值管线）。

配置外置 server/gateway_config.json（不入库，见 gateway_config.example.json）；
无硬编码本机路径，可在其它机器按文档拉起。
"""

import asyncio
import contextlib
import json
import logging
import math
import re
import socket
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server_state import get_memory_instance

import lexical

SERVER_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SERVER_DIR / "gateway_config.json"

# 默认值镜像 mem0-gb10.ts 现役默认（阈值 0.55/0.3、wRec 0.15、halfLife 14d、rerankTopN 15）。
DEFAULT_CONFIG: Dict[str, Any] = {
    "userId": "demo",
    "topK": 5,
    "threshold": 0.55,
    "rerank": True,
    "rerankEndpoint": "http://127.0.0.1:8086",
    "rerankApiKey": "",
    "rerankModel": "bge-reranker-v2-m3",
    "rerankTopN": 15,
    "rerankTimeoutMs": 30000,
    "rerankThreshold": 0.3,
    "recencyWeight": 0.15,
    "recencyHalfLifeDays": 14,
    # supersede（失效标记）为可选后置步，MVP 默认关；与插件端 config 各自独立。
    "supersede": {
        "enabled": False,
        "embedEndpoint": "http://127.0.0.1:8084",
        "embedModel": "Qwen3-Embedding-0.6B",
        "embedTimeoutMs": 3000,
        "embedMaxChars": 500,
        "simThreshold": 0.85,
        "minAgeGapDays": 7,
        "penalty": 0.5,
    },
    # B2：词法旁路（SQLite FTS5 trigram）+ RRF 融合。默认关 = 零行为变化。
    "lexical": {
        "enabled": False,
        "dbPath": "",
        "denseTopK": 20,
        "lexTopK": 20,
        "rrfK": 60,
        "autoBuildMissing": True,
    },
    # 空 = 不鉴权（仅 loopback）；建议 tailnet 暴露前设置静态 token。
    "bearerToken": "",
    "rememberAgent": "gateway",
    # 阶段 5b：git-memory API。repo 为空 = 关闭（配置外置，无硬编码路径，见 gateway_config.example.json）。
    "gitMemory": {
        "enabled": True,
        "repo": "",
        "gitBin": "git",
        "defaultFile": "MEMORY.md",
        "commitPrefix": "memory",
    },
    # 双池（2026-10-17 上策）：短期池 = 工作记忆（对话原文/待巩固候选，有 TTL），
    # 长期池 = 巩固后的事实/决策/偏好/坑（无 TTL，靠 supersede 更新）。
    # 召回：两池都查，短期最多占 shortQuota 席，长期补足；阈值为长期不设绝对值。
    "pools": {
        "enabled": True,
        "shortQuota": 2,
        "shortThreshold": 0.35,
        "shortTtlHours": 72,
        "longTtlHours": 168,
        "longRelWindow": 0.20,
        "longMinScore": 0.30,
        "longTypes": ["decision", "rule", "fix", "preference", "config"],
        "shortSrcs": ["gateway"],
        "candidateFactor": 4,
    },
    # 沉淀提炼 job（2026-10-17）：短期池原文 → 本地模型提炼 → 长期池。
    "consolidate": {
        "enabled": True,
        "maxEntries": 20,
        "maxChars": 6000,
        "maxCalls": 8,
        "endpoint": "http://127.0.0.1:18888/v1",
        "model": "GLM-5.3-Flash-EXL3",
        "timeoutMs": 120000,
    },
}


def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    return cfg


CFG = load_config()
MACHINE = socket.gethostname()

# 已带统一来源前缀的正文不再重复加（与插件 SRC_PREFIX_RE 对齐，扩展 gateway）。
SRC_PREFIX_RE = re.compile(
    r"^\[(?:opencode|dsh|hermes|gateway|consolidate)(?:[/@][^\]\n]*)?\]"
)
RERANK_DOC_MAX_CHARS = 6000
# 召回 query 的字符上限（治本 20261016）：query 超长会撑爆嵌入窗口 → 嵌入 400 →
# 器官 500 → 网关 recall 持续 fail-open。中文≈1 token/字，2000 字符 ≈ ≤2000 token，
# 远低于 hippo-embed 单槽窗口（原 4096，同日提到 16384）。可在 gateway_config.json
# 用 queryMaxChars 覆盖（0/缺省 = 用本默认）。
RECALL_QUERY_MAX_CHARS = 2000
BJ_TZ = timezone(timedelta(hours=8))
DATE_IN_TEXT_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
# 对话原文形态：同时出现「用户：」「助手：」（跨度限制，避免超长正文误判）。
CHAT_TURN_RE = re.compile(r"用户[:：].{0,6000}?助手[:：]", re.S)


# ---------------------------------------------------------------- 双池（2026-10-17）
def _pool_cfg() -> Dict[str, Any]:
    p = CFG.get("pools")
    return p if isinstance(p, dict) else {}


def _pool_enabled() -> bool:
    return _pool_cfg().get("enabled") is True


def _pool_short_srcs() -> set:
    return {str(x).strip().lower() for x in (_pool_cfg().get("shortSrcs") or [])}


def _pool_long_types() -> set:
    return {str(x).strip().lower() for x in (_pool_cfg().get("longTypes") or [])}


def classify_pool(
    text: str, metadata: Optional[Dict[str, Any]], src: Optional[str] = None
) -> str:
    """写入侧分池。优先级：显式 pool > 精选类型直进长期 > 透传网关/对话原文 → 短期 > 其余新信息 → 短期。

    用户规则：新的信息进短期池，沉淀（promote / 做梦）后进长期池。
    """
    md = metadata or {}
    explicit = str(md.get("pool") or "").strip().lower()
    if explicit in ("short", "long"):
        return explicit
    typ = str(md.get("type") or "").strip().lower()
    if typ and typ in _pool_long_types():
        return "long"
    s = str(src or md.get("src") or "").strip().lower()
    if s and s in _pool_short_srcs():
        return "short"
    if CHAT_TURN_RE.search(str(text or "")[:8000]):
        return "short"
    return "short"


def pool_of(r: Any, mem: str = "") -> str:
    """读取侧分池。无 pool 字段的存量条目：对话原文 → 短期，其余 → 长期
    （历史即长期，避免老条目被短期 TTL 误杀）。"""
    md = r.get("metadata") if isinstance(r, dict) else None
    md = md if isinstance(md, dict) else {}
    explicit = str(md.get("pool") or "").strip().lower()
    if explicit in ("short", "long"):
        return explicit
    src = str(md.get("src") or (r.get("agent_id") if isinstance(r, dict) else "") or "").strip().lower()
    if src and src in _pool_short_srcs():
        return "short"
    if CHAT_TURN_RE.search(str(mem or "")[:8000]):
        return "short"
    return "long"


def pool_expired(r: Any, pool: str) -> bool:
    """短期条过期判定：优先 pool_expires_at，否则按 created_at + TTL 推算。"""
    if pool != "short":
        return False
    md = r.get("metadata") if isinstance(r, dict) else None
    md = md if isinstance(md, dict) else {}
    exp = _parse_ts(md.get("pool_expires_at"))
    if exp is None:
        base = _parse_ts(r.get("created_at")) if isinstance(r, dict) else None
        if base is None:
            return False
        try:
            ttl_h = float(_pool_cfg().get("shortTtlHours") or 72)
        except (TypeError, ValueError):
            ttl_h = 72.0
        exp = base + timedelta(hours=ttl_h)
    return datetime.now(timezone.utc) > exp


def apply_pool_policy(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """分池配额 + 分池阈值：
    - 短期：绝对阈值 shortThreshold，最多 shortQuota 席（防原文刷屏），过期剔除；
    - 长期：不设绝对阈值，用相对窗 max(longMinScore, top - longRelWindow)（治"老事实被 0.55 切掉"）。
    """
    cfg = _pool_cfg()
    try:
        quota = max(0, int(cfg.get("shortQuota") or 0))
    except (TypeError, ValueError):
        quota = 0
    try:
        sthr = float(cfg.get("shortThreshold") or 0.0)
    except (TypeError, ValueError):
        sthr = 0.0
    try:
        win = float(cfg.get("longRelWindow") or 0.0)
    except (TypeError, ValueError):
        win = 0.0
    try:
        floor = float(cfg.get("longMinScore") or 0.0)
    except (TypeError, ValueError):
        floor = 0.0

    short_items: List[Dict[str, Any]] = []
    long_items: List[Dict[str, Any]] = []
    for it in items:
        pool = pool_of(it["r"], it["mem"])
        it["pool"] = pool
        if pool == "short":
            md = it["r"].get("metadata") if isinstance(it["r"], dict) else None
            md = md if isinstance(md, dict) else {}
            if str(md.get("pool_status") or "") == "archived":
                # 已沉淀归档：不再参与注入（数据留库可审计/可回溯）。
                continue
            if pool_expired(it["r"], pool):
                # 过期短期条：对话原文 → 不再进注入（留库可审计）；
                # 非对话形态（已接近事实）→ 读取时自动提升为长期，避免"事实被 TTL 杀掉"。
                if CHAT_TURN_RE.search(it["mem"] or ""):
                    it["expired"] = True
                    continue
                it["pool"] = "long"
                it["auto_promoted"] = True
                pool = "long"
            else:
                short_items.append(it)
                continue
        long_items.append(it)

    short_kept = [
        it
        for it in short_items
        if it["relevance01"] is not None and it["relevance01"] >= sthr
    ]
    short_kept.sort(key=lambda it: it["final"], reverse=True)
    short_kept = short_kept[:quota]

    if long_items:
        top_rel = max((it["relevance01"] or 0.0) for it in long_items)
        cutoff = max(floor, top_rel - win)
        long_kept = [
            it
            for it in long_items
            if it["relevance01"] is not None and it["relevance01"] >= cutoff
        ]
    else:
        long_kept = []

    merged = short_kept + long_kept
    merged.sort(key=lambda it: it["final"], reverse=True)
    return merged


def with_source_prefix(tag: str, text: str) -> str:
    s = (text or "").strip()
    if not s or SRC_PREFIX_RE.match(s):
        return s
    return f"[{tag}] {s}"


def label_of(hit: Dict[str, Any]) -> str:
    """从条目推断来源标签（给没带正文前缀的存量条目补标用），移植自插件 labelOf。"""
    if not isinstance(hit, dict):
        return ""
    md = hit.get("metadata")
    md = md if isinstance(md, dict) else {}
    if hit.get("agent_id"):
        return str(hit["agent_id"])
    if md.get("src"):
        return str(md["src"])
    if md.get("origin") == "dsh-mem-mcp" or md.get("dsh_id") or md.get("dsh_import_key"):
        return "dsh"
    return ""


def _parse_ts(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        dt = ts
    else:
        s = str(ts).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def age_in_days(ts: Any) -> Optional[float]:
    dt = _parse_ts(ts)
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0


def fmt_bj(ts: Any) -> Optional[str]:
    """展示时间一律北京时间 UTC+8（库内 created_at 存 UTC 属正常）。"""
    dt = _parse_ts(ts)
    if dt is None:
        return None
    return dt.astimezone(BJ_TZ).strftime("%Y-%m-%d %H:%M")


def date_from_text(mem: str) -> Optional[str]:
    m = DATE_IN_TEXT_RE.search(mem or "")
    return m.group(0) if m else None


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


async def rerank_scores(query: str, docs: List[str]) -> Optional[List[Optional[float]]]:
    """调交叉编码器 rerank（llama.cpp /v1/rerank）。返回与 docs 逐项对齐的 relevance01；
    任何失败返回 None（调用方回退向量序）。移植自插件 rerankScores。"""
    base = str(CFG.get("rerankEndpoint") or "").rstrip("/")
    if not base or not docs:
        return None
    headers = {"Content-Type": "application/json"}
    if CFG.get("rerankApiKey"):
        headers["Authorization"] = f"Bearer {CFG['rerankApiKey']}"
    payload = {
        "model": CFG.get("rerankModel") or "bge-reranker-v2-m3",
        "query": str(query),
        # 逐文档截断：单条记忆过长会顶爆 reranker 物理 batch（llama.cpp 直接 500）。
        "documents": [d[:RERANK_DOC_MAX_CHARS] for d in docs],
    }
    try:
        timeout = float(CFG.get("rerankTimeoutMs") or 30000) / 1000.0
        async with httpx.AsyncClient(timeout=timeout) as client:
            res = await client.post(base + "/v1/rerank", headers=headers, json=payload)
        if res.status_code != 200:
            return None
        data = res.json()
        arr = data.get("results") if isinstance(data, dict) else None
        if not isinstance(arr, list):
            return None
        raw: List[Optional[float]] = [None] * len(docs)
        for r in arr:
            if not isinstance(r, dict):
                continue
            v = r.get("relevance_score", None)
            if v is None:
                v = r.get("score")
            i = r.get("index")
            try:
                v = float(v)
                i = int(i)
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(raw):
                raw[i] = v
        finite = [v for v in raw if v is not None]
        if not finite:
            return None
        # bge-reranker 输出原始 logit（可为负、可 >1）→ 存在越界值则整体视为 logit 逐值 sigmoid。
        is_logit = any(v < 0 or v > 1 for v in finite)
        return [None if v is None else (_sigmoid(v) if is_logit else v) for v in raw]
    except Exception:
        return None


def _cos_sim(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    return dot / math.sqrt(na * nb) if na and nb else 0.0


async def _embed_texts(texts: List[str]) -> Optional[List[List[float]]]:
    """批量取嵌入向量（OpenAI 兼容 /v1/embeddings）。失败返回 None。"""
    sc = CFG.get("supersede") or {}
    base = str(sc.get("embedEndpoint") or "").rstrip("/")
    if not base or not texts:
        return None
    try:
        timeout = float(sc.get("embedTimeoutMs") or 5000) / 1000.0
        async with httpx.AsyncClient(timeout=timeout) as client:
            res = await client.post(
                base + "/v1/embeddings",
                json={
                    "model": sc.get("embedModel") or "Qwen3-Embedding-0.6B",
                    "input": [t[: int(sc.get("embedMaxChars") or 500)] for t in texts],
                },
            )
        if res.status_code != 200:
            return None
        rows = res.json().get("data")
        if not isinstance(rows, list) or len(rows) != len(texts):
            return None
        all_indexed = all(isinstance(d, dict) and isinstance(d.get("index"), int) for d in rows)
        out: List[Optional[List[float]]] = [None] * len(texts)
        for pos, d in enumerate(rows):
            if not (isinstance(d, dict) and isinstance(d.get("embedding"), list)):
                return None
            idx = d["index"] if all_indexed else pos
            if not (0 <= idx < len(out)):
                return None
            out[idx] = d["embedding"]
        if any(v is None for v in out):
            return None
        return out
    except Exception:
        return None


def _age_or_fallback(r: Dict[str, Any], mem: str) -> Optional[float]:
    """created_at 缺失时回退：metadata.created_at → 文本首个日期（移植自插件口径）。"""
    ca = r.get("created_at")
    if not ca:
        md = r.get("metadata")
        if isinstance(md, dict):
            ca = md.get("created_at")
    if not ca:
        ca = date_from_text(mem)
    return age_in_days(ca)


async def apply_supersede(items: List[Dict[str, Any]], t0: float, enabled: Optional[bool] = None) -> None:
    """两两比对召回条目：高相似 + 新旧时间差 → 旧条降权并记录 supersededBy（不删不隐）。

    enabled=None 用全局配置；True/False 为每请求覆盖（2026-10-13 5c：opencode 开 / DSH 关，各传各的）。
    """
    sc = CFG.get("supersede") or {}
    if enabled is None:
        enabled = sc.get("enabled") is not False
    if enabled is False or len(items) < 2:
        return
    sim_thr = float(sc.get("simThreshold") or 0.85)
    min_gap = float(sc.get("minAgeGapDays") or 7)
    penalty = float(sc.get("penalty") or 0.5)
    texts = []
    for it in items:
        s = SRC_PREFIX_RE.sub("", it["mem"], count=1).strip()
        texts.append(s or it["mem"])
    embs = await _embed_texts(texts)
    if embs is None:
        return
    marked = 0
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            age_a = _age_or_fallback(a["r"], a["mem"])
            age_b = _age_or_fallback(b["r"], b["mem"])
            if age_a is None or age_b is None or age_a < 0 or age_b < 0:
                continue
            if abs(age_a - age_b) < min_gap:
                continue
            va, vb = embs[i], embs[j]
            if not va or not vb or len(va) != len(vb):
                continue
            if _cos_sim(va, vb) < sim_thr:
                continue
            older, newer = (a, b) if age_a > age_b else (b, a)
            if older.get("superseded_by"):
                continue
            rid = newer["r"].get("id") if isinstance(newer["r"], dict) else None
            older["superseded_by"] = str(rid) if rid else "?"
            older["final"] *= penalty
            marked += 1


def _mem_text(r: Any) -> str:
    if isinstance(r, dict) and r.get("memory") is not None:
        return str(r["memory"])
    return json.dumps(r, ensure_ascii=False)


LOG = logging.getLogger("gateway.lexical")


def _lex_cfg() -> Dict[str, Any]:
    lx = dict(CFG.get("lexical") or {})
    lx.setdefault("enabled", False)
    lx.setdefault("dbPath", "")
    lx.setdefault("denseTopK", 20)
    lx.setdefault("lexTopK", 20)
    lx.setdefault("rrfK", 60)
    lx.setdefault("autoBuildMissing", True)
    return lx


def _lex_db_path(lx: Dict[str, Any]) -> str:
    p = str(lx.get("dbPath") or "").strip()
    return p or str(SERVER_DIR / "lexical_index.db")


_lex_ensured = False


def _ensure_lexical_index(lx: Dict[str, Any]) -> None:
    """Build the FTS index on first use when it is missing/empty (best-effort,
    once per process). Any failure degrades silently to dense-only retrieval."""
    global _lex_ensured
    if _lex_ensured:
        return
    _lex_ensured = True
    db = _lex_db_path(lx)
    try:
        if lexical.count(db) > 0:
            return
        if lx.get("autoBuildMissing") is False:
            return
        import lexical_index

        n = lexical_index.build_from_env(db, CFG.get("userId"))
        LOG.warning("lexical index built on first use: db=%s rows=%s", db, n)
    except Exception as exc:  # noqa: BLE001 - never fatal
        LOG.warning("lexical index auto-build skipped: %s", exc)


def rebuild_lexical_index() -> int:
    """Force a full rebuild from the active store (used by /api/lexical/rebuild).

    Source follows the environment (B5): sqlite_vec single-file db when
    ``MEM0_VECTOR_STORE=sqlite_vec``, else legacy Qdrant scroll (rollback)."""
    import lexical_index

    lx = _lex_cfg()
    return lexical_index.build_from_env(_lex_db_path(lx), CFG.get("userId"))


async def ranked_items(
    query: str,
    want: int,
    t0: Optional[float] = None,
    recency_weight: Optional[float] = None,
    supersede: Optional[bool] = None,
) -> Dict[str, Any]:
    """统一检索管线：进程内 dense search → (可选)词法旁路 RRF 融合 / (可选)rerank
    → 相关度+时效打分 → 排序 → 阈值/命中过滤。"""
    t0 = t0 or time.monotonic()
    # 治本(20261016)：截断超长 query，避免嵌入窗口溢出（dense/lexical 共用同一 query）。
    try:
        qmax = int(CFG.get("queryMaxChars") or RECALL_QUERY_MAX_CHARS)
    except (TypeError, ValueError):
        qmax = RECALL_QUERY_MAX_CHARS
    if qmax > 0 and len(query) > qmax:
        query = query[:qmax]
    use_rerank = CFG.get("rerank") is True
    lx = _lex_cfg()
    use_lex = bool(lx.get("enabled")) and not use_rerank

    dense_want = want
    if use_lex:
        try:
            dense_want = max(want, int(lx.get("denseTopK") or 0))
        except (TypeError, ValueError):
            dense_want = want

    def _search(w: int) -> Dict[str, Any]:
        return get_memory_instance().search(
            query=query, filters={"user_id": CFG["userId"]}, top_k=w
        )

    res = await asyncio.to_thread(_search, dense_want)
    raw = (res or {}).get("results") or []

    rel: Optional[List[Optional[float]]] = None
    rel_map: Dict[str, Optional[float]] = {}
    if use_rerank and raw:
        docs = [_mem_text(r) for r in raw]
        rel = await rerank_scores(query, docs)
        if rel:
            for i, r in enumerate(raw):
                if isinstance(r, dict) and r.get("id") is not None and i < len(rel):
                    rel_map[str(r["id"])] = rel[i]

    # ---- 词法旁路（best-effort，失败静默回退纯 dense）------------------------
    lex_records: List[Dict[str, Any]] = []
    if use_lex:
        try:
            _ensure_lexical_index(lx)
            lex_records = lexical.search(
                _lex_db_path(lx), query, limit=int(lx.get("lexTopK") or 20)
            )
        except Exception:  # noqa: BLE001
            lex_records = []

    # 候选集：dense 在前，词法独有条目补齐（用旁路记录构造轻量 r）。
    cand: Dict[str, Dict[str, Any]] = {}
    dense_ids: List[str] = []
    for r in raw:
        if not isinstance(r, dict) or r.get("id") is None:
            continue
        rid = str(r["id"])
        dense_ids.append(rid)
        cand[rid] = {"r": r, "lex": False}
    lex_ids: List[str] = []
    for rec in lex_records:
        if not isinstance(rec, dict) or rec.get("id") is None:
            continue
        rid = str(rec["id"])
        lex_ids.append(rid)
        if rid in cand:
            cand[rid]["lex"] = True
        else:
            meta = rec.get("metadata")
            cand[rid] = {
                "r": {
                    "id": rid,
                    "memory": rec.get("text"),
                    "created_at": rec.get("created_at"),
                    "agent_id": rec.get("agent_id"),
                    "metadata": meta if isinstance(meta, dict) else {},
                },
                "lex": True,
            }

    # 融合相关度：词法开 → 归一化 RRF（top=1.0）；否则用向量/重排分。
    rrf_norm: Dict[str, float] = {}
    if use_lex:
        lists = [lst for lst in (dense_ids, lex_ids) if lst]
        if lists:
            fused = lexical.rrf_fuse(lists, k=int(lx.get("rrfK") or 60))
            if fused and fused[0][1] > 0:
                top = fused[0][1]
                rrf_norm = {mid: sc / top for mid, sc in fused}

    w_rec = (
        max(0.0, min(1.0, float(recency_weight)))
        if recency_weight is not None
        else max(0.0, min(1.0, float(CFG.get("recencyWeight") or 0)))
    )
    w_rel = 1 - w_rec
    half_life = float(CFG.get("recencyHalfLifeDays") or 14)
    items: List[Dict[str, Any]] = []
    for rid, c in cand.items():
        r = c["r"]
        mem = _mem_text(r)
        vs = r.get("score") if isinstance(r, dict) else None
        vs = float(vs) if vs is not None else None
        rr = rel_map.get(rid)
        if use_rerank:
            relevance01 = rr if rr is not None else vs
        elif use_lex:
            relevance01 = rrf_norm.get(rid)
            if relevance01 is None:
                relevance01 = vs
        else:
            relevance01 = vs
        age = age_in_days(r.get("created_at")) if isinstance(r, dict) else None
        if age is None:
            dtxt = date_from_text(mem)
            age = age_in_days(dtxt) if dtxt else None
        recency01 = 0.0 if age is None else 0.5 ** (age / half_life)
        final = w_rel * (relevance01 if relevance01 is not None else 0.0) + w_rec * recency01
        items.append(
            {
                "r": r,
                "mem": mem,
                "vs": vs,
                "rr": rr,
                "relevance01": relevance01,
                "recency01": recency01,
                "final": final,
                "lex": c["lex"],
            }
        )
    await apply_supersede(items, t0, supersede)
    items.sort(key=lambda it: it["final"], reverse=True)

    thr = CFG.get("threshold")
    thr = float(thr) if thr not in (None, "") else None
    use_rr_thr = use_rerank and rel is not None
    if use_rr_thr:
        rt = CFG.get("rerankThreshold")
        rt = float(rt) if rt not in (None, "") else None
        eff_thr = rt if rt is not None else 0.3
        kept = [
            it
            for it in items
            if it["relevance01"] is not None and it["relevance01"] >= eff_thr
        ]
    elif use_lex:
        # 保留条件：dense 余弦 >= 阈值，或词法命中（RRF 融合的核心增益）。
        kept = [
            it
            for it in items
            if it["lex"] or thr is None or (it["vs"] is not None and it["vs"] >= thr)
        ]
    elif thr is not None and math.isfinite(thr):
        kept = [
            it
            for it in items
            if it["relevance01"] is not None and it["relevance01"] >= thr
        ]
    else:
        kept = items
    # 双池策略（2026-10-17）：覆盖上面的单一阈值逻辑——分池配额 + 分池阈值 + 短期过期剔除。
    if _pool_enabled():
        kept = apply_pool_policy(items)
    return {"raw": raw, "items": items, "kept": kept, "wRec": w_rec}


def _shown_text(it: Dict[str, Any]) -> str:
    tag = label_of(it["r"]) if isinstance(it["r"], dict) else ""
    body = it["mem"]
    if not SRC_PREFIX_RE.match(it["mem"]) and tag:
        body = f"[{tag}] {it['mem']}"
    # 双池来源分级：让"这是刚发生的对话"与"这是沉淀的事实"在注入块里可见。
    mark = {"short": "[短]", "long": "[长]"}.get(str(it.get("pool") or ""), "")
    return f"{mark} {body}" if mark else body


def _score_suffix(it: Dict[str, Any], w_rec: float) -> str:
    if it["relevance01"] is None:
        return ""
    src = "·rr" if it["rr"] is not None else ("·lex" if it.get("lex") else "")
    tstamp = f"·t{it['recency01']:.2f}" if w_rec > 0 else ""
    sup = f"·疑似被新条取代→{str(it['superseded_by'])[:8]}" if it.get("superseded_by") else ""
    return f"（相关度 {it['relevance01']:.3f}{src}{tstamp}{sup}）"


def _when_suffix(it: Dict[str, Any]) -> str:
    r = it["r"] if isinstance(it["r"], dict) else {}
    bj = fmt_bj(r.get("created_at"))
    if bj:
        return f" [{bj}]"
    if r.get("created_at"):
        return f" [{r['created_at']}]"
    return ""


def format_injected_lines(kept: List[Dict[str, Any]], w_rec: float) -> str:
    """注入块用的行格式（"- ..."，无序号），移植自插件 formatInjectedLines。"""
    return "\n".join(
        f"- {_shown_text(it)}{_score_suffix(it, w_rec)}{_when_suffix(it)}" for it in kept
    )


def format_search_hits(kept: List[Dict[str, Any]], w_rec: float) -> str:
    """工具用的行格式（带序号 + id，便于接续 forget），移植自插件 formatSearchHits。"""
    lines = []
    for i, it in enumerate(kept):
        rid = it["r"].get("id") if isinstance(it["r"], dict) else None
        line = f"{i + 1}. {_shown_text(it)}{_score_suffix(it, w_rec)}{_when_suffix(it)}"
        if rid:
            line += f" id={rid}"
        lines.append(line)
    return "\n".join(lines)


async def run_recall(
    query: str,
    top_k: Optional[int] = None,
    recency_weight: Optional[float] = None,
    supersede: Optional[bool] = None,
) -> Dict[str, Any]:
    q = str(query or "").strip()
    t0 = time.monotonic()
    top_k = max(1, int(top_k or CFG.get("topK") or 5))
    use_rerank = CFG.get("rerank") is True
    want = max(int(CFG.get("rerankTopN") or top_k), top_k) if use_rerank else top_k
    # 双池：候选放大（分池后还要挑配额，候选不足会漏老事实）。
    if _pool_enabled():
        try:
            factor = max(1, int(_pool_cfg().get("candidateFactor") or 1))
        except (TypeError, ValueError):
            factor = 1
        want = max(want, top_k * factor)
    res = await ranked_items(q, want, t0, recency_weight, supersede)
    kept = res["kept"][:top_k]
    return {
        "query": q,
        "raw_count": len(res["raw"]),
        "kept_count": len(res["kept"]),
        "wRec": res["wRec"],
        "lines": format_injected_lines(kept, res["wRec"]),
        "hits": format_search_hits(kept, res["wRec"]),
        "items": [
            {
                "id": (it["r"].get("id") if isinstance(it["r"], dict) else None),
                "memory": it["mem"],
                "relevance01": it["relevance01"],
                "recency01": it["recency01"],
                "final": it["final"],
                "superseded_by": it.get("superseded_by"),
                "pool": it.get("pool"),
                "expired": it.get("expired"),
            }
            for it in kept
        ],
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
    }


def _do_remember(
    text: str,
    user_id: str,
    agent_id: str,
    infer: bool,
    metadata: Optional[Dict[str, Any]],
    tag: Optional[str] = None,
) -> Any:
    """走 main.add_memory（带 ingest_guard 去重/低价值管线），懒导入避免循环依赖。"""
    from main import MemoryCreate, Message, add_memory

    visible_tag = tag or f"{agent_id}@{MACHINE}"
    md: Dict[str, Any] = {**(metadata or {}), "src": agent_id, "machine": MACHINE}
    if _pool_enabled():
        pool = classify_pool(text, md, src=agent_id)
        md.setdefault("pool", pool)
        md.setdefault("pool_origin", "write" if md.get("type") else "write-default")
        if pool == "short":
            try:
                ttl_h = float(_pool_cfg().get("shortTtlHours") or 72)
            except (TypeError, ValueError):
                ttl_h = 72.0
            md.setdefault(
                "pool_expires_at",
                (datetime.now(timezone.utc) + timedelta(hours=ttl_h)).isoformat(),
            )
            md.setdefault("pool_status", "active")
    mc = MemoryCreate(
        messages=[Message(role="user", content=with_source_prefix(visible_tag, text))],
        user_id=user_id,
        agent_id=agent_id,
        infer=bool(infer),
        metadata=md,
    )
    return add_memory(memory_create=mc, _auth=None)


async def run_remember(
    text: str,
    infer: bool = False,
    metadata: Optional[Dict[str, Any]] = None,
    agent_id: Optional[str] = None,
    user_id: Optional[str] = None,
    tag: Optional[str] = None,
) -> Any:
    aid = agent_id or str(CFG.get("rememberAgent") or "gateway")
    uid = user_id or CFG["userId"]
    result = await asyncio.to_thread(_do_remember, text, uid, aid, infer, metadata, tag)
    _index_remembered(result, aid)
    return result


def _index_remembered(result: Any, agent_id: str) -> None:
    """Best-effort: append newly written memories to the lexical index so it stays
    fresh between full rebuilds. No-op when lexical is disabled; never fatal."""
    lx = _lex_cfg()
    if not lx.get("enabled"):
        return
    try:
        body = getattr(result, "body", None)
        if body is None:
            return
        if isinstance(body, (bytes, bytearray)):
            body = body.decode("utf-8", "replace")
        payload = json.loads(body)
        now = datetime.now(timezone.utc).isoformat()
        db = _lex_db_path(lx)
        for rec in payload.get("results") or []:
            mid = rec.get("id")
            mem = rec.get("memory")
            if mid and isinstance(mem, str) and mem and rec.get("event") != "DELETE":
                lexical.add_one(db, str(mid), mem, created_at=now, agent_id=agent_id)
    except Exception:  # noqa: BLE001 - never fatal
        return


# ------------------------------------------------------------------ REST 兜底

router = APIRouter(prefix="/api", tags=["gateway"])


def _check_bearer(authorization: Optional[str]) -> None:
    tok = str(CFG.get("bearerToken") or "").strip()
    if not tok:
        return
    if authorization != f"Bearer {tok}":
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


class RecallRequest(BaseModel):
    query: str
    top_k: Optional[int] = None
    # 每请求覆盖（5c：opencode 现网 wRec=0.05+supersede 开，DSH 关/默认 0.15，客户端各传各的）。
    recency_weight: Optional[float] = None
    supersede: Optional[bool] = None


class RememberRequest(BaseModel):
    text: str
    infer: bool = False
    metadata: Optional[Dict[str, Any]] = None
    agent_id: Optional[str] = None
    user_id: Optional[str] = None
    # 可见正文前缀标签（如 opencode/researcher@host）；缺省 = [agent_id@host]。
    tag: Optional[str] = None
    # 双池：显式指定 "short"/"long"；缺省由服务端 classify_pool 判定。
    pool: Optional[str] = None


@router.post("/recall")
async def api_recall(body: RecallRequest, authorization: Optional[str] = Header(None)):
    _check_bearer(authorization)
    return await run_recall(body.query, body.top_k, body.recency_weight, body.supersede)


@router.post("/remember")
async def api_remember(body: RememberRequest, authorization: Optional[str] = Header(None)):
    _check_bearer(authorization)
    md = dict(body.metadata or {})
    if body.pool in ("short", "long"):
        md["pool"] = body.pool
    # add_memory 可能返回 JSONResponse（去重 NOOP 等），直接透传。
    return await run_remember(body.text, body.infer, md, body.agent_id, body.user_id, body.tag)


# ------------------------------------------------- 双池：观测 + 沉淀提炼（2026-10-17）
# 短期池 → 长期池的"沉淀"通道：把过期/待巩固的原文交给本地模型提炼成事实，
# 原文标记 archived 退出注入。模型走 102→101 的 SSH 隧道（127.0.0.1:18888），
# 刻意绕开 101 透传网关的 memory_hook，避免提炼过程反写记忆池。

CONSOLIDATE_LAST: Dict[str, Any] = {}
DISTILL_LAST_RAW: str = ""


def _consolidate_cfg() -> Dict[str, Any]:
    c = CFG.get("consolidate")
    return c if isinstance(c, dict) else {}


def _all_rows(limit: int = 8000) -> List[Dict[str, Any]]:
    from main import _list_all_memories

    try:
        data = _list_all_memories(limit=limit)
    except Exception:  # noqa: BLE001
        return []
    return list((data or {}).get("results") or [])


def pool_status() -> Dict[str, Any]:
    rows = _all_rows()
    stat: Dict[str, Any] = {
        "total": 0, "short": 0, "long": 0,
        "short_active": 0, "short_expired": 0,
        "archived": 0, "consolidated": 0, "last": CONSOLIDATE_LAST,
    }
    for r in rows:
        md = r.get("metadata") or {}
        mem = str(r.get("memory") or "")
        pool = pool_of(r, mem)
        stat["total"] += 1
        stat[pool] = stat.get(pool, 0) + 1
        if str(md.get("pool_status") or "") == "archived":
            stat["archived"] += 1
        if str(md.get("pool_origin") or "") == "consolidate":
            stat["consolidated"] += 1
        if pool == "short" and str(md.get("pool_status") or "") != "archived":
            if pool_expired(r, pool):
                stat["short_expired"] += 1
            else:
                stat["short_active"] += 1
    return stat


DISTILL_PROMPT = """你是记忆巩固器。以下是某段对话/工作记录原文（可能含「用户：」「助手：」）。

任务：提炼**最多 3 条**值得长期记住的事实，供未来会话直接复用。

要求：
- 只保留结论性、可复用信息（决策、规则、修复、偏好、已验证事实），必须原样保留关键数字、路径、版本号、commit、机器名；
- 不要"用户提到/助手解释"这类过程描述；不要寒暄、情绪、未完成待办；
- 每条独立成立，≤120 字，中文；
- **每条必须以完整主语开头**（系统/配置/主机/项目/具体组件/具体人），不得以"当…时""第 N 步""大小一致时""对账时"这类依赖上下文的从句开头；无法独立成立的合并进上一条或丢弃；
- 没有长期价值就返回空数组；
- 全文总输出 ≤600 字（务必简短，超长会被截断）。

只输出 JSON 数组，元素形如 {"text": "...", "type": "decision|rule|fix|preference|fact"}，不要任何解释文字。

原文：
"""


def _parse_distill_json(content: str) -> List[Dict[str, Any]]:
    s = str(content or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    i, j = s.find("["), s.rfind("]")
    arr: Any = None
    if i >= 0 and j > i:
        try:
            arr = json.loads(s[i : j + 1])
        except Exception:  # noqa: BLE001
            arr = None
    if not isinstance(arr, list):
        # 加固：模型被 max_tokens 截断时数组不闭合 → 抢救其中完整对象。
        arr = []
        for m in re.finditer(r"\{[^{}]*\"text\"[^{}]*\}", s):
            try:
                obj = json.loads(m.group(0))
            except Exception:  # noqa: BLE001
                continue
            if isinstance(obj, dict):
                arr.append(obj)
    out: List[Dict[str, Any]] = []
    for it in arr:
        if not isinstance(it, dict):
            continue
        t = str(it.get("text") or "").strip()
        typ = str(it.get("type") or "fact").strip().lower()
        if not _looks_standalone(t):
            continue
        if typ not in ("decision", "rule", "fix", "preference", "fact"):
            typ = "fact"
        out.append({"text": t[:300], "type": typ})
    return out[:3]


# 碎片过滤：提炼质量闸。判断"是否能脱离上下文独立成立"的轻量启发式——
# 以从句/指示词开头、或者又短又没有任何具体信息（数字/字母/路径/标点冒号）的，一律丢弃。
FRAGMENT_HEAD_RE = re.compile(
    r"^(当|若|如果|则|就|并|且|而|或|由于|因为|所以|然后|之后|此前|此时|"
    r"大小一致|第\s*[0-9一二三四五六七八九十]+\s*步|该|此|其|上述|以上|以下)"
)
INFO_RE = re.compile(r"[A-Za-z0-9_:/\\\-\.#]")


def _looks_standalone(text: str) -> bool:
    t = str(text or "").strip()
    if len(t) < 24:
        return False
    if FRAGMENT_HEAD_RE.match(t):
        return False
    if "用户：" in t or "助手：" in t:
        return False
    # 没有任何具体信息（无数字/字母/路径/冒号结论）且偏短 → 视为泛泛而谈。
    if len(t) < 40 and not INFO_RE.search(t) and "：" not in t:
        return False
    return True


async def distill_text(text: str) -> List[Dict[str, Any]]:
    cfg = _consolidate_cfg()
    endpoint = str(cfg.get("endpoint") or "http://127.0.0.1:18888/v1").rstrip("/")
    model = str(cfg.get("model") or "GLM-5.3-Flash-EXL3")
    try:
        timeout = float(cfg.get("timeoutMs") or 120000) / 1000.0
    except (TypeError, ValueError):
        timeout = 120.0
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": DISTILL_PROMPT + text}],
        "temperature": 0.2,
        "max_tokens": 1200,
        # GLM-5.3 是思考型：不关思考会把 max_tokens 全烧在 reasoning 上，content 返回空。
        "chat_template_kwargs": {"thinking": False},
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(endpoint + "/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
    content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    global DISTILL_LAST_RAW
    DISTILL_LAST_RAW = str(content)[:4000]
    return _parse_distill_json(content)


async def run_consolidate(
    dry_run: bool = True, limit: Optional[int] = None
) -> Dict[str, Any]:
    """短期池 → 长期池沉淀：按 (agent, 日期) 分组，模型提炼后写长期池，原文归档。"""
    t0 = time.monotonic()
    cfg = _consolidate_cfg()
    if cfg.get("enabled") is False:
        return {"ok": False, "skipped": "consolidate.enabled=false"}
    try:
        max_n = max(1, int(limit or cfg.get("maxEntries") or 20))
    except (TypeError, ValueError):
        max_n = 20
    try:
        max_chars = max(500, int(cfg.get("maxChars") or 6000))
    except (TypeError, ValueError):
        max_chars = 6000

    rows = _all_rows()
    cands: List[Dict[str, Any]] = []
    for r in rows:
        md = r.get("metadata") or {}
        if pool_of(r, str(r.get("memory") or "")) != "short":
            continue
        if str(md.get("pool_status") or "") == "archived":
            continue
        mem = str(r.get("memory") or "")
        if len(mem) < 40:
            continue
        age = age_in_days(r.get("created_at"))
        if not (
            pool_expired(r, "short")
            or CHAT_TURN_RE.search(mem[:8000])
            or (age is not None and age >= 1.0)
        ):
            continue
        cands.append(r)
    cands.sort(key=lambda r: str(r.get("created_at") or ""))
    picked = cands[:max_n]

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in picked:
        key = f"{r.get('agent_id') or '?'}|{str(r.get('created_at') or '')[:10]}"
        groups.setdefault(key, []).append(r)

    # 组内再按 maxChars 切块：保证被选中的每一条都会被处理（否则每次只吃前几千字）。
    chunks: List[Dict[str, Any]] = []
    for key, rs in groups.items():
        buf = ""
        used: List[Dict[str, Any]] = []
        idx = 0
        for r in rs:
            mem = str(r.get("memory") or "")
            if buf and len(buf) + len(mem) + 2 > max_chars:
                chunks.append({"key": f"{key}#{idx}", "used": used, "text": buf})
                idx += 1
                buf = ""
                used = []
            buf += ("\n\n" if buf else "") + mem
            used.append(r)
        if used:
            chunks.append({"key": f"{key}#{idx}", "used": used, "text": buf})

    try:
        max_calls = max(1, int(cfg.get("maxCalls") or 8))
    except (TypeError, ValueError):
        max_calls = 8

    report: Dict[str, Any] = {
        "ok": True, "dry_run": bool(dry_run), "candidates": len(cands),
        "picked": len(picked), "groups": len(groups), "chunks": len(chunks),
        "created": 0, "archived": 0, "errors": [], "samples": [],
    }
    for ch in chunks[:max_calls]:
        key = ch["key"]
        used = ch["used"]
        text = ch["text"]
        if not text:
            continue
        try:
            facts = await distill_text(text)
        except Exception as e:  # noqa: BLE001
            report["errors"].append(f"{key}: {type(e).__name__}")
            continue
        if dry_run:
            if len(report["samples"]) < 3:
                report["samples"].append(
                    {
                        "group": key,
                        "chars": len(text),
                        "facts": facts,
                        "raw": DISTILL_LAST_RAW[:800],
                    }
                )
            continue
        for f in facts:
            try:
                await run_remember(
                    f["text"],
                    False,
                    {
                        "type": f["type"],
                        "pool": "long",
                        "pool_origin": "consolidate",
                        "source_ids": [r.get("id") for r in used][:20],
                        "source_tag": key,
                    },
                    agent_id="consolidate",
                    tag="consolidate",
                )
                report["created"] += 1
            except Exception as e:  # noqa: BLE001
                report["errors"].append(f"{key}/write: {type(e).__name__}")
        for r in used:
            try:
                md = dict(r.get("metadata") or {})
                md["pool"] = "short"
                md["pool_status"] = "archived"
                await asyncio.to_thread(
                    get_memory_instance().update, memory_id=r.get("id"), metadata=md
                )
                report["archived"] += 1
            except Exception as e:  # noqa: BLE001
                report["errors"].append(f"{r.get('id')}/archive: {type(e).__name__}")

    report["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    CONSOLIDATE_LAST.clear()
    CONSOLIDATE_LAST.update(report)
    return report


@router.get("/pool/status")
async def api_pool_status(authorization: Optional[str] = Header(None)):
    _check_bearer(authorization)
    return await asyncio.to_thread(pool_status)


class ConsolidateRequest(BaseModel):
    dry_run: bool = True
    limit: Optional[int] = None


@router.post("/pool/consolidate")
async def api_pool_consolidate(
    body: ConsolidateRequest, authorization: Optional[str] = Header(None)
):
    _check_bearer(authorization)
    return await run_consolidate(body.dry_run, body.limit)


@router.post("/lexical/rebuild")
async def api_lexical_rebuild(authorization: Optional[str] = Header(None)):
    """重建词法 FTS 索引（全量；源=当前 vector store：sqlite_vec 单库或 Qdrant 回滚）。"""
    _check_bearer(authorization)
    try:
        n = await asyncio.to_thread(rebuild_lexical_index)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"lexical rebuild failed: {exc}")
    return {"ok": True, "rows": n}


# ------------------------------------------------------------------ git-memory API（阶段 5b）
#
# 服务端读写 git-memory 明文记忆仓库（独立 .git，不上云）：
#   GET  /api/git-memory/status   仓库健康（HEAD/branch/dirty）
#   GET  /api/git-memory/memory   读 MEMORY.md（?file= 可读任意仓库内 .md）
#   POST /api/git-memory/entries  追加一条目 + git commit（memory: 前缀）
#   GET  /api/git-memory/log      最近 commit（验证用）
# 并发口径：单用户单进程，asyncio.Lock 串行写（章程 §6.6 拍板 3，乐观并发备而不用）。
# 硬约束：条目/commit 禁密钥（CONVENTIONS.md）；commit 消息前缀 memory:（dream: 留给 Dreaming）。

GITMEM_SECRET_RES = [
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{10,}"),
    re.compile(r"(?i)(?:password|passwd|secret|token|密码|口令)\s*[:=]\s*\S+"),
]

gitmem_lock = asyncio.Lock()


def _gitmem_cfg() -> Dict[str, Any]:
    gm = dict(CFG.get("gitMemory") or {})
    gm.setdefault("enabled", True)
    gm.setdefault("repo", "")
    gm.setdefault("gitBin", "git")
    gm.setdefault("defaultFile", "MEMORY.md")
    gm.setdefault("commitPrefix", "memory")
    return gm


def _gitmem_repo() -> Path:
    gm = _gitmem_cfg()
    if gm["enabled"] is False or not str(gm["repo"]).strip():
        raise HTTPException(
            status_code=503,
            detail="git-memory API disabled: set gitMemory.repo in gateway_config.json",
        )
    repo = Path(str(gm["repo"]))
    if not (repo / ".git").exists():
        raise HTTPException(status_code=503, detail=f"git-memory repo not found: {repo}")
    return repo


def _gitmem_run(repo: Path, *args: str, timeout: float = 30.0) -> str:
    gm = _gitmem_cfg()
    try:
        proc = subprocess.run(
            [str(gm.get("gitBin") or "git"), *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail=f"git {args[0]} timed out")
    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"git {args[0]} failed (rc={proc.returncode}): {proc.stderr.strip()[:500]}",
        )
    return proc.stdout


def _assert_no_secrets(text: str) -> None:
    for rx in GITMEM_SECRET_RES:
        m = rx.search(text)
        if m:
            raise HTTPException(
                status_code=400,
                detail=f"entry rejected by secret guard (matched {rx.pattern}); git-memory 条目禁密钥",
            )


def _resolve_gitmem_file(repo: Path, rel: str) -> Path:
    rel = (rel or "").strip().replace("\\", "/").lstrip("/")
    if not rel or not rel.lower().endswith(".md"):
        raise HTTPException(status_code=400, detail="file must be a repo-relative .md path")
    p = (repo / rel).resolve()
    repo_r = repo.resolve()
    if p != repo_r and repo_r not in p.parents:
        raise HTTPException(status_code=400, detail="path escapes git-memory repo")
    return p


def _build_entry_line(text: str, source: str) -> str:
    s = re.sub(r"\s*\r?\n\s*", " ", str(text or "")).strip()
    if s.startswith("- "):
        s = s[2:].strip()
    if not s:
        raise HTTPException(status_code=400, detail="entry text is empty")
    _assert_no_secrets(s)
    src = re.sub(r"\s+", " ", str(source or "user").strip()) or "user"
    src = src.replace("[", "(").replace("]", ")")
    added = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    return f"- {s} [source: {src}; added: {added}]"


def _append_to_markdown(content: str, line: str, section: Optional[str]) -> str:
    """按 CONVENTIONS 追加一行 bullet；section 给定时插到 `## <section>` 块尾（无则建节）。"""
    if not section:
        base = content.rstrip("\n")
        return (base + "\n\n" + line + "\n") if base else (line + "\n")
    sec = section.strip()
    m = re.search(rf"^##\s+{re.escape(sec)}\s*$", content, re.M)
    if m:
        rest = content[m.end():]
        nxt = re.search(r"^#{1,6}\s", rest, re.M)
        cut = m.end() + nxt.start() if nxt else len(content)
        body = content[:cut].rstrip("\n")
        tail = content[cut:]
        out = body + "\n" + line + "\n"
        if tail.strip():
            out += "\n" + tail if not tail.startswith("\n") else tail
        return out
    base = content.rstrip("\n")
    if base:
        return base + f"\n\n## {sec}\n\n{line}\n"
    return f"## {sec}\n\n{line}\n"


class GitMemoryEntryRequest(BaseModel):
    text: str
    file: Optional[str] = None  # 默认 MEMORY.md；可写仓库内任意 .md（主题文件按需生长）
    section: Optional[str] = None  # 插到 `## <section>` 块尾；缺省追加文件末尾
    source: Optional[str] = None  # 行尾 [source: …]，缺省 user
    commit: Optional[str] = None  # commit 主题，自动补 memory: 前缀


async def _gitmem_append(req: GitMemoryEntryRequest) -> Dict[str, Any]:
    repo = _gitmem_repo()
    gm = _gitmem_cfg()
    fpath = _resolve_gitmem_file(repo, req.file or gm["defaultFile"])
    rel = fpath.relative_to(repo.resolve()).as_posix()
    line = _build_entry_line(req.text, req.source or "user")
    subject = (req.commit or "").strip() or f"{Path(rel).stem} 条目"
    if not re.match(r"^(memory|dream):", subject):
        subject = f"{gm['commitPrefix']}: {subject}"

    def _work() -> None:
        old = fpath.read_text(encoding="utf-8") if fpath.exists() else ""
        new = _append_to_markdown(old, line, req.section)
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(new, encoding="utf-8", newline="\n")
        _gitmem_run(repo, "add", "--", rel)
        _gitmem_run(repo, "commit", "-m", subject)

    async with gitmem_lock:
        await asyncio.to_thread(_work)
    sha = _gitmem_run(repo, "rev-parse", "--short", "HEAD").strip()
    return {"ok": True, "file": rel, "line": line, "commit": sha, "message": subject}


@router.get("/git-memory/status")
async def api_gitmem_status(authorization: Optional[str] = Header(None)):
    _check_bearer(authorization)
    gm = _gitmem_cfg()
    if gm["enabled"] is False or not str(gm["repo"]).strip():
        return {"enabled": False}
    repo = Path(str(gm["repo"]))
    if not (repo / ".git").exists():
        return {"enabled": True, "repo": str(repo), "ok": False, "detail": "repo/.git missing"}
    head = _gitmem_run(repo, "rev-parse", "--short", "HEAD").strip()
    branch = _gitmem_run(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    dirty = _gitmem_run(repo, "status", "--porcelain").strip()
    return {"enabled": True, "repo": str(repo), "ok": True, "head": head, "branch": branch, "dirty": bool(dirty)}


@router.get("/git-memory/memory")
async def api_gitmem_memory(file: Optional[str] = None, authorization: Optional[str] = Header(None)):
    _check_bearer(authorization)
    repo = _gitmem_repo()
    gm = _gitmem_cfg()
    fpath = _resolve_gitmem_file(repo, file or gm["defaultFile"])
    if not fpath.exists():
        raise HTTPException(status_code=404, detail=f"not found: {fpath.relative_to(repo.resolve()).as_posix()}")
    content = await asyncio.to_thread(fpath.read_text, encoding="utf-8")
    head = _gitmem_run(repo, "rev-parse", "--short", "HEAD").strip()
    return JSONResponse(
        content={
            "file": fpath.relative_to(repo.resolve()).as_posix(),
            "head": head,
            "content": content,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/git-memory/entries")
async def api_gitmem_entries(body: GitMemoryEntryRequest, authorization: Optional[str] = Header(None)):
    _check_bearer(authorization)
    return await _gitmem_append(body)


@router.get("/git-memory/log")
async def api_gitmem_log(limit: int = 5, authorization: Optional[str] = Header(None)):
    _check_bearer(authorization)
    repo = _gitmem_repo()
    n = max(1, min(int(limit), 50))
    out = _gitmem_run(repo, "log", f"-{n}", "--pretty=%h %ad %s", "--date=format-local:%Y-%m-%d %H:%M")
    return {"log": [line for line in out.splitlines() if line.strip()]}


# ------------------------------------------------------------------ MCP /mcp


def _build_mcp():
    from fastmcp import FastMCP

    token = str(CFG.get("bearerToken") or "").strip()
    if token:
        from fastmcp.server.auth import StaticTokenVerifier

        auth = StaticTokenVerifier(tokens={token: {"client_id": "memory-gateway-clients"}})
        mcp = FastMCP("memory-gateway", auth=auth)
    else:
        mcp = FastMCP("memory-gateway")

    @mcp.tool
    async def recall(query: str, top_k: Optional[int] = None) -> str:
        """在 本机记忆库里检索与查询相关的记忆（语义检索 + 交叉编码器 rerank + 时效加权 + 阈值过滤），返回最相关的若干条。"""
        res = await run_recall(query, top_k)
        if not res["kept_count"]:
            return f"没有找到相关记忆（raw={res['raw_count']}，已应用 rerank={'on' if CFG.get('rerank') else 'off'} + 阈值过滤）。"
        return res["hits"]

    @mcp.tool
    async def remember(
        text: str,
        infer: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
        agent_id: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> str:
        """把一条值得长期记住的信息（事实/决定/偏好/坑）写入 本机记忆库（mem0）。所有 agent 与设备共享同一 user_id 池，写入后其它会话可检索到。infer=true 时由服务端 LLM 抽取事实（默认 false 原样存文本）。tag=可选可见正文前缀（如 opencode/researcher@host），缺省 [agent_id@host]。"""
        res = await run_remember(text, infer, metadata, agent_id, None, tag)
        if isinstance(res, JSONResponse):
            payload = json.loads(bytes(res.body))
        else:
            payload = res
        return f"已写入 本机记忆库。\n{json.dumps(payload, ensure_ascii=False, default=str)}"

    @mcp.tool
    async def git_memory_read(file: Optional[str] = None) -> str:
        """读 git-memory 明文记忆仓库的文件（默认 MEMORY.md 每会话注入入口；可读仓库内任意 .md）。"""
        try:
            repo = _gitmem_repo()
        except HTTPException as e:
            return f"git-memory 不可用：{e.detail}"
        gm = _gitmem_cfg()
        fpath = _resolve_gitmem_file(repo, file or gm["defaultFile"])
        if not fpath.exists():
            return f"未找到 {fpath.relative_to(repo.resolve()).as_posix()}"
        return fpath.read_text(encoding="utf-8")

    @mcp.tool
    async def git_memory_write(
        text: str,
        section: Optional[str] = None,
        file: Optional[str] = None,
        source: Optional[str] = None,
        commit: Optional[str] = None,
    ) -> str:
        """向 git-memory 明文记忆仓库追加一条条目并 commit。一行一 bullet，行尾自动带 [source; added:北京时间]；commit 自动加 memory: 前缀；写入前做禁密钥检查。section 给定时插到 `## <section>` 块尾。"""
        try:
            res = await _gitmem_append(
                GitMemoryEntryRequest(text=text, section=section, file=file, source=source, commit=commit)
            )
        except HTTPException as e:
            return f"写入失败：{e.detail}"
        return f"已写入 {res['file']} 并提交 {res['commit']}（{res['message']}）：\n{res['line']}"

    return mcp


mcp = _build_mcp()
# path="/mcp"：由 main.py 以 app.mount("/", mcp_app) 根挂载（须加在所有路由之后），
# 这样 /mcp 与 /mcp/（子 app redirect）都可达；若改为挂 "/mcp" 前缀，剥前缀后
# 空路径会 404（Starlette Route 不接受 ""）。
mcp_app = mcp.http_app(path="/mcp")


@contextlib.asynccontextmanager
async def mcp_lifespan():
    """供 main.py lifespan 合并：mount 不会自动跑子 app 的 lifespan。"""
    async with mcp_app.router.lifespan_context(mcp_app):
        yield
