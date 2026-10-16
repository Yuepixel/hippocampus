#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for the brainstem consumer C-S3 (health report; stdlib unittest, PC + 102).

Run:  python -m unittest discover -s Hippocampus/server/brainstem-consumers/tests -v
"""
from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import health_report as hr  # noqa: E402


def _write(root: Path, stype: str, body: dict) -> Path:
    d = Path(root) / "signals" / stype
    d.mkdir(parents=True, exist_ok=True)
    p = d / "current.json"
    p.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return p


S3 = {"type": "s3", "ts": "2026-10-16T20:30:01+08:00", "health": "ok", "down": []}
S4 = {"type": "s4", "ts": "2026-10-16T20:31:02+08:00", "event": "transition",
      "name": "gateway-101", "from": "up", "to": "down"}


class TestRender(unittest.TestCase):
    def test_line_s3_ok(self):
        ln = hr.line_for(S3)
        self.assertIn("[快照]", ln)
        self.assertIn("health=ok", ln)
        self.assertIn("down=-", ln)
        self.assertIn("20:30:01", ln)

    def test_line_s3_degraded_lists_down(self):
        ln = hr.line_for({"type": "s3", "ts": "2026-10-16T20:30:01+08:00",
                          "health": "degraded", "down": ["mem0-102"]})
        self.assertIn("down=mem0-102", ln)

    def test_line_s4(self):
        ln = hr.line_for(S4)
        self.assertIn("[转移]", ln)
        self.assertIn("gateway-101", ln)
        self.assertIn("up -> down", ln)

    def test_parse_ts_fallback_on_garbage(self):
        self.assertIsInstance(hr.parse_ts("not-a-date"), dt.datetime)

    def test_report_path_naming(self):
        self.assertEqual(hr.report_path(Path("/x"), "2026-10-16").name,
                         "health-2026-10-16.md")


class TestProcess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "root"
        self.out = self.base / "out"
        self.root.mkdir(parents=True, exist_ok=True)
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def _log(self, m):
        self.logs.append(m)

    def _report(self, day="2026-10-16"):
        return (self.out / f"health-{day}.md").read_text(encoding="utf-8")

    def test_s3_appends_and_clears(self):
        sig = _write(self.root, "s3", S3)
        self.assertEqual(hr.process_signal(self.root, self.out, "s3", self._log), 1)
        self.assertFalse(sig.exists())                 # signal consumed -> path re-arms
        txt = self._report()
        self.assertIn("健康报告", txt)
        self.assertIn("health=ok", txt)

    def test_s4_appends_both_directions(self):
        _write(self.root, "s4", S4)
        self.assertEqual(hr.process_signal(self.root, self.out, "s4", self._log), 1)
        _write(self.root, "s4", {**S4, "from": "down", "to": "up"})
        self.assertEqual(hr.process_signal(self.root, self.out, "s4", self._log), 1)
        txt = self._report()
        self.assertIn("up -> down", txt)
        self.assertIn("down -> up", txt)

    def test_no_signal_is_noop(self):
        self.assertEqual(hr.process_signal(self.root, self.out, "s3", self._log), 0)
        self.assertFalse(self.out.exists())

    def test_malformed_signal_cleared_with_warning(self):
        d = self.root / "signals" / "s3"
        d.mkdir(parents=True)
        p = d / "current.json"
        p.write_text("{not json", encoding="utf-8")
        self.assertEqual(hr.process_signal(self.root, self.out, "s3", self._log), 0)
        self.assertFalse(p.exists())                   # cleared so the path does not jam
        self.assertTrue(any("WARN" in m for m in self.logs))

    def test_header_written_once(self):
        _write(self.root, "s3", S3)
        hr.process_signal(self.root, self.out, "s3", self._log)
        _write(self.root, "s3", {**S3, "ts": "2026-10-16T21:00:01+08:00"})
        hr.process_signal(self.root, self.out, "s3", self._log)
        txt = self._report()
        self.assertEqual(txt.count("# 健康报告"), 1)
        self.assertEqual(txt.count("[快照]"), 2)

    def test_day_rollover_new_file(self):
        _write(self.root, "s3", {**S3, "ts": "2026-10-16T23:59:01+08:00"})
        hr.process_signal(self.root, self.out, "s3", self._log)
        _write(self.root, "s3", {**S3, "ts": "2026-10-17T00:29:01+08:00"})
        hr.process_signal(self.root, self.out, "s3", self._log)
        self.assertTrue((self.out / "health-2026-10-16.md").exists())
        self.assertTrue((self.out / "health-2026-10-17.md").exists())

    def test_run_consumes_both_signals(self):
        _write(self.root, "s3", S3)
        _write(self.root, "s4", S4)
        self.assertEqual(hr.run(self.root, self.out, self._log), 2)
        txt = self._report()
        self.assertIn("[快照]", txt)
        self.assertIn("[转移]", txt)

    def test_run_failsoft_on_missing_root(self):
        self.assertEqual(hr.run(self.base / "nope", self.out, self._log), 0)


class TestBoundary(unittest.TestCase):
    def test_source_has_no_memory_or_request_path_refs(self):
        src = (Path(__file__).resolve().parents[1] / "health_report.py").read_text(encoding="utf-8")
        for bad in (":8890", "git commit", "v1/chat", "curl", "18888", "8888"):
            self.assertNotIn(bad, src, f"consumer must not reference {bad!r}")


if __name__ == "__main__":
    unittest.main()
