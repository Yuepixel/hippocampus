#!/usr/bin/env python3
"""D2 single-pool convergence: local Qdrant `mem0` -> mini single-file sqlite store.

Reuses the three-stage pattern of `scripts/b-migrate.py` (export / load /
verify), specialized for an **incremental append** convergence:

  ids-qdrant  Qdrant scroll (id only) -> TSV ``id\\tcreated_at``        [READ-ONLY]
  ids-sqlite  sqlite_vec meta_<col>  -> TSV ``id\\tcreated_at``        [READ-ONLY]
  diff        two id TSVs -> ids only in A (in A not B) + counts
  export      Qdrant scroll -> JSONL (all, or only ids in --ids-file)  [READ-ONLY]
  load        JSONL -> sqlite db, APPEND with per-id dedupe            [WRITES db]
  verify      db vs JSONL: count + id-set + structural + sampled L2 + payload

`load`/`verify` need the appliance provider (`server/sqlite_vec_store.py`) and
`sqlite-vec`; run them on mini with its venv python (pass --site-packages /
--server-dir). `ids-*`/`diff`/`export` are stdlib-only (export needs network to
Qdrant).

Example (D2, 2026-10-14):
  python scripts/d2-converge.py ids-qdrant --out local_ids.tsv
  ssh mini 'python3 d2-converge.py ids-sqlite --db .../mem0.db --out /tmp/mini_ids.tsv'
  python scripts/d2-converge.py diff --a local_ids.tsv --b mini_ids.tsv --out new.tsv
  python scripts/d2-converge.py export --ids-file new.tsv --out new.jsonl
  scp new.jsonl mini:/tmp/d2/
  ssh mini '.../venv/bin/python d2-converge.py load --jsonl /tmp/d2/new.jsonl --db .../mem0.db'
  ssh mini '.../venv/bin/python d2-converge.py verify --jsonl /tmp/d2/new.jsonl --db .../mem0.db'
"""

import argparse
import gzip
import hashlib
import json
import os
import random
import sqlite3
import struct
import sys
import time
import urllib.request


def open_jsonl(path):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "r", encoding="utf-8")


def http_json(url, payload=None, timeout=120, retries=4):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"HTTP failed after {retries} tries: {url} ({last})")


def dense_vector(vector):
    if isinstance(vector, list):
        return vector
    if isinstance(vector, dict):
        if "" in vector and isinstance(vector[""], list):
            return vector[""]
        for key, val in vector.items():
            if key != "bm25" and isinstance(val, list):
                return val
        raise ValueError(f"no dense vector found in keys {sorted(vector)}")
    raise TypeError(f"unexpected vector type {type(vector)!r}")


def created_at_of(payload):
    if not isinstance(payload, dict):
        return ""
    ca = payload.get("created_at")
    if ca:
        return str(ca)
    meta = payload.get("metadata")
    if isinstance(meta, dict) and meta.get("created_at"):
        return str(meta["created_at"])
    return ""


def load_ids(path):
    ids = set()
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                ids.add(line.split("\t")[0])
    return ids


# ----------------------------------------------------------- ids-qdrant ----

def cmd_ids_qdrant(args):
    offset, n = None, 0
    with open(args.out, "w", encoding="utf-8") as fh:
        while True:
            body = {"limit": args.limit, "with_vector": False, "with_payload": True}
            if offset is not None:
                body["offset"] = offset
            res = http_json(
                f"{args.qdrant}/collections/{args.collection}/points/scroll", body)["result"]
            pts = res.get("points", [])
            for p in pts:
                fh.write(f"{p['id']}\t{created_at_of(p.get('payload') or {})}\n")
            n += len(pts)
            offset = res.get("next_page_offset")
            if offset is None:
                break
    print(f"ids-qdrant: wrote {n} -> {args.out}")
    return 0


# ----------------------------------------------------------- ids-sqlite ----

def cmd_ids_sqlite(args):
    meta = f"meta_{args.collection}"
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    n = 0
    with open(args.out, "w", encoding="utf-8") as fh:
        for pid, payload_json in conn.execute(f"SELECT id, payload FROM {meta}"):
            try:
                payload = json.loads(payload_json)
            except (TypeError, ValueError):
                payload = {}
            fh.write(f"{pid}\t{created_at_of(payload)}\n")
            n += 1
    conn.close()
    print(f"ids-sqlite: wrote {n} -> {args.out}")
    return 0


# ----------------------------------------------------------------- diff ----

def cmd_diff(args):
    a = load_ids(args.a)
    b = load_ids(args.b)
    only_a, only_b = a - b, b - a
    with open(args.out, "w", encoding="utf-8") as fh:
        for i in sorted(only_a):
            fh.write(i + "\n")
    print(f"diff: a={len(a)} b={len(b)} only_in_a={len(only_a)} only_in_b={len(only_b)} -> {args.out}")
    if only_b:
        print(f"  WARNING only_in_b (first 5): {sorted(only_b)[:5]}")
    return 0


# --------------------------------------------------------------- export ----

def cmd_export(args):
    wanted = load_ids(args.ids_file) if args.ids_file else None
    info = http_json(f"{args.qdrant}/collections/{args.collection}")["result"]
    src_count = info.get("points_count")
    offset, done = None, 0
    sha = hashlib.sha256()
    with open(args.out, "w", encoding="utf-8") as fh:
        while True:
            body = {"limit": args.limit, "with_vector": True, "with_payload": True}
            if offset is not None:
                body["offset"] = offset
            res = http_json(
                f"{args.qdrant}/collections/{args.collection}/points/scroll", body)["result"]
            pts = res.get("points", [])
            for p in pts:
                if wanted is not None and p["id"] not in wanted:
                    continue
                rec = {"id": p["id"], "vector": dense_vector(p["vector"]),
                       "payload": p.get("payload") or {}}
                line = json.dumps(rec, ensure_ascii=False)
                fh.write(line + "\n")
                sha.update(line.encode("utf-8"))
                done += 1
            offset = res.get("next_page_offset")
            if offset is None:
                break
    manifest = {"collection": args.collection, "source_points_count": src_count,
                "exported": done, "filtered_by_ids": wanted is not None,
                "sha256": sha.hexdigest(), "seconds": None}
    with open(args.out + ".manifest.json", "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    print(f"export: exported {done} (source {src_count}) -> {args.out} "
          f"sha256={sha.hexdigest()[:16]}…")
    if wanted is not None and done != len(wanted):
        print(f"  WARNING wanted {len(wanted)} != exported {done} (some ids not in collection?)")
    return 0


# ----------------------------------------------------------------- load ----

def _import_provider(site_packages, server_dir):
    for path in (server_dir, site_packages):
        if path and path not in sys.path:
            sys.path.insert(0, path)
    import sqlite_vec  # noqa: F401
    from mem0.vector_stores.base import VectorStoreBase  # noqa: F401
    import sqlite_vec_store
    return sqlite_vec_store


def cmd_load(args):
    svs = _import_provider(args.site_packages, args.server_dir)
    store = svs.SQLiteVecStore(db_path=args.db, collection_name=args.collection,
                               embedding_model_dims=args.dim, distance="cosine")
    before = store.col_info()["count"]
    added = skipped = 0
    batch = {"ids": [], "vecs": [], "pays": []}

    def flush():
        nonlocal added
        if batch["ids"]:
            store.insert(vectors=batch["vecs"], payloads=batch["pays"], ids=batch["ids"])
            added += len(batch["ids"])
            batch["ids"], batch["vecs"], batch["pays"] = [], [], []

    with open_jsonl(args.jsonl) as fh:
        for line in fh:
            rec = json.loads(line)
            if store.get(rec["id"]) is not None:
                skipped += 1
                continue
            batch["ids"].append(rec["id"])
            batch["vecs"].append(rec["vector"])
            batch["pays"].append(rec["payload"])
            if len(batch["ids"]) >= args.batch:
                flush()
    flush()
    after = store.col_info()["count"]
    store.close()
    print(f"load: before={before} added={added} skipped_existing={skipped} after={after}")
    return 0


# --------------------------------------------------------------- verify ----

def cmd_verify(args):
    _import_provider(args.site_packages, args.server_dir)
    conn = sqlite3.connect(args.db)
    conn.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    vec_t, meta_t, fts_t = (f"vec_{args.collection}", f"meta_{args.collection}",
                            f"fts_{args.collection}")
    (vec_n,) = conn.execute(f"SELECT COUNT(*) FROM {vec_t}").fetchone()
    db_ids = {r[0] for r in conn.execute(f"SELECT id FROM {meta_t}")}
    (fts_n,) = conn.execute(f"SELECT COUNT(*) FROM {fts_t}").fetchone()
    print(f"db: vec={vec_n} meta={len(db_ids)} fts={fts_n}")

    jsonl_ids, sample_vecs, sample_pays = set(), {}, {}
    sample = set()
    if args.sample:
        sample = set(random.Random(args.seed).sample(
            sorted(db_ids), min(args.sample, len(db_ids))))
    with open_jsonl(args.jsonl) as fh:
        for line in fh:
            rec = json.loads(line)
            jsonl_ids.add(rec["id"])
            if rec["id"] in sample:
                sample_vecs[rec["id"]] = rec["vector"]
                sample_pays[rec["id"]] = rec["payload"]
    print(f"jsonl: {len(jsonl_ids)} unique ids")

    fails = []
    missing = jsonl_ids - db_ids
    if missing:
        fails.append(f"jsonl ids missing from db: {sorted(missing)[:5]} ...({len(missing)})")

    l2_max, worst, pay_bad = 0.0, None, 0
    for pid in sample_vecs:
        row = conn.execute(f"SELECT rowid FROM {meta_t} WHERE id = ?", (pid,)).fetchone()
        (blob,) = conn.execute(f"SELECT embedding FROM {vec_t} WHERE rowid = ?", (row[0],)).fetchone()
        db_vec = struct.unpack(f"{args.dim}f", blob)
        l2 = sum((a - b) ** 2 for a, b in zip(db_vec, sample_vecs[pid])) ** 0.5
        if l2 > l2_max:
            l2_max, worst = l2, pid
        if l2 > args.eps:
            fails.append(f"vector L2 {l2:.2e} > eps {args.eps:g} for {pid}")
        if sample_pays.get(pid) != json.loads(conn.execute(
                f"SELECT payload FROM {meta_t} WHERE id = ?", (pid,)).fetchone()[0]):
            pay_bad += 1
    if sample:
        print(f"sampled {len(sample)} vectors: max L2={l2_max:.3e} (eps {args.eps:g}); "
              f"payload mismatches={pay_bad}")
    if pay_bad:
        fails.append(f"payload mismatch on {pay_bad}/{len(sample)} sampled ids")
    if vec_n != len(db_ids) or fts_n != len(db_ids):
        fails.append(f"structural: vec={vec_n} meta={len(db_ids)} fts={fts_n} unequal")

    # optional global reconciliation vs an authoritative id list
    if args.ids_file:
        ref = load_ids(args.ids_file)
        print(f"ref ids={len(ref)} db={len(db_ids)}")
        only_ref, only_db = ref - db_ids, db_ids - ref
        if only_ref:
            fails.append(f"ids in ref not db: {len(only_ref)} e.g. {sorted(only_ref)[:3]}")
        if only_db:
            fails.append(f"ids in db not ref: {len(only_db)} e.g. {sorted(only_db)[:3]}")
        if not only_ref and not only_db:
            print("reconcile: db id-set == ref id-set (EXACT)")

    conn.close()
    if fails:
        print("\nVERIFY FAILED:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("\nVERIFY PASSED")
    return 0


# ----------------------------------------------------------------- main ----

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="stage", required=True)

    a1 = sub.add_parser("ids-qdrant")
    a1.add_argument("--qdrant", default="http://127.0.0.1:6333")
    a1.add_argument("--collection", default="mem0")
    a1.add_argument("--out", required=True)
    a1.add_argument("--limit", type=int, default=512)
    a1.set_defaults(fn=cmd_ids_qdrant)

    a2 = sub.add_parser("ids-sqlite")
    a2.add_argument("--db", required=True)
    a2.add_argument("--collection", default="mem0")
    a2.add_argument("--out", required=True)
    a2.set_defaults(fn=cmd_ids_sqlite)

    a3 = sub.add_parser("diff")
    a3.add_argument("--a", required=True)
    a3.add_argument("--b", required=True)
    a3.add_argument("--out", required=True)
    a3.set_defaults(fn=cmd_diff)

    a4 = sub.add_parser("export")
    a4.add_argument("--qdrant", default="http://127.0.0.1:6333")
    a4.add_argument("--collection", default="mem0")
    a4.add_argument("--out", required=True)
    a4.add_argument("--ids-file", default=None)
    a4.add_argument("--limit", type=int, default=512)
    a4.set_defaults(fn=cmd_export)

    a5 = sub.add_parser("load")
    a5.add_argument("--jsonl", required=True)
    a5.add_argument("--db", required=True)
    a5.add_argument("--collection", default="mem0")
    a5.add_argument("--dim", type=int, default=1024)
    a5.add_argument("--batch", type=int, default=256)
    a5.add_argument("--site-packages", default="")
    a5.add_argument("--server-dir", default="")
    a5.set_defaults(fn=cmd_load)

    a6 = sub.add_parser("verify")
    a6.add_argument("--jsonl", required=True)
    a6.add_argument("--db", required=True)
    a6.add_argument("--collection", default="mem0")
    a6.add_argument("--dim", type=int, default=1024)
    a6.add_argument("--sample", type=int, default=100)
    a6.add_argument("--eps", type=float, default=1e-3)
    a6.add_argument("--seed", type=int, default=42)
    a6.add_argument("--ids-file", default=None)
    a6.add_argument("--site-packages", default="")
    a6.add_argument("--server-dir", default="")
    a6.set_defaults(fn=cmd_verify)

    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
