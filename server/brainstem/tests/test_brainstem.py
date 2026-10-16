#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for the brainstem (fourth organ, stdlib unittest - runs on PC and 102).

Run:  python -m unittest discover -s Hippocampus/server/brainstem/tests -v
"""
from __future__ import annotations

import datetime as dt
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brainstem  # noqa: E402


def mk(tmp: str, **kw) -> brainstem.Brainstem:
    defaults = dict(tick=30.0, s1_hhmm=(4, 0), idle_min=10.0, idle_load=2.0,
                    s3_interval=1800.0, probes=[])
    defaults.update(kw)
    return brainstem.Brainstem(Path(tmp), **defaults)


class TestTime(unittest.TestCase):
    def test_parse_hhmm(self):
        self.assertEqual(brainstem.parse_hhmm("04:00"), (4, 0))
        self.assertEqual(brainstem.parse_hhmm("3:5"), (3, 5))


class TestS1(unittest.TestCase):
    def _at(self, h, m, date="2026-10-17"):
        return dt.datetime.fromisoformat(f"{date}T{h:02d}:{m:02d}:00+08:00")

    def test_before_time_false(self):
        self.assertFalse(brainstem.s1_due(self._at(3, 59), (4, 0), ""))

    def test_at_time_true(self):
        self.assertTrue(brainstem.s1_due(self._at(4, 0), (4, 0), ""))

    def test_after_time_true(self):
        self.assertTrue(brainstem.s1_due(self._at(5, 30), (4, 0), ""))

    def test_already_today_false(self):
        self.assertFalse(brainstem.s1_due(self._at(9, 0), (4, 0), "2026-10-17"))

    def test_next_day_true(self):
        self.assertTrue(brainstem.s1_due(self._at(4, 1, "2026-10-18"), (4, 0), "2026-10-17"))


class TestIdle(unittest.TestCase):
    def test_step_accumulates_low_load(self):
        self.assertEqual(brainstem.idle_step(0.0, 0.5, 2.0, 30.0), 30.0)
        self.assertEqual(brainstem.idle_step(30.0, 1.9, 2.0, 30.0), 60.0)

    def test_step_resets_on_busy(self):
        self.assertEqual(brainstem.idle_step(120.0, 5.0, 2.0, 30.0), 0.0)

    def test_due(self):
        self.assertTrue(brainstem.idle_due(600.0, 600.0, False))
        self.assertFalse(brainstem.idle_due(600.0, 600.0, True))
        self.assertFalse(brainstem.idle_due(300.0, 600.0, False))


class TestS3S4(unittest.TestCase):
    def test_s3_due(self):
        self.assertFalse(brainstem.s3_due(1000.0, 900.0, 1800.0))
        self.assertTrue(brainstem.s3_due(3000.0, 900.0, 1800.0))

    def test_evaluate_s4_down_and_up(self):
        prev = {"a": "up", "b": "up"}
        cur = {"a": "down", "b": "up"}
        self.assertEqual(brainstem.evaluate_s4(prev, cur),
                         [{"name": "a", "from": "up", "to": "down"}])
        # recovery
        self.assertEqual(brainstem.evaluate_s4({"a": "down"}, {"a": "up"}),
                         [{"name": "a", "from": "down", "to": "up"}])

    def test_evaluate_s4_no_change(self):
        self.assertEqual(brainstem.evaluate_s4({"a": "up"}, {"a": "up"}), [])

    def test_evaluate_s4_new_target_no_event(self):
        self.assertEqual(brainstem.evaluate_s4({}, {"a": "up"}), [])


class TestProbes(unittest.TestCase):
    def test_tcp_up(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            self.assertTrue(brainstem.probe_tcp("127.0.0.1", port, timeout=2))
        finally:
            srv.close()

    def test_tcp_down(self):
        # bind then close -> port free
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.close()
        self.assertFalse(brainstem.probe_tcp("127.0.0.1", port, timeout=1))

    def test_run_probe_unknown_kind(self):
        self.assertFalse(brainstem.run_probe({"name": "x", "kind": "wat"}))


class TestOrgan(unittest.TestCase):
    def test_emit_writes_history_and_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            bs = mk(tmp)
            bs.emit("s1", {"action": "memory-review"})
            cur = Path(tmp) / "signals" / "s1" / "current.json"
            self.assertTrue(cur.exists())
            body = json.loads(cur.read_text(encoding="utf-8"))
            self.assertEqual(body["type"], "s1")
            self.assertEqual(body["action"], "memory-review")
            hist = list((Path(tmp) / "signals" / "s1").glob("*.json"))
            self.assertGreaterEqual(len(hist), 2)  # ts file + current.json

    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            bs = mk(tmp)
            st = bs.load_state()
            st["ticks"] = 7
            st["last_s1_date"] = "2026-10-16"
            bs.save_state(st)
            again = brainstem.Brainstem(Path(tmp), 30.0, (4, 0), 10.0, 2.0, 1800.0, []).load_state()
            self.assertEqual(again["ticks"], 7)
            self.assertEqual(again["last_s1_date"], "2026-10-16")

    def test_heartbeat_increments(self):
        with tempfile.TemporaryDirectory() as tmp:
            bs = mk(tmp)
            st = bs.load_state()
            bs.heartbeat(st)
            bs.heartbeat(st)
            self.assertEqual(st["ticks"], 2)
            hb = json.loads((Path(tmp) / "state" / "heartbeat.json").read_text(encoding="utf-8"))
            self.assertEqual(hb["ticks"], 2)

    def test_run_once_s1_emits_and_persists_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            bs = mk(tmp, s1_hhmm=(0, 0))  # 00:00 -> always due today
            st = bs.load_state()
            with mock.patch.object(brainstem, "run_probe", return_value=True):
                with mock.patch.object(brainstem, "read_loadavg", return_value=5.0):
                    bs.run_once(st)
            self.assertEqual(st["last_s1_date"], brainstem.now_local().strftime("%Y-%m-%d"))
            self.assertTrue((Path(tmp) / "signals" / "s1" / "current.json").exists())
            # second run same day -> no new s1 (date guard)
            st2 = bs.load_state()
            with mock.patch.object(brainstem, "run_probe", return_value=True):
                with mock.patch.object(brainstem, "read_loadavg", return_value=5.0):
                    bs.step_s1(st2)
            self.assertEqual(st2["last_s1_date"], st["last_s1_date"])

    def test_run_once_s4_transition_emits(self):
        with tempfile.TemporaryDirectory() as tmp:
            probes = [{"name": "org", "kind": "tcp", "host": "127.0.0.1", "port": 1}]
            bs = mk(tmp, probes=probes, s1_hhmm=(23, 59))
            st = bs.load_state()
            with mock.patch.object(brainstem, "run_probe", return_value=True):
                with mock.patch.object(brainstem, "read_loadavg", return_value=9.0):
                    bs.run_once(st)
            self.assertEqual(st["s4"], {"org": "up"})
            self.assertFalse((Path(tmp) / "signals" / "s4" / "current.json").exists())
            # now it goes down -> transition emits
            with mock.patch.object(brainstem, "run_probe", return_value=False):
                with mock.patch.object(brainstem, "read_loadavg", return_value=9.0):
                    bs.run_once(st)
            self.assertEqual(st["s4"], {"org": "down"})
            body = json.loads((Path(tmp) / "signals" / "s4" / "current.json").read_text(encoding="utf-8"))
            self.assertEqual(body["event"], "transition")
            self.assertEqual(body["to"], "down")

    def test_run_once_s2_emits_when_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            bs = mk(tmp, tick=60.0, idle_min=1.0, idle_load=2.0, s1_hhmm=(23, 59))
            st = bs.load_state()
            with mock.patch.object(brainstem, "run_probe", return_value=True):
                with mock.patch.object(brainstem, "read_loadavg", return_value=0.1):
                    bs.run_once(st)  # idle 60 >= 60 -> emit
            self.assertTrue(st["idle_emitted"])
            self.assertTrue((Path(tmp) / "signals" / "s2" / "current.json").exists())
            # busy resets and re-arms
            with mock.patch.object(brainstem, "run_probe", return_value=True):
                with mock.patch.object(brainstem, "read_loadavg", return_value=5.0):
                    bs.run_once(st)
            self.assertEqual(st["idle_seconds"], 0.0)
            self.assertFalse(st["idle_emitted"])

    def test_run_once_s5_reflex(self):
        with tempfile.TemporaryDirectory() as tmp:
            bs = mk(tmp, s1_hhmm=(23, 59))
            (Path(tmp) / "inbox" / "reflex" / "r1.json").write_text(
                '{"danger": "x"}', encoding="utf-8")
            st = bs.load_state()
            with mock.patch.object(brainstem, "run_probe", return_value=True):
                with mock.patch.object(brainstem, "read_loadavg", return_value=9.0):
                    bs.run_once(st)
            self.assertTrue((Path(tmp) / "signals" / "s5" / "current.json").exists())
            self.assertTrue((Path(tmp) / "inbox" / "reflex" / "r1.processed").exists())

    def test_run_once_survives_probe_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            bs = mk(tmp, s1_hhmm=(23, 59))
            st = bs.load_state()
            with mock.patch.object(brainstem, "run_probe", side_effect=RuntimeError("boom")):
                with mock.patch.object(brainstem, "read_loadavg", return_value=9.0):
                    bs.run_once(st)  # must not raise
            self.assertEqual(st["ticks"], 1)


if __name__ == "__main__":
    unittest.main()
