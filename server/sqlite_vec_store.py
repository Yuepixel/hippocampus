"""mem0 vector_store provider backed by a single-file SQLite database (sqlite-vec).

Implements ``mem0.vector_stores.base.VectorStoreBase`` for the Hippocampus
appliance: one ``.db`` file holding

- a ``vec0`` virtual table (dense vectors, cosine distance, ``user_id``
  metadata column for native KNN push-down),
- a metadata table mapping mem0 uuid ids to vec0 rowids plus the full payload
  JSON (memory body lives in ``payload["data"]``),
- an FTS5 (trigram) lexical sidecar table.

All ``search()`` results are returned with similarity scores in ``[0, 1]``
(higher = more similar): ``score = max(0, 1 - cosine_distance)`` (L2:
``1 / (1 + distance)``), matching the contract documented in mem0 2.2.1
``vector_stores/base.py``.

Target mem0ai version: **2.2.1** (signatures verified against its
``vector_stores/base.py``; see Hippocampus ``docs/整体计划.md``).

Registration (server_state-style monkeypatch; the module only needs to be
importable, e.g. living next to ``gateway.py`` on ``sys.path``)::

    from mem0.utils.factory import VectorStoreFactory
    import sqlite_vec_store
    sqlite_vec_store.register()

    # .env: MEM0_VECTOR_STORE=sqlite_vec
    # vector_store config: {"provider": "sqlite_vec", "config": {
    #     "db_path": "...", "embedding_model_dims": 1024, ...}}
"""

import json
import logging
import sqlite3
import struct
import threading
import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from mem0.vector_stores.base import VectorStoreBase

logger = logging.getLogger(__name__)

__version__ = "0.1.0"

# vec0 KNN over-fetch factor when extra (non user_id) filters must be applied
# in Python after the native filtered KNN.
_OVERFETCH = 4


class OutputData(BaseModel):
    """Same shape as the OutputData used by mem0's bundled providers."""

    id: Optional[str]
    score: Optional[float]
    payload: Optional[Dict]


def register() -> None:
    """Register this provider with mem0's VectorStoreFactory.

    Two layers are patched, both mem0 2.2.1 verified:

    1. ``VectorStoreFactory.provider_to_class`` so ``Memory`` instantiates us.
    2. ``VectorStoreConfig`` (pydantic model validator) which rejects unknown
       providers: inject a synthetic ``mem0.configs.vector_stores.sqlite_vec``
       module plus a ``_provider_configs`` entry. ``_provider_configs`` is a
       pydantic ``ModelPrivateAttr``; its ``.default`` dict is patched before
       any instance is created (main.py imports this module at config time).
    """
    import sys
    import types

    from mem0.utils.factory import VectorStoreFactory

    VectorStoreFactory.provider_to_class["sqlite_vec"] = "sqlite_vec_store.SQLiteVecStore"

    mod_name = "mem0.configs.vector_stores.sqlite_vec"
    if mod_name not in sys.modules:
        from pydantic import BaseModel, ConfigDict

        module = types.ModuleType(mod_name)

        class SQLiteVecConfig(BaseModel):
            """Config shim; fields match SQLiteVecStore.__init__ kwargs."""

            model_config = ConfigDict(extra="allow")

            db_path: str = "/tmp/sqlite_vec.db"
            collection_name: str = "memories"
            embedding_model_dims: int = 1024
            distance: str = "cosine"

        module.SQLiteVecConfig = SQLiteVecConfig
        sys.modules[mod_name] = module

    from mem0.vector_stores.configs import VectorStoreConfig

    priv = VectorStoreConfig.__private_attributes__.get("_provider_configs")
    if priv is not None:
        mapping = dict(priv.default) if isinstance(priv.default, dict) else {}
        mapping["sqlite_vec"] = "SQLiteVecConfig"
        priv.default = mapping
    else:  # pragma: no cover - mem0 changed internals
        logger.warning("VectorStoreConfig has no _provider_configs private attr")

    logger.info("registered mem0 vector_store provider 'sqlite_vec'")


def _to_blob(vector: List[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _distance_to_similarity(distance: float, metric: str) -> float:
    if metric == "cosine":
        return max(0.0, 1.0 - float(distance))
    return 1.0 / (1.0 + float(distance))


class SQLiteVecStore(VectorStoreBase):
    def __init__(
        self,
        db_path: str,
        collection_name: str = "memories",
        embedding_model_dims: int = 1024,
        distance: str = "cosine",
    ):
        """
        Args:
            db_path: Path to the single SQLite database file (created if absent).
            collection_name: mem0 collection name; used as table-name prefix so
                one file can technically host several collections.
            embedding_model_dims: Vector dimension (must match all inserts).
            distance: "cosine" (default) or "l2". Similarity is always mapped
                to [0, 1] per the mem0 contract.
        """
        if distance not in ("cosine", "l2"):
            raise ValueError(f"unsupported distance {distance!r} (cosine|l2)")
        self.db_path = db_path
        self.collection_name = collection_name
        self.embedding_model_dims = embedding_model_dims
        self.distance = distance
        self.vec_table = f"vec_{collection_name}"
        self.meta_table = f"meta_{collection_name}"
        self.fts_table = f"fts_{collection_name}"

        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self._load_vec_extension()
        self._ensure_schema()
        self._lock = threading.RLock()

    # -- setup -----------------------------------------------------------

    def _load_vec_extension(self) -> None:
        import sqlite_vec

        self.db.enable_load_extension(True)
        sqlite_vec.load(self.db)
        self.db.enable_load_extension(False)
        (version,) = self.db.execute("select vec_version()").fetchone()
        logger.info("sqlite-vec %s loaded for %s", version, self.db_path)

    def _ensure_schema(self) -> None:
        cur = self.db
        cur.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {self.vec_table} USING vec0("
            f"embedding float[{self.embedding_model_dims}] distance_metric={self.distance},"
            f"user_id text)"
        )
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {self.meta_table} ("
            f"id TEXT PRIMARY KEY, rowid INTEGER UNIQUE, user_id TEXT, payload TEXT NOT NULL)"
        )
        # Content-free FTS5 (trigram): ext_id links back to the meta table.
        cur.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {self.fts_table} "
            f"USING fts5(ext_id UNINDEXED, body, tokenize='trigram')"
        )
        cur.commit()

    # -- filters ---------------------------------------------------------

    @staticmethod
    def _equality_filter(filters: Optional[Dict]) -> Dict[str, Any]:
        """Normalize mem0 filters to a flat {field: scalar-or-list} dict.

        Supports the scalar / eq / in forms mem0 passes for user_id, agent_id,
        run_id, actor_id. Anything richer (range ops, AND/OR/NOT trees,
        contains, ...) raises: prototype scope is the identity-filter path the
        gateway actually uses. ``"*"`` wildcards are ignored.
        """
        if not filters:
            return {}
        out: Dict[str, Any] = {}
        for key, value in filters.items():
            if key in ("AND", "OR", "NOT", "$and", "$or", "$not"):
                raise NotImplementedError(
                    f"SQLiteVecStore prototype does not support logical filters, got {key!r}"
                )
            if isinstance(value, dict):
                if set(value) == {"eq"}:
                    value = value["eq"]
                elif set(value) == {"in"} and isinstance(value["in"], list):
                    value = value["in"]
                else:
                    raise NotImplementedError(
                        f"SQLiteVecStore prototype only supports eq/in operators, got {key}: {set(value)}"
                    )
            if value == "*":
                continue
            if not isinstance(value, (str, int, float, list)):
                raise ValueError(f"unsupported filter value for {key!r}: {value!r}")
            out[key] = value
        return out

    # -- helpers ---------------------------------------------------------

    def _knn(self, vector, top_k: int, user_id: Optional[str],
             limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Run vec0 KNN; returns [{'id', 'score', 'payload'}] pre-OutputData.

        ``vector`` is a single embedding (list of floats) per the mem0 2.2.1
        contract (qdrant provider passes it straight to query_points); a
        single-element nested list is tolerated and unwrapped.
        """
        if vector and isinstance(vector[0], (list, tuple)):
            if len(vector) != 1:
                raise ValueError("search expects exactly one query vector")
            vector = vector[0]
        fetch = limit if limit is not None else top_k
        sql = f"SELECT rowid, distance FROM {self.vec_table} WHERE embedding MATCH ?"
        params: List[Any] = [_to_blob(vector)]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY distance LIMIT ?"
        params.append(fetch)
        hits = self.db.execute(sql, params).fetchall()
        if not hits:
            return []
        marks = ",".join("?" * len(hits))
        rows = self.db.execute(
            f"SELECT id, rowid, payload FROM {self.meta_table} WHERE rowid IN ({marks})",
            [h[0] for h in hits],
        ).fetchall()
        by_rowid = {r[1]: (r[0], r[2]) for r in rows}
        out = []
        for rowid, dist in hits:
            if rowid not in by_rowid:
                continue  # vec0 row without meta (should not happen)
            pid, payload_json = by_rowid[rowid]
            out.append({
                "id": pid,
                "score": _distance_to_similarity(dist, self.distance),
                "payload": json.loads(payload_json),
            })
        return out

    @staticmethod
    def _payload_matches(payload: Dict[str, Any], field: str, wanted: Any) -> bool:
        value = payload.get(field)
        if isinstance(wanted, list):
            return value in wanted
        return value == wanted

    def _post_filter(self, rows: List[Dict[str, Any]], filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            r for r in rows
            if all(self._payload_matches(r["payload"], k, v) for k, v in filters.items())
        ]

    # -- VectorStoreBase API ----------------------------------------------

    def create_col(self, name=None, vector_size=None, distance=None):
        """Create the collection's tables if missing (idempotent)."""
        with self._lock:
            if vector_size is not None:
                self.embedding_model_dims = int(vector_size)
            if distance is not None:
                self.distance = distance
            self._ensure_schema()

    def insert(self, vectors: List[List[float]], payloads: Optional[List[Dict]] = None,
               ids: Optional[List[str]] = None):
        with self._lock:
            n = len(vectors)
            if payloads is not None and len(payloads) != n:
                raise ValueError("payloads/vectors length mismatch")
            if ids is not None and len(ids) != n:
                raise ValueError("ids/vectors length mismatch")
            (max_rowid,) = self.db.execute(
                f"SELECT COALESCE(MAX(rowid), 0) FROM {self.vec_table}").fetchone()
            vec_rows, meta_rows, fts_rows = [], [], []
            for i, vector in enumerate(vectors):
                if len(vector) != self.embedding_model_dims:
                    raise ValueError(
                        f"vector dim {len(vector)} != {self.embedding_model_dims}")
                rowid = int(max_rowid) + 1 + i
                pid = str(ids[i]) if ids is not None else str(uuid.uuid4())
                payload = payloads[i] if payloads else {}
                payload_json = json.dumps(payload, ensure_ascii=False)
                user_id = payload.get("user_id")
                vec_rows.append((rowid, _to_blob(vector), user_id))
                meta_rows.append((pid, rowid, user_id, payload_json))
                fts_rows.append((pid, payload.get("data", "") or ""))
            self.db.execute("BEGIN")
            try:
                self.db.executemany(
                    f"INSERT INTO {self.vec_table}(rowid, embedding, user_id) VALUES (?, ?, ?)",
                    vec_rows)
                self.db.executemany(
                    f"INSERT INTO {self.meta_table}(id, rowid, user_id, payload) VALUES (?, ?, ?, ?)",
                    meta_rows)
                self.db.executemany(
                    f"INSERT INTO {self.fts_table}(ext_id, body) VALUES (?, ?)", fts_rows)
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            self.db.commit()

    def search(self, query: str, vectors: List[float], top_k: int = 5,
               filters: Optional[Dict] = None) -> List[OutputData]:
        flat = self._equality_filter(filters)
        user_id = flat.pop("user_id", None)
        with self._lock:
            fetch = top_k * _OVERFETCH if flat else top_k
            rows = self._knn(vectors, top_k, user_id, limit=fetch)
            if flat:
                rows = self._post_filter(rows, flat)[:top_k]
        return [OutputData(id=r["id"], score=r["score"], payload=r["payload"]) for r in rows]

    def keyword_search(self, query: str, top_k: int = 5,
                       filters: Optional[Dict] = None) -> Optional[List[OutputData]]:
        """FTS5 trigram lookup; score is rank-normalized 1/(1+rank0) in (0, 1].

        ``query`` is free text from callers (mem0 passes the raw lemmatized
        query) and must be converted to a safe MATCH expression: tokens >= 3
        chars are quoted and OR-ed (same rules as lexical.fts_query).
        """
        flat = self._equality_filter(filters)
        match_expr = self._fts_query(query)
        if not match_expr:
            return []
        with self._lock:
            rows = self.db.execute(
                f"SELECT ext_id, rank FROM {self.fts_table} WHERE {self.fts_table} MATCH ? "
                f"ORDER BY rank LIMIT ?", (match_expr, top_k)).fetchall()
            out = []
            for i, (ext_id, _rank) in enumerate(rows):
                payload = self._payload_by_id(ext_id)
                if payload is None:
                    continue
                if flat and not self._post_filter([{"payload": payload}], flat):
                    continue
                out.append(OutputData(id=ext_id, score=1.0 / (1.0 + i), payload=payload))
        return out

    @staticmethod
    def _fts_query(query: str, min_token: int = 3) -> Optional[str]:
        """Free text -> safe FTS5 MATCH expression (tokens quoted, OR-joined)."""
        if not isinstance(query, str):
            return None
        kept = ['"' + tok.replace('"', '""') + '"'
                for tok in query.split() if len(tok) >= min_token]
        return " OR ".join(kept) if kept else None

    def _payload_by_id(self, pid: str) -> Optional[Dict[str, Any]]:
        row = self.db.execute(
            f"SELECT payload FROM {self.meta_table} WHERE id = ?", (pid,)).fetchone()
        return json.loads(row[0]) if row else None

    def delete(self, vector_id: str):
        with self._lock:
            row = self.db.execute(
                f"SELECT rowid FROM {self.meta_table} WHERE id = ?", (str(vector_id),)).fetchone()
            if row is None:
                return
            self.db.execute("BEGIN")
            try:
                self.db.execute(f"DELETE FROM {self.vec_table} WHERE rowid = ?", (row[0],))
                self.db.execute(f"DELETE FROM {self.meta_table} WHERE id = ?", (str(vector_id),))
                self.db.execute(f"DELETE FROM {self.fts_table} WHERE ext_id = ?", (str(vector_id),))
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            self.db.commit()

    def update(self, vector_id: str, vector: Optional[List[float]] = None,
               payload: Optional[Dict] = None):
        """Update via delete + reinsert (keeps the same uuid)."""
        old_payload = self.get(vector_id)
        if old_payload is None:
            return
        old_vector = self._vector_by_id(vector_id)
        if vector is None and old_vector is None:
            raise ValueError(f"vector for id {vector_id!r} missing and no replacement given")
        with self._lock:
            self.delete(vector_id)
            self.insert(
                vectors=[vector if vector is not None else old_vector],
                payloads=[payload if payload is not None else old_payload.payload],
                ids=[vector_id],
            )

    def _vector_by_id(self, vector_id: str) -> Optional[List[float]]:
        row = self.db.execute(
            f"SELECT rowid FROM {self.meta_table} WHERE id = ?", (str(vector_id),)).fetchone()
        if row is None:
            return None
        (blob,) = self.db.execute(
            f"SELECT embedding FROM {self.vec_table} WHERE rowid = ?", (row[0],)).fetchone()
        return list(struct.unpack(f"{self.embedding_model_dims}f", blob))

    def get(self, vector_id: str) -> Optional[OutputData]:
        with self._lock:
            payload = self._payload_by_id(str(vector_id))
        if payload is None:
            return None
        return OutputData(id=str(vector_id), score=None, payload=payload)

    def list_cols(self) -> List[str]:
        row = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (self.meta_table,)).fetchone()
        return [self.collection_name] if row else []

    def delete_col(self):
        with self._lock:
            for table in (self.fts_table, self.meta_table, self.vec_table):
                self.db.execute(f"DROP TABLE IF EXISTS {table}")
            self.db.commit()

    def col_info(self) -> Dict[str, Any]:
        with self._lock:
            (count,) = self.db.execute(
                f"SELECT COUNT(*) FROM {self.meta_table}").fetchone()
        return {
            "name": self.collection_name,
            "count": count,
            "dim": self.embedding_model_dims,
            "distance": self.distance,
            "db_path": self.db_path,
        }

    def list(self, filters: Optional[Dict] = None, top_k: Optional[int] = 100) -> List[OutputData]:
        flat = self._equality_filter(filters)
        with self._lock:
            rows = self.db.execute(
                f"SELECT id, payload FROM {self.meta_table} LIMIT ?",
                (top_k if top_k is not None else -1,)).fetchall()
        out = []
        for pid, payload_json in rows:
            payload = json.loads(payload_json)
            if flat and not all(self._payload_matches(payload, k, v) for k, v in flat.items()):
                continue
            out.append(OutputData(id=pid, score=None, payload=payload))
        return out

    def reset(self):
        """Drop and recreate all tables in place."""
        self.delete_col()
        self._ensure_schema()

    def close(self):
        """Close the underlying connection (idempotent)."""
        with self._lock:
            self.db.close()
