#!/usr/bin/env python3
"""B3 migration: Qdrant scroll -> JSONL -> SQLiteVecStore db -> reconcile.

Three stages, runnable independently (idempotent, resumable):

  export  Qdrant scroll full corpus (vector+payload) -> JSONL (+ manifest).
          Pure stdlib; safe to run on mini against 127.0.0.1:6333. READ-ONLY.
  load    JSONL -> single-file SQLite db via server/sqlite_vec_store.py
          (needs sqlite-vec + mem0.base importable).
  verify  Reconcile db vs JSONL: point count, ID set, sampled vector L2,
          payload equality, plus structural counts (vec/meta/fts tables).

Examples (local x86, b2 venv):
  python scripts/b-migrate.py export --qdrant http://127.0.0.1:6333 \
      --collection mem0 --out /tmp/b3/mem0-export.jsonl
  python scripts/b-migrate.py load --jsonl mem0-export.jsonl --db mem0.db
  python scripts/b-migrate.py verify --jsonl mem0-export.jsonl --db mem0.db
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
    """Open a JSONL file for reading; transparent .gz support."""
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "r", encoding="utf-8")

DEFAULT_SITE_PACKAGES = r"C:\path\to\venv\Lib\site-packages"


# ---------------------------------------------------------------- http ----

def http_json(url, payload=None, timeout=120, retries=4):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - retry any network error
            last = exc
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"HTTP failed after {retries} tries: {url} ({last})")


def dense_vector(vector):
    """Return the dense list from a Qdrant point vector (list or named dict)."""
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


# --------------------------------------------------------------- export ----

def cmd_export(args):
    info = http_json(f"{args.qdrant}/collections/{args.collection}")["result"]
    src_count = info.get("points_count")
    vec_cfg = info["config"]["params"]["vectors"]
    dim = vec_cfg["size"] if isinstance(vec_cfg, dict) and "size" in vec_cfg else None
    distance = vec_cfg.get("distance") if isinstance(vec_cfg, dict) else None
    print(f"source: {args.collection} points={src_count} dim={dim} distance={distance}")
    if dim is not None and dim != args.dim:
        raise SystemExit(f"source dim {dim} != expected {args.dim}")

    ckpt_path, manifest_path = args.out + ".ckpt", args.out + ".manifest.json"
    offset, done = None, 0
    mode = "w"
    if os.path.exists(ckpt_path):
        if not args.resume:
            raise SystemExit(f"{ckpt_path} exists; pass --resume or remove it")
        with open(ckpt_path, "r", encoding="utf-8") as fh:
            ck = json.load(fh)
        offset, done, mode = ck["offset"], ck["count"], "a"
        print(f"resuming from offset={offset} count={done}")
    elif os.path.exists(args.out) and not args.fresh:
        raise SystemExit(f"{args.out} exists; pass --fresh to overwrite")

    t0, sha = time.time(), hashlib.sha256()
    with open(args.out, mode, encoding="utf-8") as fh:
        while True:
            body = {"limit": args.limit, "with_vector": True, "with_payload": True}
            if offset is not None:
                body["offset"] = offset
            res = http_json(f"{args.qdrant}/collections/{args.collection}/points/scroll",
                            body)["result"]
            pts = res.get("points", [])
            for p in pts:
                rec = {"id": p["id"], "vector": dense_vector(p["vector"]),
                       "payload": p.get("payload") or {}}
                line = json.dumps(rec, ensure_ascii=False)
                fh.write(line + "\n")
                sha.update(line.encode("utf-8"))
            done += len(pts)
            offset = res.get("next_page_offset")
            print(f"  scrolled total={done} next={offset}", flush=True)
            if offset is None:
                break
            with open(ckpt_path, "w", encoding="utf-8") as cf:
                json.dump({"offset": offset, "count": done}, cf)
    dt = time.time() - t0
    manifest = {"collection": args.collection, "exported": done,
                "source_points_count": src_count, "dim": dim, "distance": distance,
                "sha256": sha.hexdigest(), "seconds": round(dt, 2)}
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    if os.path.exists(ckpt_path):
        os.remove(ckpt_path)
    print(f"exported {done} points in {dt:.1f}s -> {args.out}")
    if src_count is not None and done != src_count:
        print(f"WARNING: exported {done} != source points_count {src_count} "
              f"(concurrent writes?)")
    return 0 if src_count is None or done == src_count else 1


# ----------------------------------------------------------------- load ----

def _import_provider(site_packages):
    for path in (site_packages,
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server")):
        if path not in sys.path:
            sys.path.insert(0, path)
    import sqlite_vec  # noqa: F401
    from mem0.vector_stores.base import VectorStoreBase  # noqa: F401
    import sqlite_vec_store
    return sqlite_vec_store


def cmd_load(args):
    svs = _import_provider(args.site_packages)
    if os.path.exists(args.db):
        if args.fresh:
            os.remove(args.db)
        elif not args.resume:
            raise SystemExit(f"{args.db} exists; pass --fresh or --resume")
    store = svs.SQLiteVecStore(db_path=args.db, collection_name=args.collection,
                               embedding_model_dims=args.dim, distance="cosine")
    already = store.col_info()["count"]
    if args.resume and already:
        print(f"resuming: {already} rows already in db, skipping that many lines")

    t0, n, batch_ids, batch_vecs, batch_pays = time.time(), 0, [], [], []
    with open_jsonl(args.jsonl) as fh:
        for i, line in enumerate(fh):
            if args.resume and i < already:
                continue
            rec = json.loads(line)
            batch_ids.append(rec["id"])
            batch_vecs.append(rec["vector"])
            batch_pays.append(rec["payload"])
            if len(batch_ids) >= args.batch:
                store.insert(vectors=batch_vecs, payloads=batch_pays, ids=batch_ids)
                n += len(batch_ids)
                batch_ids, batch_vecs, batch_pays = [], [], []
                if n % (args.batch * 10) == 0:
                    print(f"  loaded {n} rows ({time.time() - t0:.0f}s)", flush=True)
    if batch_ids:
        store.insert(vectors=batch_vecs, payloads=batch_pays, ids=batch_ids)
        n += len(batch_ids)
    count = store.col_info()["count"]
    dt = time.time() - t0
    size_mb = os.path.getsize(args.db) / 1e6
    print(f"loaded {n} new rows in {dt:.1f}s; db count={count} size={size_mb:.1f}MB")
    store.close()
    return 0


# --------------------------------------------------------------- verify ----

def cmd_verify(args):
    svs = _import_provider(args.site_packages)
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

    sample = set(random.Random(args.seed).sample(
        sorted(db_ids), min(args.sample, len(db_ids))))
    jsonl_ids, sample_vecs, sample_pays = set(), {}, {}
    with open_jsonl(args.jsonl) as fh:
        for line in fh:
            rec = json.loads(line)
            jsonl_ids.add(rec["id"])
            if rec["id"] in sample:
                sample_vecs[rec["id"]] = rec["vector"]
                sample_pays[rec["id"]] = rec["payload"]
    print(f"jsonl: {len(jsonl_ids)} unique ids; sampled {len(sample_vecs)}/{len(sample)}")

    fails = []
    if len(db_ids) != len(jsonl_ids):
        fails.append(f"count: db {len(db_ids)} != jsonl {len(jsonl_ids)}")
    only_db, only_jsonl = db_ids - jsonl_ids, jsonl_ids - db_ids
    if only_db:
        fails.append(f"ids only in db: {sorted(only_db)[:5]} ...({len(only_db)})")
    if only_jsonl:
        fails.append(f"ids only in jsonl: {sorted(only_jsonl)[:5]} ...({len(only_jsonl)})")
    if vec_n != len(db_ids) or fts_n != len(db_ids):
        fails.append(f"structural: vec={vec_n} meta={len(db_ids)} fts={fts_n} unequal")

    sample = random.Random(args.seed).sample(sorted(db_ids), min(args.sample, len(db_ids)))
    l2_max, worst = 0.0, None
    pay_bad = 0
    for pid in sample:
        row = conn.execute(
            f"SELECT rowid FROM {meta_t} WHERE id = ?", (pid,)).fetchone()
        (blob,) = conn.execute(
            f"SELECT embedding FROM {vec_t} WHERE rowid = ?", (row[0],)).fetchone()
        db_vec = struct.unpack(f"{args.dim}f", blob)
        jsonl_vec = sample_vecs[pid]
        l2 = sum((a - b) ** 2 for a, b in zip(db_vec, jsonl_vec)) ** 0.5
        if l2 > l2_max:
            l2_max, worst = l2, pid
        if l2 > args.eps:
            fails.append(f"vector L2 {l2:.2e} > eps {args.eps:g} for {pid}")
        if sample_pays.get(pid) != json.loads(conn.execute(
                f"SELECT payload FROM {meta_t} WHERE id = ?", (pid,)).fetchone()[0]):
            pay_bad += 1
    if pay_bad:
        fails.append(f"payload mismatch on {pay_bad}/{len(sample)} sampled ids")
    print(f"sampled {len(sample)} vectors: max L2 = {l2_max:.3e} (eps {args.eps:g})"
          f"{f' worst={worst}' if worst else ''}; payload mismatches={pay_bad}")

    if fails:
        print("\nVERIFY FAILED:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("\nVERIFY PASSED: count/id-set/structural/sample-L2/payload all ok")
    return 0


# ------------------------------------------------------------------ main ----

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="stage", required=True)

    e = sub.add_parser("export")
    e.add_argument("--qdrant", default="http://127.0.0.1:6333")
    e.add_argument("--collection", default="mem0")
    e.add_argument("--out", required=True)
    e.add_argument("--limit", type=int, default=256)
    e.add_argument("--dim", type=int, default=1024)
    e.add_argument("--resume", action="store_true")
    e.add_argument("--fresh", action="store_true")
    e.set_defaults(fn=cmd_export)

    l = sub.add_parser("load")
    l.add_argument("--jsonl", required=True)
    l.add_argument("--db", required=True)
    l.add_argument("--collection", default="mem0")
    l.add_argument("--dim", type=int, default=1024)
    l.add_argument("--batch", type=int, default=256)
    l.add_argument("--resume", action="store_true")
    l.add_argument("--fresh", action="store_true")
    l.add_argument("--site-packages", default=DEFAULT_SITE_PACKAGES)
    l.set_defaults(fn=cmd_load)

    v = sub.add_parser("verify")
    v.add_argument("--jsonl", required=True)
    v.add_argument("--db", required=True)
    v.add_argument("--collection", default="mem0")
    v.add_argument("--dim", type=int, default=1024)
    v.add_argument("--sample", type=int, default=100)
    v.add_argument("--eps", type=float, default=1e-3)
    v.add_argument("--seed", type=int, default=42)
    v.add_argument("--site-packages", default=DEFAULT_SITE_PACKAGES)
    v.set_defaults(fn=cmd_verify)

    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
