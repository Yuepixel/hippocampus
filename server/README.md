# Hippocampus appliance server (source of truth for the appliance fork)

This directory holds the **appliance-side** copy of the memory gateway, evolved from the
`memory-service` project's `server/` tree (that tree stays read-only; the appliance
only ever *reads/fork* from it).

## Files

- `gateway.py` — forked from the `memory-service` gateway
  (base md5 `bf333bc45377dadc4bb03c48b2335095`), plus the B2 lexical-side-channel
  patch (see below).
- `lexical.py` — stdlib-only SQLite **FTS5 `tokenize='trigram'`** side-channel
  (disposable, rebuildable from the active store). No network, no vector-store
  imports. B5 adds `delete_one` / `clear` for the delete hooks.
- `lexical_index.py` — builds/refreshes the FTS index from the active store.
  B5: source follows `MEM0_VECTOR_STORE` — `sqlite_vec` reads the single-file
  db's `meta_<collection>` table (read-only, **no `qdrant-client`**); `qdrant`
  keeps the legacy scroll path (rollback).
- `main.py` — appliance fork of the mem0 REST server. B4 adds the `sqlite_vec`
  vector-store branch; B5 adds best-effort lexical sidecar hooks:
  `_lexical_forget` (single delete), `_lexical_index_results` (direct
  `POST /memories` writes), `_lexical_rebuild_async` (bulk delete / reset —
  ids unknown), wired into `delete_memory` / `delete_all_memories` /
  `reset_memory` / `ingest_guard._delete`. All hooks are never fatal.
- `gateway_config.example.json` — appliance config template (includes the
  `lexical` block).
- `sqlite_vec_store.py` — mem0 `VectorStoreBase` provider backed by a
  **single-file SQLite db** (`sqlite-vec` 0.1.9 `vec0` + metadata table + FTS5
  trigram). Verified against mem0ai 2.2.1 by a 100-entry
  add/search/delete smoke loop (19 checks). See
  `docs/整体计划.md`.

## sqlite_vec provider (Qdrant→SQLite migration track)

`SQLiteVecStore` implements mem0 2.2.1's contract: `search()` returns
`OutputData(id, score∈[0,1], payload)` (cosine distance → `1 - distance`);
`user_id` is a native `vec0` metadata column (KNN push-down); other equality
filters (agent_id/run_id/eq/in) are post-filtered after an over-fetched KNN.
`keyword_search()` serves FTS5 trigram lookups. Register with
`sqlite_vec_store.register()` (monkeypatches `VectorStoreFactory.provider_to_class`).

## B2 — lexical channel + RRF fusion

When `lexical.enabled=true` **and** `rerank=false`, `ranked_items` merges the dense
candidate list with the FTS candidate list via **Reciprocal Rank Fusion** (k from
`lexical.rrfK`, default 60). Ranking relevance = RRF normalised so the top item ≈ 1.
Retention gate = `dense cosine >= threshold` **or** a lexical hit (this is the whole
point: recover exact path/port/version/ID/date matches the dense retriever misses).
`cross-encoder` rerank stays off. `recencyWeight` still blends in as before.

- Index build: `python lexical_index.py --db <path> [--source sqlite|qdrant] ...`
  (CLI; `auto` follows `MEM0_VECTOR_STORE`), `POST /api/lexical/rebuild`,
  or lazily on first recall when the index file is missing/empty
  (`lexical.autoBuildMissing`).
- New writes are appended best-effort in `run_remember` (`lexical.add_one`);
  B5: direct `POST /memories` writes too (`_lexical_index_results`), and every
  delete path removes the row (`lexical.delete_one`) or triggers a background
  rebuild (bulk delete / reset) — the sidecar can no longer serve deleted
  memories via RRF.

## Deploy note

Deploy target on the GB10 worker **102** (`worker-node`; the earlier Mac mini is
retired): `/opt/hippocampus/server/` (venv
`/opt/hippocampus/venv/bin/python`). Config at
`/opt/hippocampus/server/gateway_config.json`; index DB default
`/opt/hippocampus/data/lexical/lexical.db` via `lexical.dbPath`.
