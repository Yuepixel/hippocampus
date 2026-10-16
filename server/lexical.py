"""Lexical retrieval side-channel backed by SQLite FTS5 (trigram tokenizer).

The index is fully disposable: it only ever stores facts already held in Qdrant
(payload key ``data``) and can be rebuilt at any time from the vector store.
Standard library only -- this module never imports the vector store or network.

Why FTS5 ``tokenize='trigram'``: it is the only built-in tokenizer that indexes
CJK text (and identifiers like ``8890`` / ``gateway.py``) without a segmenter.
Consequences (measured on the appliance, SQLite 3.46.1):
  * a bare sequence of tokens is a *contiguous phrase*; use explicit ``OR`` to
    get disjunctive matching (we always join with OR);
  * tokens shorter than 3 characters never match (trigram is a 3-char window);
    ``fts_query`` therefore drops them.

B2 design note: this index supplies a second ranked list to RRF fusion in the
gateway; it is a *recall supplement*, not a replacement for dense retrieval.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

MIN_TOKEN = 3
TABLE = "mem_fts"
# id + provenance columns are UNINDEXED (stored verbatim, not tokenised); only
# ``body`` feeds the trigram index and therefore bm25 ranking.
DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5("
    "id UNINDEXED, body, created_at UNINDEXED, agent_id UNINDEXED, meta UNINDEXED, "
    "tokenize='trigram')"
)


def fts_query(query: str) -> Optional[str]:
    """Turn a free-text query into a safe FTS5 MATCH expression.

    Split on whitespace; keep tokens of length >= MIN_TOKEN; wrap each kept token
    in double quotes (doubling any embedded double quote); join with `` OR ``.
    Return None when no usable token remains.
    """
    if not isinstance(query, str):
        return None
    kept: List[str] = []
    for tok in query.split():
        if len(tok) >= MIN_TOKEN:
            kept.append('"' + tok.replace('"', '""') + '"')
    if not kept:
        return None
    return " OR ".join(kept)


def connect(db_path: Any) -> Optional[sqlite3.Connection]:
    """Open ``db_path`` (creating parent dirs + the FTS table); None on failure."""
    p = Path(str(db_path))
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    try:
        conn.execute(DDL)
        conn.commit()
    except sqlite3.Error:
        conn.close()
        return None
    return conn


def _meta_json(metadata: Any) -> Optional[str]:
    if metadata is None:
        return None
    try:
        return json.dumps(metadata, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def build_index(db_path: Any, items: Iterable[Dict[str, Any]]) -> int:
    """Rebuild the index from scratch. Each item: {id, text, created_at?,
    agent_id?, metadata?}. Returns the number of rows written."""
    p = Path(str(db_path))
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    try:
        conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
        conn.execute(DDL)
        count = 0
        rows: List[Tuple[str, str, Any, Any, Optional[str]]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if not isinstance(text, str) or not text:
                continue
            mid = item.get("id")
            rows.append(
                (
                    "" if mid is None else str(mid),
                    text,
                    item.get("created_at"),
                    item.get("agent_id"),
                    _meta_json(item.get("metadata")),
                )
            )
            count += 1
        if rows:
            conn.executemany(
                f"INSERT INTO {TABLE}(id, body, created_at, agent_id, meta) "
                f"VALUES (?, ?, ?, ?, ?)",
                rows,
            )
        conn.commit()
        return count
    finally:
        conn.close()


def add_one(
    db_path: Any,
    mem_id: str,
    text: str,
    created_at: Any = None,
    agent_id: Any = None,
    metadata: Any = None,
) -> bool:
    """Idempotent incremental upsert of a single memory (delete-by-id then insert).
    Best-effort: returns False (never raises) when the text is empty or on error."""
    if not isinstance(text, str) or not text:
        return False
    try:
        conn = connect(db_path)
        if conn is None:
            return False
        try:
            sid = "" if mem_id is None else str(mem_id)
            conn.execute(f"DELETE FROM {TABLE} WHERE id = ?", (sid,))
            conn.execute(
                f"INSERT INTO {TABLE}(id, body, created_at, agent_id, meta) "
                f"VALUES (?, ?, ?, ?, ?)",
                (sid, text, created_at, agent_id, _meta_json(metadata)),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def delete_one(db_path: Any, mem_id: str) -> bool:
    """Remove a single memory from the index (no-op when absent/never built).
    Best-effort: returns False on any error, never raises. Keeps the sidecar in
    sync with deletes issued against the vector store (B5 hook in main.py)."""
    try:
        p = Path(str(db_path))
        if not p.exists():
            return False
        conn = sqlite3.connect(str(p))
        try:
            conn.execute(
                f"DELETE FROM {TABLE} WHERE id = ?",
                ("" if mem_id is None else str(mem_id),),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def clear(db_path: Any) -> bool:
    """Drop all rows from the index (used after a full store reset). Best-effort."""
    try:
        p = Path(str(db_path))
        if not p.exists():
            return False
        conn = sqlite3.connect(str(p))
        try:
            conn.execute(f"DELETE FROM {TABLE}")
            conn.commit()
            return True
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def count(db_path: Any) -> int:
    """Row count of the index, or -1 when it does not exist / is unreadable."""
    p = Path(str(db_path))
    if not p.exists():
        return -1
    try:
        conn = sqlite3.connect(str(p))
        try:
            row = conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except sqlite3.Error:
        return -1


def search(db_path: Any, query: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Return matching records best-first (by bm25), at most ``limit``.

    Each record: {id, text, created_at, agent_id, metadata, bm25}. Returns [] when
    the query has no usable token, the index is absent, or nothing matches. Never
    raises on a malformed user query.
    """
    q = fts_query(query)
    if q is None:
        return []
    p = Path(str(db_path))
    if not p.exists():
        return []
    try:
        conn = sqlite3.connect(str(p))
        try:
            rows = conn.execute(
                f"SELECT id, body, created_at, agent_id, meta, bm25({TABLE}) "
                f"FROM {TABLE} WHERE {TABLE} MATCH ? ORDER BY rank LIMIT ?",
                (q, int(limit)),
            ).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, ValueError, TypeError):
        return []
    out: List[Dict[str, Any]] = []
    for r in rows:
        meta = None
        if r[4]:
            try:
                meta = json.loads(r[4])
            except (TypeError, ValueError):
                meta = None
        out.append(
            {
                "id": str(r[0]),
                "text": r[1],
                "created_at": r[2],
                "agent_id": r[3],
                "metadata": meta,
                "bm25": float(r[5]) if r[5] is not None else None,
            }
        )
    return out


def rrf_fuse(
    ranked_lists: Sequence[Sequence[str]], k: int = 60
) -> List[Tuple[str, float]]:
    """Reciprocal Rank Fusion over lists ordered best-first (rank 0 = best).

    score(id) = sum over lists of 1/(k + rank). Returns [(id, score)] sorted by
    score descending, ties broken by first-appearance order (stable)."""
    scores: Dict[str, float] = {}
    order: List[str] = []
    for lst in ranked_lists:
        for rank, mem_id in enumerate(lst):
            if mem_id not in scores:
                order.append(mem_id)
                scores[mem_id] = 0.0
            scores[mem_id] += 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], order.index(kv[0])))
