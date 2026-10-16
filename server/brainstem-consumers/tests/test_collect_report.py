#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for the brainstem consumer C-S2 (idle info collection; stdlib unittest, PC + 102).

Run:  python -m unittest discover -s Hippocampus/server/brainstem-consumers/tests -v
"""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collect_report as cr  # noqa: E402

FIXED_NOW = dt.datetime(2026, 10, 16, 21, 3, 12,
                        tzinfo=dt.timezone(dt.timedelta(hours=8)))
S2 = {"type": "s2", "ts": "2026-10-16T20:12:38.359891+08:00",
      "idle_seconds": 600.0, "loadavg": 0.18}


def opts(**over):
    o = dict(cr.DEFAULTS)
    o.update(over)
    return o


OK_REPLY = {"ok": True, "content": "摘要正文：一切正常。", "finish_reason": "stop",
            "usage": {"completion_tokens": 42}}
FAIL_REPLY = {"ok": False, "content": "", "finish_reason": "",
              "detail": "URLError: [Errno 111] refused"}
EMPTY_REPLY = {"ok": True, "content": "", "finish_reason": "length", "usage": {}}


class FakeChat:
    """Records every call so tests can assert count + budget caps."""

    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply if reply is not None else OK_REPLY

    def __call__(self, base_url, prompt, model="", timeout=0.0, max_tokens=0):
        self.calls.append({"base_url": base_url, "prompt": prompt, "model": model,
                           "timeout": timeout, "max_tokens": max_tokens})
        return self.reply


class TestTextUtil(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_tail_truncates_and_marks(self):
        p = self.base / "big.txt"
        p.write_text("A" * 500, encoding="utf-8")
        t = cr.tail_text(p, 50)
        self.assertTrue(t.startswith("...(截断)"))
        self.assertLessEqual(len(t), 50 + 10)

    def test_tail_short_file_verbatim(self):
        p = self.base / "s.txt"
        p.write_text("hello", encoding="utf-8")
        self.assertEqual(cr.tail_text(p, 50), "hello")

    def test_tail_missing_returns_empty(self):
        self.assertEqual(cr.tail_text(self.base / "nope", 50), "")

    def test_head_missing_returns_empty(self):
        self.assertEqual(cr.head_text(self.base / "nope", 50), "")

    def test_sys_brief_is_string(self):
        self.assertIsInstance(cr.sys_brief(), str)


class TestMaterial(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "brainstem"
        self.gm = self.base / "git-memory"

    def tearDown(self):
        self.tmp.cleanup()

    def test_gather_includes_brainstem_state(self):
        (self.root / "state").mkdir(parents=True)
        (self.root / "state" / "brainstem.json").write_text(
            json.dumps({"ticks": 90, "last_s1_date": "2026-10-16"}), encoding="utf-8")
        items = cr.gather_material(self.root, self.gm, 1200)
        blob = "\n".join(i["body"] for i in items)
        self.assertIn("ticks", blob)
        self.assertEqual(items[0]["title"], "脑干 state")

    def test_gather_tolerates_missing_everything(self):
        items = cr.gather_material(self.base / "void", self.base / "void2", 1200)
        self.assertGreaterEqual(len(items), 6)
        for it in items:
            self.assertIsInstance(it["title"], str)
            self.assertIsInstance(it["body"], str)

    def test_git_recent_reads_repo(self):
        if not shutil.which("git"):
            self.skipTest("git not available")
        repo = self.base / "r"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "memory: sample entry"],
                       cwd=repo, check=True)
        self.assertIn("memory: sample entry", cr.git_recent(repo, 5))

    def test_git_recent_unreadable_is_failsafe(self):
        self.assertIsInstance(cr.git_recent(self.base / "void", 5), str)

    def test_assemble_respects_cap(self):
        items = [{"title": "t", "body": "B" * 400} for _ in range(5)]
        text = cr.assemble_material(items, 100)
        self.assertLessEqual(len(text), 100 + len("\n...(材料已按上限截断)"))
        self.assertTrue(text.endswith("...(材料已按上限截断)"))

    def test_assemble_no_truncation_when_small(self):
        items = [{"title": "t", "body": "hello"}]
        self.assertNotIn("截断", cr.assemble_material(items, 5000))

    def test_latest_file_missing_dir(self):
        self.assertIsNone(cr.latest_file(self.base / "void", "*.md"))


class TestPrompt(unittest.TestCase):
    def test_prompt_carries_material_and_constraints(self):
        p = cr.build_prompt("MATERIAL-XYZ")
        self.assertIn("MATERIAL-XYZ", p)
        self.assertIn("不得臆造", p)
        self.assertIn("材料开始", p)


class TestRender(unittest.TestCase):
    def test_report_path_naming(self):
        self.assertEqual(cr.report_path(Path("/x"), "2026-10-16").name,
                         "collect-2026-10-16.md")

    def test_skip_section_mentions_reason(self):
        s = cr.skip_section(FIXED_NOW, S2, "限频：测试")
        self.assertIn("跳过", s)
        self.assertIn("限频：测试", s)
        self.assertIn("idle_seconds=600.0", s)

    def test_ok_section_has_body_and_stats(self):
        s = cr.ok_section(FIXED_NOW, S2, opts(), 7, 4321, OK_REPLY, 12.3)
        self.assertIn("摘要正文：一切正常。", s)
        self.assertIn("7 段 / 4321 字符", s)
        self.assertIn("tokens=42", s)


class TestRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "brainstem"
        self.out = self.base / "out"
        self.gm = self.base / "git-memory"
        self.state = self.base / "state"
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def _log(self, m):
        self.logs.append(m)

    def _signal(self, body=None, day="2026-10-16"):
        d = self.root / "signals" / "s2"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "current.json"
        p.write_text(json.dumps(body or S2, ensure_ascii=False), encoding="utf-8")
        return p

    def _cur(self):
        return self.root / "signals" / "s2" / "current.json"

    def _report(self, day="2026-10-16"):
        return (self.out / f"collect-{day}.md").read_text(encoding="utf-8")

    def _run(self, chat, **over):
        return cr.run(self.root, self.out, self.gm, self.state, opts(**over),
                      self._log, chat=chat, now=FIXED_NOW)

    def test_no_signal_is_noop(self):
        f = FakeChat()
        self.assertEqual(self._run(f), 0)
        self.assertEqual(len(f.calls), 0)
        self.assertFalse(self.out.exists())

    def test_ok_writes_report_and_clears_signal(self):
        self._signal()
        f = FakeChat()
        self.assertEqual(self._run(f), 1)
        self.assertFalse(self._cur().exists())              # consumed -> path re-arms
        txt = self._report()
        self.assertIn("空闲信息收集报告", txt)
        self.assertIn("摘要正文：一切正常。", txt)
        self.assertEqual(txt.count("# 空闲信息收集报告"), 1)

    def test_exactly_one_llm_call(self):
        self._signal()
        f = FakeChat()
        self._run(f)
        self.assertEqual(len(f.calls), 1)

    def test_state_written_on_run(self):
        self._signal()
        self._run(FakeChat())
        st = json.loads((self.state / "consumer-s2.json").read_text(encoding="utf-8"))
        self.assertEqual(st["runs"], 1)
        self.assertEqual(st["last_result"], "ok")
        self.assertTrue(st["last_run_ts"].startswith("2026-10-16T21:03"))

    def test_failsoft_engine_unreachable(self):
        self._signal()
        f = FakeChat(FAIL_REPLY)
        self.assertEqual(self._run(f), 0)
        self.assertFalse(self._cur().exists())              # still cleared
        txt = self._report()
        self.assertIn("跳过", txt)
        self.assertIn("引擎不可达", txt)
        self.assertEqual(len(f.calls), 1)                   # exactly one try, no retry
        st = json.loads((self.state / "consumer-s2.json").read_text(encoding="utf-8"))
        self.assertEqual(st["last_result"], "skip")

    def test_empty_reply_treated_as_skip(self):
        self._signal()
        self.assertEqual(self._run(FakeChat(EMPTY_REPLY)), 0)
        self.assertIn("模型返回空", self._report())
        self.assertFalse(self._cur().exists())

    def test_rate_limit_skips_second_run(self):
        self._signal()
        f = FakeChat()
        self.assertEqual(self._run(f, min_interval_hours=6), 1)
        self._signal()                                       # signal fires again quickly
        self.assertEqual(self._run(f, min_interval_hours=6), 0)
        self.assertEqual(len(f.calls), 1)                    # second run did NOT call the LLM
        self.assertFalse(self._cur().exists())               # but signal still cleared
        self.assertIn("限频", self._report())

    def test_rate_limit_zero_allows_repeat(self):
        self._signal()
        f = FakeChat()
        self._run(f, min_interval_hours=0)
        self._signal()
        self._run(f, min_interval_hours=0)
        self.assertEqual(len(f.calls), 2)

    def test_budget_caps_passed_to_engine(self):
        self._signal()
        f = FakeChat()
        self._run(f, max_tokens=333, timeout=7.5, max_material_chars=200, model="m-test")
        c = f.calls[0]
        self.assertEqual(c["max_tokens"], 333)
        self.assertEqual(c["timeout"], 7.5)
        self.assertEqual(c["model"], "m-test")
        self.assertEqual(c["base_url"], cr.DEFAULT_BASE_URL)
        # material section inside the prompt is bounded by the cap (plus template overhead)
        self.assertLess(len(c["prompt"]), 200 + len(cr.PROMPT_TMPL) + 400)

    def test_malformed_signal_cleared_with_warning(self):
        d = self.root / "signals" / "s2"
        d.mkdir(parents=True)
        p = d / "current.json"
        p.write_text("{not json", encoding="utf-8")
        f = FakeChat()
        self.assertEqual(self._run(f), 0)
        self.assertFalse(p.exists())
        self.assertEqual(len(f.calls), 0)
        self.assertTrue(any("WARN" in m for m in self.logs))

    def test_day_rollover_new_file(self):
        self._signal(body={**S2, "ts": "2026-10-16T23:59:01+08:00"})
        self._run(FakeChat(), min_interval_hours=0)
        self._signal(body={**S2, "ts": "2026-10-17T00:29:01+08:00"})
        self._run(FakeChat(), min_interval_hours=0)
        self.assertTrue((self.out / "collect-2026-10-16.md").exists())
        self.assertTrue((self.out / "collect-2026-10-17.md").exists())

    def test_header_written_once_across_runs(self):
        self._signal()
        self._run(FakeChat(), min_interval_hours=0)
        self._signal(body={**S2, "ts": "2026-10-16T22:00:00+08:00"})
        self._run(FakeChat(), min_interval_hours=0)
        txt = self._report()
        self.assertEqual(txt.count("# 空闲信息收集报告"), 1)
        self.assertEqual(txt.count("## 运行"), 2)


class TestBoundary(unittest.TestCase):
    """章程 §2 非目标 / §5 护栏的源码级机械核验。"""

    @classmethod
    def setUpClass(cls):
        cls.src = (Path(__file__).resolve().parents[1] / "collect_report.py").read_text(
            encoding="utf-8")

    def test_no_memory_pool_or_git_write(self):
        for bad in (":8890", "git commit", "git push", "git add", "git reset",
                    "git checkout", "git-memory-sync"):
            self.assertNotIn(bad, self.src, f"must not reference {bad!r}")

    def test_no_tools_and_no_curl(self):
        for bad in ('"tools"', "tool_calls", "curl", "requests."):
            self.assertNotIn(bad, self.src, f"must not reference {bad!r}")

    def test_only_loopback_network_egress(self):
        urls = re.findall(r"https?://[^\s\"')`]+", self.src)
        self.assertTrue(urls, "expected the loopback tunnel endpoint to be present")
        for u in urls:
            self.assertTrue(u.startswith("http://127.0.0.1:18888"),
                            f"non-loopback egress {u!r}")
        self.assertNotIn("https://", self.src)

    def test_thinking_explicitly_disabled(self):
        self.assertIn("enable_thinking", self.src)


if __name__ == "__main__":
    unittest.main()
