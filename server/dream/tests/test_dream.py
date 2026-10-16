#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for the dream job (stage 4c-1, stdlib unittest - runs on PC and on 102).

Run:  python -m unittest discover -s Hippocampus/server/dream/tests -v
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dream  # noqa: E402


def make_repo(root: Path) -> Path:
    (root / "projects").mkdir(parents=True, exist_ok=True)
    (root / "archive").mkdir(parents=True, exist_ok=True)
    (root / "dreams").mkdir(parents=True, exist_ok=True)
    (root / "MEMORY.md").write_text(
        "# MEMORY\n\n- alpha entry [source: t; added: 2026-01-01]\n- see [[projects/alpha]]\n",
        encoding="utf-8")
    (root / "projects" / "alpha.md").write_text(
        "# alpha\n\n- fact one [source: t; added: 2026-01-01]\n- fact two no source\n", encoding="utf-8")
    (root / "projects" / "orphan.md").write_text("# orphan\n\n- alone\n", encoding="utf-8")
    (root / "CONVENTIONS.md").write_text("# conv\n", encoding="utf-8")
    (root / "dream-prompt.md").write_text("# prompt\n", encoding="utf-8")
    (root / "archive" / "old.md").write_text("# old\n- [[does/not/exist]]\n", encoding="utf-8")
    (root / "dreams" / "2026-01-01.md").write_text("# report\n", encoding="utf-8")
    return root


class TestDeterministicLayer(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_scan_skips_archive_and_dreams(self):
        res = dream.scan(self.repo)
        self.assertIn("MEMORY.md", res["files"])
        self.assertIn("projects/alpha.md", res["files"])
        self.assertNotIn("archive/old.md", res["files"])
        self.assertNotIn("dreams/2026-01-01.md", res["files"])

    def test_links_resolution(self):
        res = dream.scan(self.repo)
        self.assertEqual(dream.check_links(self.repo, res["links"]), [])  # [[projects/alpha]] resolves
        broken = dream.check_links(self.repo, [("x.md", "does/not/exist")])
        self.assertEqual(len(broken), 1)

    def test_memory_size_check(self):
        self.assertTrue(dream.check_memory(self.repo)["ok"])
        big = self.repo / "MEMORY.md"
        big.write_text("\n".join(f"- line {i}" for i in range(100)), encoding="utf-8")
        self.assertFalse(dream.check_memory(self.repo)["ok"])

    def test_no_source_detection(self):
        res = dream.scan(self.repo)
        self.assertTrue(any("alpha.md" in s for s in res["no_source"]))

    def test_orphans(self):
        res = dream.scan(self.repo)
        orphans = dream.find_orphans(self.repo, res["files"], res["links"])
        self.assertIn("projects/orphan.md", orphans)
        self.assertNotIn("projects/alpha.md", orphans)
        self.assertNotIn("CONVENTIONS.md", orphans)

    def test_report_renders_and_says_dry_run(self):
        res = dream.scan(self.repo)
        rep = dream.render_report(
            "dry-run", "2026-10-16", self.repo, res, [], dream.check_memory(self.repo),
            dream.find_orphans(self.repo, res["files"], res["links"]),
            {"ok": True, "url": "u", "models": ["m"], "detail": "http 200"},
            {"implemented": False, "note": "stub", "items": []},
            {"is_git": False, "head": "", "status": ""},
            {"is_git": False, "head": "", "status": ""})
        self.assertIn("Dream 报告（dry-run）", rep)
        self.assertIn("未做任何仓库改动", rep)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = json.dumps({"object": "list", "data": [{"id": "GLM-5.3-Flash-EXL3"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence
        pass


class TestEngineProbe(unittest.TestCase):
    def test_probe_ok(self):
        srv = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            res = dream.engine_probe(f"http://127.0.0.1:{srv.server_port}/v1")
            self.assertTrue(res["ok"])
            self.assertEqual(res["models"], ["GLM-5.3-Flash-EXL3"])
        finally:
            srv.shutdown()

    def test_probe_unreachable_is_fail_soft(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        res = dream.engine_probe(f"http://127.0.0.1:{port}/v1", timeout=1)
        self.assertFalse(res["ok"])
        self.assertIn("detail", res)


class TestFalsePositives(unittest.TestCase):
    """Regressions found on the first real run (2026-10-16): placeholders quoted in docs are not links."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "MEMORY.md").write_text(
            "# MEMORY\n\n- entry with source [source: t; added: 2026-01-01]\n"
            "- 用 `[[链接]]` 跳转，格式见 [[CONVENTIONS]]\n", encoding="utf-8")
        (self.repo / "CONVENTIONS.md").write_text(
            "# conv\n\n- 同一事实只在一处陈述，别处用 `[[链接]]` 引用\n"
            "- 顺着 `[[双链]]` 跳：`[[projects/alpha]]`\n\n```\n[[链接]]\n```\n", encoding="utf-8")
        (self.repo / "projects").mkdir(exist_ok=True)
        (self.repo / "projects" / "alpha.md").write_text("# a\n- fact [source: t; added: 2026-01-01]\n",
                                                         encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_placeholder_links_ignored(self):
        res = dream.scan(self.repo)
        targets = [t for _, t in res["links"]]
        self.assertNotIn("链接", targets)
        self.assertNotIn("双链", targets)
        self.assertIn("CONVENTIONS", targets)

    def test_spec_doc_bullets_are_not_entries(self):
        res = dream.scan(self.repo)
        self.assertFalse(any(s.startswith("CONVENTIONS.md") for s in res["no_source"]))
        self.assertTrue(all(s.startswith(("MEMORY.md", "projects/")) for s in res["no_source"]) or not res["no_source"])


class TestGitSelfCheck(unittest.TestCase):
    def test_git_status_on_real_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            env_repo = make_repo(repo)
            subprocess.run(["git", "init", "-q", str(env_repo)], check=True)
            for k, v in (("user.email", "t@t"), ("user.name", "t")):
                subprocess.run(["git", "-C", str(env_repo), "config", k, v], check=True)
            subprocess.run(["git", "-C", str(env_repo), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(env_repo), "commit", "-q", "-m", "x"], check=True)
            st = dream.git_status(env_repo)
            self.assertTrue(st["is_git"])
            self.assertEqual(len(st["head"]), 40)
            self.assertEqual(st["status"], "")


class TestEndToEndDryRun(unittest.TestCase):
    def test_dry_run_writes_report_and_no_repo_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = make_repo(root / "repo")
            out, logd = root / "out", root / "logs"
            rc = dream.main(["--mode", "dry-run", "--repo", str(repo), "--out", str(out),
                             "--log", str(logd), "--engine", "http://127.0.0.1:1/v1",
                             "--date", "2026-10-16"])
            self.assertEqual(rc, 0)  # engine unreachable must NOT fail the job
            self.assertTrue((out / "2026-10-16.md").exists())
            self.assertTrue((logd / "dream-2026-10-16.log").exists())
            # repo untouched: no new/removed files, MEMORY.md identical
            self.assertEqual(sorted(p.name for p in repo.glob("*.md")),
                             ["CONVENTIONS.md", "MEMORY.md", "dream-prompt.md"])
            self.assertIn("alpha entry", (repo / "MEMORY.md").read_text(encoding="utf-8"))
            self.assertIn("不可达", (out / "2026-10-16.md").read_text(encoding="utf-8"))

    def test_apply_mode_on_non_git_repo_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = make_repo(root / "repo")  # deliberately NOT a git repo
            rc = dream.main(["--mode", "apply", "--repo", str(repo), "--out", str(root / "o"),
                             "--log", str(root / "l"), "--date", "2026-10-16"])
            self.assertEqual(rc, 1)  # apply needs git: rollback relies on it
            self.assertFalse((repo / "dreams" / "2026-10-16.md").exists())


class TestAdjudicationLayer(unittest.TestCase):
    def test_candidate_pairs_finds_near_duplicates(self):
        entries = [
            {"file": "MEMORY.md", "line": 5, "text": "alpha 项目阶段 1 完成，验收全绿 12/12"},
            {"file": "projects/alpha.md", "line": 3, "text": "alpha 项目阶段 1 完成，验收全绿 12/12（清单见 refs）"},
            {"file": "projects/beta.md", "line": 9, "text": "完全无关的另一件事，讲天气和茶叶"},
        ]
        pairs = dream.candidate_pairs(entries, threshold=0.45, top=5)
        self.assertEqual(len(pairs), 1)
        self.assertIn("alpha", pairs[0]["a"]["text"])

    def test_parse_action_tolerates_fences(self):
        for raw in ('{"action":"flag","rationale":"x"}',
                    '```json\n{"action":"flag","rationale":"x"}\n```',
                    'here you go: {"action":"merge","keep":"A","merged":"m"} ok'):
            self.assertTrue(dream.parse_action(raw)["ok"])
        self.assertFalse(dream.parse_action("no json here")["ok"])
        self.assertFalse(dream.parse_action('{"action":"delete"}')["ok"])

    def test_merge_guard_blocks_new_facts(self):
        src = ["alpha 完成于 2026-10-13，验收 12/12", "alpha 清单在 refs/ 下"]
        clean = "alpha 完成于 2026-10-13，验收 12/12；清单在 refs/ 下"
        self.assertEqual(dream.merge_guard(clean, src), [])
        invented = "alpha 完成于 2026-10-13，验收 15/15；由助手于 2026-11-01 复核"
        self.assertTrue(dream.merge_guard(invented, src))


class _JudgeHandler(BaseHTTPRequestHandler):
    """Serves /v1/models and /v1/chat/completions (canned merge action)."""

    last_payload = None

    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        _JudgeHandler.last_payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self._json({"choices": [{"message": {"content":
            '{"action":"merge","keep":"A","merged":"alpha entry [source: t; added: 2026-01-01]","archive":"none",'
            '"rationale":"同一事实"}'}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    def do_GET(self):  # noqa: N802
        self._json({"object": "list", "data": [{"id": "GLM-5.3-Flash-EXL3"}]})

    def log_message(self, *a):
        pass


class _ThinkingOnlyHandler(_JudgeHandler):
    """Mimics the 2026-10-16 failure: everything burned in reasoning, content=null."""

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self._json({"choices": [{"message": {"content": None, "reasoning_content": "let me think " * 20},
                                 "finish_reason": "length"}],
                    "usage": {"completion_tokens": 900,
                              "completion_tokens_details": {"reasoning_tokens": 900}}})


class TestAdjudicationEndToEnd(unittest.TestCase):
    def test_thinking_is_disabled_in_request(self):
        srv = HTTPServer(("127.0.0.1", 0), _JudgeHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            dream.llm_chat(f"http://127.0.0.1:{srv.server_port}/v1", "PROMPT", model="m")
            payload = _JudgeHandler.last_payload or {}
            self.assertEqual(payload.get("chat_template_kwargs"), {"enable_thinking": False})
        finally:
            srv.shutdown()

    def test_thinking_only_reply_is_reported_as_error(self):
        """Regression for the 2026-10-16 live run: content=null (all tokens in reasoning)."""
        srv = HTTPServer(("127.0.0.1", 0), _ThinkingOnlyHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                repo = make_repo(Path(tmp) / "repo")
                (repo / "projects" / "alpha.md").write_text(
                    "# alpha\n\n- alpha entry [source: t; added: 2026-01-01]\n", encoding="utf-8")
                out = Path(tmp) / "out"
                rc = dream.main(["--mode", "dry-run", "--repo", str(repo), "--out", str(out),
                                 "--log", str(Path(tmp) / "logs"),
                                 "--engine", f"http://127.0.0.1:{srv.server_port}/v1",
                                 "--date", "2026-10-16"])
                self.assertEqual(rc, 0)
                rep = (out / "2026-10-16.md").read_text(encoding="utf-8")
                self.assertIn("errors=1", rep.replace("错误=", "errors="))
                self.assertIn("empty content", rep)
        finally:
            srv.shutdown()

    def test_dry_run_with_cortex_still_touches_nothing(self):
        srv = HTTPServer(("127.0.0.1", 0), _JudgeHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repo = make_repo(root / "repo")
                # plant a near-duplicate pair across files
                (repo / "projects" / "alpha.md").write_text(
                    "# alpha\n\n- alpha entry [source: t; added: 2026-01-01]\n", encoding="utf-8")
                out = root / "out"
                rc = dream.main(["--mode", "dry-run", "--repo", str(repo), "--out", str(out),
                                 "--log", str(root / "logs"),
                                 "--engine", f"http://127.0.0.1:{srv.server_port}/v1",
                                 "--date", "2026-10-16"])
                self.assertEqual(rc, 0)
                rep = (out / "2026-10-16.md").read_text(encoding="utf-8")
                self.assertIn("裁决层", rep)
                self.assertIn("merge=1", rep)
                self.assertIn("未落库", rep)
                # repo untouched
                self.assertEqual(sorted(p.name for p in (repo / "projects").iterdir()),
                                 ["alpha.md", "orphan.md"])
        finally:
            srv.shutdown()


def make_git_repo(root: Path) -> Path:
    """make_repo + git init/commit, so the apply layer has a rollback-able repo."""
    repo = make_repo(root)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        subprocess.run(["git", "-C", str(repo), "config", k, v], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True)
    return repo


def commit_count(repo: Path) -> int:
    out = subprocess.run(["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    return int(out.stdout.strip())


class TestApplyLayer(unittest.TestCase):
    """4c-3: plan_ops / execute_ops / build_subject (deterministic, no LLM)."""

    def _items(self, action, keep="A", merged="", archive="none"):
        a = {"file": "MEMORY.md", "line": 5, "raw": "- A [source: t; added: 2026-01-01]",
             "text": "A [source: t; added: 2026-01-01]"}
        b = {"file": "projects/x.md", "line": 3, "raw": "- B [source: t; added: 2026-01-01]",
             "text": "B [source: t; added: 2026-01-01]"}
        return {"ran": True, "items": [{"pair": "MEMORY.md:5 <-> projects/x.md:3", "sim": 0.9,
                                        "action": action, "keep": keep, "merged": merged,
                                        "archive": archive, "a": a, "b": b}]}

    def test_plan_ops_merge(self):
        ops = dream.plan_ops(self._items("merge", keep="A",
                                         merged="A [source: t; added: 2026-01-01]"))
        self.assertEqual(len(ops), 1)
        o = ops[0]
        self.assertEqual(o["type"], "merge")
        self.assertEqual((o["file"], o["line"]), ("MEMORY.md", 5))
        self.assertEqual((o["drop_file"], o["drop_line"]), ("projects/x.md", 3))
        self.assertTrue(o["new"].startswith("- "))

    def test_plan_ops_merge_archive_dropped(self):
        ops = dream.plan_ops(self._items("merge", keep="A", merged="A", archive="B"))
        self.assertTrue(ops[0]["archive_dropped"])

    def test_plan_ops_skips_flag_and_bad_keep(self):
        self.assertEqual(dream.plan_ops(self._items("flag")), [])
        self.assertEqual(dream.plan_ops(self._items("merge", keep="none", merged="x")), [])

    def test_plan_ops_archive(self):
        ops = dream.plan_ops(self._items("archive", archive="B"))
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["type"], "archive")
        self.assertEqual((ops[0]["file"], ops[0]["line"]), ("projects/x.md", 3))

    def test_execute_ops_merge_replaces_and_removes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            (repo / "projects" / "x.md").write_text(
                "# x\n\n- B old [source: t; added: 2026-01-01]\n- keep me\n", encoding="utf-8")
            ops = [{"type": "merge", "pair": "p", "file": "MEMORY.md", "line": 3,
                    "new": "- merged alpha [source: t; added: 2026-01-01]",
                    "drop_file": "projects/x.md", "drop_line": 3, "archive_dropped": False,
                    "merged": "merged alpha [source: t; added: 2026-01-01]"}]
            res = dream.execute_ops(repo, ops, lambda m: None)
            self.assertEqual(res["applied"][0]["dropped_disposition"], "removed")
            mem = (repo / "MEMORY.md").read_text(encoding="utf-8")
            self.assertIn("merged alpha", mem)
            x = (repo / "projects" / "x.md").read_text(encoding="utf-8")
            self.assertNotIn("B old", x)
            self.assertIn("keep me", x)  # untouched line survives (bottom-up edits)

    def test_execute_ops_archive_moves_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            (repo / "projects" / "x.md").write_text("# x\n\n- retire me [source: t; added: 2026-01-01]\n",
                                                    encoding="utf-8")
            ops = [{"type": "archive", "pair": "p", "file": "projects/x.md", "line": 3}]
            res = dream.execute_ops(repo, ops, lambda m: None)
            self.assertIn("archive/projects/x.md", res["archive_files"])
            self.assertNotIn("retire me", (repo / "projects" / "x.md").read_text(encoding="utf-8"))
            self.assertIn("retire me", (repo / "archive" / "projects" / "x.md").read_text(encoding="utf-8"))

    def test_build_subject(self):
        self.assertIn("仓库健康", dream.build_subject("2026-10-16", []))
        self.assertIn("合并 1 条", dream.build_subject("2026-10-16", [{"type": "merge"}]))
        self.assertIn("dreams/2026-10-16.md", dream.build_subject("2026-10-16", [{"type": "archive"}]))


class TestApplyEndToEnd(unittest.TestCase):
    """4c-3: full apply on a real git repo -> whitelisted ops + exactly one dream commit."""

    def test_apply_merge_commits_exactly_once(self):
        srv = HTTPServer(("127.0.0.1", 0), _JudgeHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repo = make_git_repo(root / "repo")
                # plant a near-duplicate pair (MEMORY.md:3 <-> projects/alpha.md) for the judge
                (repo / "projects" / "alpha.md").write_text(
                    "# alpha\n\n- alpha entry [source: t; added: 2026-01-01]\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
                subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "plant"], check=True)
                n0 = commit_count(repo)
                out = root / "out"
                rc = dream.main(["--mode", "apply", "--repo", str(repo), "--out", str(out),
                                 "--log", str(root / "logs"),
                                 "--engine", f"http://127.0.0.1:{srv.server_port}/v1",
                                 "--date", "2026-10-16"])
                self.assertEqual(rc, 0)
                self.assertEqual(commit_count(repo), n0 + 1)  # exactly one new commit
                self.assertTrue((repo / "dreams" / "2026-10-16.md").exists())
                st = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace")
                self.assertEqual(st.stdout.strip(), "")  # clean worktree
                self.assertIn("dream:", subprocess.run(
                    ["git", "-C", str(repo), "log", "-1", "--format=%s"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace").stdout)
        finally:
            srv.shutdown()

    def test_apply_no_actions_commits_report_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = make_git_repo(root / "repo")
            n0 = commit_count(repo)
            rc = dream.main(["--mode", "apply", "--repo", str(repo), "--out", str(root / "out"),
                             "--log", str(root / "logs"), "--engine", "http://127.0.0.1:1/v1",
                             "--date", "2026-10-16"])
            self.assertEqual(rc, 0)
            self.assertEqual(commit_count(repo), n0 + 1)  # healthy repo -> report-only commit
            self.assertTrue((repo / "dreams" / "2026-10-16.md").exists())

    def test_apply_second_run_same_day_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = make_git_repo(root / "repo")
            args = ["--mode", "apply", "--repo", str(repo), "--out", str(root / "out"),
                    "--log", str(root / "logs"), "--engine", "http://127.0.0.1:1/v1",
                    "--date", "2026-10-16"]
            dream.main(list(args))
            n1 = commit_count(repo)
            rc = dream.main(list(args))  # identical report -> nothing to commit
            self.assertEqual(rc, 0)
            self.assertEqual(commit_count(repo), n1)  # no duplicate commit

    def test_apply_dirty_repo_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = make_git_repo(root / "repo")
            (repo / "MEMORY.md").write_text("# MEMORY\n\n- dirty uncommitted [source: t; added: 2026-01-01]\n",
                                            encoding="utf-8")
            n0 = commit_count(repo)
            rc = dream.main(["--mode", "apply", "--repo", str(repo), "--out", str(root / "out"),
                             "--log", str(root / "logs"), "--engine", "http://127.0.0.1:1/v1",
                             "--date", "2026-10-16"])
            self.assertEqual(rc, 0)  # fail-soft refusal
            self.assertEqual(commit_count(repo), n0)
            self.assertFalse((repo / "dreams" / "2026-10-16.md").exists())


class TestDenoiseAndInline(unittest.TestCase):
    """4c-3 follow-up (A): citation trailers / pointer-detail pairs were 100% of the false
    positives measured on the live repo 2026-10-16; inline duplicates were undetectable."""

    def test_strip_meta_removes_trailers(self):
        s = dream.strip_meta("条目正文 [source: 记忆系统/docs/整体计划.md §7; added: 2026-10-13] [手动] （相关度 0.9）")
        self.assertIn("条目正文", s)
        for junk in ("source", "added", "相关度", "手动"):
            self.assertNotIn(junk, s)

    def test_trailer_only_similarity_is_not_a_candidate(self):
        # legacy behaviour: these two share nothing but the [source: …; added: …] tail
        a = {"file": "MEMORY.md", "line": 13, "raw": "- opencode 手动选中的模型优先 [source: ses_f933ca5ddffe; added: 2026-10-13]",
             "text": "opencode 手动选中的模型优先 [source: ses_f933ca5ddffe; added: 2026-10-13]"}
        b = {"file": "MEMORY.md", "line": 16, "raw": "- GB10 天生不适合稠密模型、应跑 MoE [source: ses_f2d0350e1ffe; added: 2026-10-13]",
             "text": "GB10 天生不适合稠密模型、应跑 MoE [source: ses_f2d0350e1ffe; added: 2026-10-13]"}
        import difflib
        raw = difflib.SequenceMatcher(None, dream._norm(a["text"]), dream._norm(b["text"])).ratio()
        self.assertGreaterEqual(raw, 0.45)                      # the old, noisy comparison
        self.assertEqual(dream.candidate_pairs([a, b], threshold=0.45, top=5), [])

    def test_pointer_detail_pair_is_skipped(self):
        short = {"file": "MEMORY.md", "line": 19,
                 "raw": "- 三机 SSH（`worker-node`=102 / `head-node`=101）运维细节 [[environment/machines]]",
                 "text": "三机 SSH（`worker-node`=102 / `head-node`=101）运维细节 [[environment/machines]]"}
        detail = {"file": "environment/machines.md", "line": 3,
                  "raw": "- 三机 SSH（`worker-node`=102 / `head-node`=101，均 BatchMode 免密）",
                  "text": "三机 SSH（`worker-node`=102 / `head-node`=101，均 BatchMode 免密）"}
        import difflib
        raw = difflib.SequenceMatcher(None, dream._norm(short["text"]), dream._norm(detail["text"])).ratio()
        self.assertGreaterEqual(raw, 0.45)
        self.assertEqual(dream.candidate_pairs([short, detail], threshold=0.45, top=5), [])

    def test_true_duplicate_still_found(self):
        entries = [
            {"file": "MEMORY.md", "line": 5, "raw": "- alpha 项目阶段 1 完成，验收全绿 12/12",
             "text": "alpha 项目阶段 1 完成，验收全绿 12/12"},
            {"file": "projects/alpha.md", "line": 3,
             "raw": "- alpha 项目阶段 1 完成，验收全绿 12/12（清单见 refs）",
             "text": "alpha 项目阶段 1 完成，验收全绿 12/12（清单见 refs）"},
        ]
        pairs = dream.candidate_pairs(entries, threshold=0.45, top=5)
        self.assertEqual(len(pairs), 1)
        self.assertGreaterEqual(pairs[0]["shared"], dream.MIN_SHARED_TOKENS)

    def test_min_shared_filters_shape_only_similarity(self):
        a = {"file": "MEMORY.md", "line": 5, "raw": "- Hippocampus 阶段 B 完成，验收全绿 12/12",
             "text": "Hippocampus 阶段 B 完成，验收全绿 12/12"}
        b = {"file": "MEMORY.md", "line": 30, "raw": "- Hippocampus 收束，核查全通 13/13",
             "text": "Hippocampus 收束，核查全通 13/13"}
        import difflib
        shared = dream.sig_tokens(dream.strip_meta(a["text"])) & dream.sig_tokens(dream.strip_meta(b["text"]))
        self.assertEqual(len(shared), 1)                      # similar shape, ONE real shared token
        r = difflib.SequenceMatcher(None, dream._norm(dream.strip_meta(a["text"])),
                                    dream._norm(dream.strip_meta(b["text"]))).ratio()
        self.assertEqual(dream.candidate_pairs([a, b], threshold=0.30, top=5, min_shared=2), [])
        found = dream.candidate_pairs([a, b], threshold=0.30, top=5, min_shared=1)
        self.assertEqual(len(found), 1 if r >= 0.30 else 0)   # proves the token floor did the filtering

    def test_inline_duplicate_clause_detected(self):
        e = {"file": "MEMORY.md", "line": 7,
             "raw": "- 阶段 D 完成，验收全绿 12/12。交接在 refs 下。阶段 D 完成，验收全绿 12/12。",
             "text": "阶段 D 完成，验收全绿 12/12。交接在 refs 下。阶段 D 完成，验收全绿 12/12。"}
        findings = dream.inline_duplicates([e])
        self.assertEqual(len(findings), 1)
        self.assertEqual(len(findings[0]["hits"]), 1)
        self.assertGreaterEqual(findings[0]["hits"][0]["sim"], dream.INLINE_SIM)

    def test_inline_path_echo_not_reported(self):
        e = {"file": "projects/x.md", "line": 10,
             "raw": "- 交接 `交接\\阶段X-示例-20261014.md`；另见 `交接\\阶段X-示例-20261014.md`。",
             "text": "交接 `交接\\阶段X-示例-20261014.md`；另见 `交接\\阶段X-示例-20261014.md`。"}
        self.assertEqual(dream.inline_duplicates([e]), [])

    def test_report_has_inline_section_and_zero_candidates_on_tidy_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "MEMORY.md").write_text(
                "# MEMORY\n\n- alpha 事实一 [source: a; added: 2026-10-13]\n"
                "- beta 事实二 [source: b; added: 2026-10-13]\n", encoding="utf-8")
            rc = dream.main(["--mode", "dry-run", "--repo", str(repo), "--out", str(root / "out"),
                             "--log", str(root / "logs"), "--engine", "http://127.0.0.1:1/v1",
                             "--date", "2026-10-16"])
            self.assertEqual(rc, 0)
            rep = (root / "out" / "2026-10-16.md").read_text(encoding="utf-8")
            self.assertIn("2.1 行内近重复子句", rep)
            self.assertIn("未发现", rep)


if __name__ == "__main__":
    unittest.main(verbosity=2)
