"""Build / refresh the lexical FTS5 index (``lexical.py``) from the memory store.

The index is a derived, disposable companion of the vector store: every row is
sourced from the store's payload (``data`` = memory text). Rebuild is
idempotent and safe to run at any time.

Sources (B5): the primary source is the single-file ``sqlite_vec`` store
(``meta_<collection>`` table, read-only) selected automatically when
``MEM0_VECTOR_STORE=sqlite_vec`` -- no ``qdrant-client`` needed. The legacy
Qdrant scroll path is kept for rollback (``MEM0_VECTOR_STORE=qdrant`` or
``--source qdrant``) and is imported lazily.

CLI (run with the appliance venv python):
    python lexical_index.py --db <path> [--user demo]        # auto source
    python lexical_index.py --db <path> --source sqlite --source-db <path>
    python lexical_index.py --db <path> --source qdrant --host 127.0.0.1 --port 6333
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Any, Dict, Iterator, List, Optional

import lexical

# Mirrors main.py::_RESERVED_PAYLOAD_KEYS -- these map to top-level fields, the
# rest of the payload becomes the ``metadata`` dict.
_RESERVED_PAYLOAD_KEYS = {
    "data",
    "user_id",
    "agent_id",
    "run_id",
    "hash",
    "created_at",
    "updated_at",
    "expiration_date",
}
PAGE = 1000


def _metadata(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in payload.items() if k not in _RESERVED_PAYLOAD_KEYS}


def iter_items(
    host: str, port: int, collection: str, user_id: Optional[str]
) -> Iterator[Dict[str, Any]]:
    """Yield {'id','text','created_at','agent_id','metadata'} for every point."""
    from qdrant_client import models
    from qdrant_client import QdrantClient

    client = QdrantClient(host=host, port=int(port))
    flt = None
    if user_id:
        flt = models.Filter(
            must=[models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id))]
        )
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            scroll_filter=flt,
            limit=PAGE,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for p in points:
            payload = getattr(p, "payload", None) or {}
            yield {
                "id": str(getattr(p, "id", "")),
                "text": payload.get("data"),
                "created_at": payload.get("created_at"),
                "agent_id": payload.get("agent_id"),
                "metadata": _metadata(payload),
            }
        if offset is None:
            break


def iter_items_sqlite(
    source_db: str, collection: str, user_id: Optional[str]
) -> Iterator[Dict[str, Any]]:
    """Yield {'id','text','created_at','agent_id','metadata'} from the
    single-file sqlite_vec store (read-only; same record shape as iter_items).

    ``collection`` selects the ``meta_<collection>`` table; the memory body
    lives in ``payload["data"]`` -- identical payload shape to the Qdrant
    points this store was migrated from (B3)."""
    table = f"meta_{collection}"
    conn = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    try:
        sql = f"SELECT id, payload FROM {table}"
        params: List[Any] = []
        if user_id:
            sql += " WHERE user_id = ?"
            params.append(user_id)
        for pid, payload_json in conn.execute(sql, params):
            try:
                payload = json.loads(payload_json)
            except (TypeError, ValueError):
                continue
            yield {
                "id": pid,
                "text": payload.get("data"),
                "created_at": payload.get("created_at"),
                "agent_id": payload.get("agent_id"),
                "metadata": _metadata(payload),
            }
    finally:
        conn.close()


def build(
    db_path: str,
    host: str,
    port: int,
    collection: str,
    user_id: Optional[str] = None,
) -> int:
    """Full rebuild from Qdrant (legacy/rollback source); returns rows written."""
    return lexical.build_index(db_path, iter_items(host, port, collection, user_id))


def build_sqlite(
    db_path: str, source_db: str, collection: str, user_id: Optional[str] = None
) -> int:
    """Full rebuild from the single-file sqlite_vec store; returns rows written."""
    return lexical.build_index(
        db_path, iter_items_sqlite(source_db, collection, user_id)
    )


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v else default


def build_from_env(db_path: str, user_id: Optional[str] = None) -> int:
    """Convenience for the gateway: pick the source from the environment.

    - ``MEM0_VECTOR_STORE=sqlite_vec`` -> read ``SQLITE_VEC_PATH`` (single-file
      store; collection from ``POSTGRES_COLLECTION_NAME``). No qdrant-client.
    - otherwise (qdrant, incl. rollback .env) -> Qdrant scroll via
      ``QDRANT_HOST`` / ``QDRANT_PORT`` / ``POSTGRES_COLLECTION_NAME``.
    """
    collection = _env("POSTGRES_COLLECTION_NAME", "mem0")
    if _env("MEM0_VECTOR_STORE", "qdrant") == "sqlite_vec":
        source_db = _env("SQLITE_VEC_PATH", "")
        if not source_db:
            raise RuntimeError(
                "MEM0_VECTOR_STORE=sqlite_vec but SQLITE_VEC_PATH is not set"
            )
        return build_sqlite(db_path, source_db, collection, user_id)
    host = _env("QDRANT_HOST", "127.0.0.1")
    port = int(_env("QDRANT_PORT", "6333"))
    return build(db_path, host, port, collection, user_id)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Rebuild the lexical FTS5 index (sqlite_vec store or Qdrant)."
    )
    ap.add_argument("--db", default="./lexical_index.db")
    ap.add_argument(
        "--source",
        choices=("auto", "sqlite", "qdrant"),
        default="auto",
        help="auto = follow MEM0_VECTOR_STORE (default qdrant when unset)",
    )
    ap.add_argument("--source-db", default=_env("SQLITE_VEC_PATH", ""),
                    help="sqlite_vec store path (source=sqlite)")
    ap.add_argument("--collection", default=_env("POSTGRES_COLLECTION_NAME", "mem0"))
    ap.add_argument("--host", default=_env("QDRANT_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(_env("QDRANT_PORT", "6333")))
    ap.add_argument("--user", default="demo")
    args = ap.parse_args(argv)
    if args.source == "sqlite" or (
        args.source == "auto" and _env("MEM0_VECTOR_STORE", "qdrant") == "sqlite_vec"
    ):
        source_db = args.source_db
        if not source_db:
            ap.error("--source-db / SQLITE_VEC_PATH required for sqlite source")
        n = build_sqlite(args.db, source_db, args.collection, args.user)
        print(
            f"lexical index built: db={args.db} source=sqlite({source_db}) rows={n}"
        )
    else:
        n = build(args.db, args.host, args.port, args.collection, args.user)
        print(f"lexical index built: db={args.db} collection={args.collection} rows={n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
