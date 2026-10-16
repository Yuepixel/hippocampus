#!/usr/bin/env python3
"""Hippocampus 脑干（brainstem，第四器官）。

定位（章程 `docs/脑干设计-20261014.md`）：在**现役器官机 102**常驻一个**极小的循环计时
程序**——按机械条件（时间 / 空闲 / 健康）发出信号，并做自身生命维持（liveness）与
最小反射；它是 M0 连续性的物理底座、§9.2 授权的执行者、M2 巩固（做梦）的驱动者。

架构不变量（章程 §2）：**只发机械信号 / 做机械反射，永不判断**；**纯发布者**——
只往 `signals/` 写文件，永不 import 网关 / 器官内部实现、不被其反向依赖。

信号（章程 §3）：
  S1 定时     -> 每日 04:00 北京时间（消费者 = 做梦 job）
  S2 空闲     -> 负载低于阈值持续 >= idle_min 分钟
  S3 健康     -> 周期性服务器健康快照
  S4 liveness -> 目标探活；状态转移（up->down / down->up）发信号
  S5 反射     -> 读 inbox/reflex/*.json（接口预留；v1 无真实消费者）

stdlib-only（Python 3.8+）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=5"]

# 目标探活（只读）——默认清单；可用 --config 覆盖。
DEFAULT_PROBES = [
    {"name": "gateway-101", "kind": "ssh_systemctl", "host": "user@<HEAD_IP>",
     "unit": "hippo-gateway", "key": "/home/user/.ssh/id_ed25519_dream"},
    {"name": "embed-102", "kind": "tcp", "host": "127.0.0.1", "port": 8084},
    {"name": "mem0-102", "kind": "tcp", "host": "127.0.0.1", "port": 8890},
    {"name": "engine-tunnel-102", "kind": "tcp", "host": "127.0.0.1", "port": 18888},
]


def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


def parse_hhmm(s: str):
    h, m = s.split(":")
    return int(h), int(m)


# ---------------------------------------------------------------- pure decisions
def s1_due(now: dt.datetime, s1_hhmm, last_date: str) -> bool:
    """S1：当日时刻 >= s1 且今日尚未发过。"""
    h, m = s1_hhmm
    today = now.strftime("%Y-%m-%d")
    return (last_date != today) and (now.hour, now.minute) >= (h, m)


def idle_step(prev_idle: float, load: float, load_threshold: float, tick: float) -> float:
    """空闲累计：负载低于阈值则累加 tick，否则归零。"""
    return prev_idle + tick if load < load_threshold else 0.0


def idle_due(idle_seconds: float, threshold_seconds: float, emitted: bool) -> bool:
    return (idle_seconds >= threshold_seconds) and (not emitted)


def s3_due(now_ts: float, last_ts: float, interval: float) -> bool:
    return (now_ts - last_ts) >= interval


def evaluate_s4(prev: dict, cur: dict) -> list:
    """状态转移事件：[{"name","from","to"}]（含 up->down 与 down->up）。"""
    events = []
    for name, state in cur.items():
        before = prev.get(name)
        if before is not None and before != state:
            events.append({"name": name, "from": before, "to": state})
    return events


# ---------------------------------------------------------------- probes (mechanical)
def read_loadavg() -> float:
    """1-minute load average; 0.0 on platforms without os.getloadavg (Windows)."""
    try:
        return float(os.getloadavg()[0])
    except (OSError, AttributeError):
        return 0.0


def probe_tcp(host, port, timeout=3.0) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def probe_ssh_systemctl(host, unit, key=None, timeout=10.0) -> bool:
    cmd = ["ssh", *SSH_OPTS]
    if key:
        cmd += ["-i", key]
    cmd += [host, "systemctl", "is-active", unit]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return r.stdout.strip() == "active"
    except Exception:
        return False


def run_probe(p: dict) -> bool:
    try:
        if p.get("kind") == "tcp":
            return probe_tcp(p["host"], p["port"])
        if p.get("kind") == "ssh_systemctl":
            return probe_ssh_systemctl(p["host"], p["unit"], p.get("key"))
    except Exception:
        return False
    return False


# ---------------------------------------------------------------- the organ
class Brainstem:
    def __init__(self, root: Path, tick: float, s1_hhmm, idle_min: float,
                 idle_load: float, s3_interval: float, probes: list):
        self.root = Path(root)
        self.sig = self.root / "signals"
        self.state_dir = self.root / "state"
        self.inbox = self.root / "inbox"
        self.logd = self.root / "logs"
        for d in (self.sig, self.state_dir, self.inbox / "reflex", self.logd):
            d.mkdir(parents=True, exist_ok=True)
        self.tick = float(tick)
        self.s1_hhmm = s1_hhmm
        self.idle_min = float(idle_min)
        self.idle_load = float(idle_load)
        self.s3_interval = float(s3_interval)
        self.probes = probes

    # -- logging
    def log(self, msg: str):
        line = f"[{now_local().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line, flush=True)
        try:
            with (self.logd / "brainstem.log").open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    # -- state
    def _state_path(self) -> Path:
        return self.state_dir / "brainstem.json"

    def load_state(self) -> dict:
        try:
            return json.loads(self._state_path().read_text(encoding="utf-8"))
        except Exception:
            return {"ticks": 0, "last_s1_date": "", "last_s3_ts": 0.0,
                    "idle_seconds": 0.0, "idle_emitted": False,
                    "s4": {}, "last_reflex_ts": 0.0}

    def save_state(self, st: dict):
        tmp = self._state_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._state_path())

    # -- publish (纯发布者：只写 signals/)
    def emit(self, stype: str, payload: dict):
        d = self.sig / stype
        d.mkdir(parents=True, exist_ok=True)
        ts = now_local().strftime("%Y%m%dT%H%M%S")
        body = {"type": stype, "ts": now_local().isoformat(), **payload}
        txt = json.dumps(body, ensure_ascii=False, indent=2)
        (d / f"{ts}.json").write_text(txt, encoding="utf-8")
        (d / "current.json").write_text(txt, encoding="utf-8")
        self.log(f"emit {stype}: {json.dumps(payload, ensure_ascii=False)}")

    def heartbeat(self, st: dict):
        st["ticks"] = int(st.get("ticks", 0)) + 1
        (self.state_dir / "heartbeat.json").write_text(
            json.dumps({"ticks": st["ticks"], "ts": now_local().isoformat()},
                       ensure_ascii=False), encoding="utf-8")

    # -- steps
    def step_probes(self, st: dict) -> dict:
        cur = {p["name"]: ("up" if run_probe(p) else "down") for p in self.probes}
        for e in evaluate_s4(st.get("s4", {}), cur):
            self.emit("s4", {"event": "transition", **e})
        st["s4"] = cur
        return cur

    def step_s3(self, st: dict, now_ts: float):
        if s3_due(now_ts, float(st.get("last_s3_ts", 0.0)), self.s3_interval):
            cur = st.get("s4", {})
            down = sorted(k for k, v in cur.items() if v != "up")
            self.emit("s3", {"health": "ok" if not down else "degraded", "down": down})
            st["last_s3_ts"] = now_ts

    def step_s1(self, st: dict):
        if s1_due(now_local(), self.s1_hhmm, st.get("last_s1_date", "")):
            self.emit("s1", {"action": "memory-review", "note": "M2 巩固（做梦 job）"})
            st["last_s1_date"] = now_local().strftime("%Y-%m-%d")

    def step_s2(self, st: dict):
        load = read_loadavg()
        st["idle_seconds"] = idle_step(float(st.get("idle_seconds", 0.0)), load,
                                       self.idle_load, self.tick)
        if idle_due(st["idle_seconds"], self.idle_min * 60.0, bool(st.get("idle_emitted", False))):
            self.emit("s2", {"idle_seconds": round(st["idle_seconds"], 1), "loadavg": round(load, 2)})
            st["idle_emitted"] = True
        if st["idle_seconds"] == 0.0:
            st["idle_emitted"] = False

    def step_s5(self, st: dict):
        for f in sorted((self.inbox / "reflex").glob("*.json")):
            try:
                payload = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                payload = {"raw": f.name}
            self.emit("s5", {"reflex": payload})
            try:
                f.replace(f.with_suffix(".processed"))
            except OSError:
                pass
            st["last_reflex_ts"] = time.time()

    def run_once(self, st: dict):
        now_ts = time.time()
        self.heartbeat(st)
        self.step_probes(st)
        self.step_s3(st, now_ts)
        self.step_s1(st)
        self.step_s2(st)
        self.step_s5(st)
        self.save_state(st)

    def run_forever(self):
        st = self.load_state()
        self.log(f"brainstem start tick={self.tick}s s1={self.s1_hhmm} "
                 f"idle_min={self.idle_min} idle_load={self.idle_load} "
                 f"probes={[p['name'] for p in self.probes]} resume_ticks={st.get('ticks')}")
        while True:
            try:
                self.run_once(st)
            except Exception as e:  # fail-soft：一个 tick 出错不熄灭
                self.log(f"ERROR step failed: {e!r}")
            time.sleep(self.tick)


def load_config(path: str | None) -> dict:
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"WARN: config unreadable ({path}): {e!r}", file=sys.stderr)
        return {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Hippocampus brainstem (fourth organ)")
    ap.add_argument("--root", default="/opt/hippocampus/brainstem")
    ap.add_argument("--tick", type=float, default=30.0)
    ap.add_argument("--s1", default="04:00", help="S1 daily time HH:MM (Asia/Shanghai)")
    ap.add_argument("--idle-min", type=float, default=10.0)
    ap.add_argument("--idle-load", type=float, default=2.0)
    ap.add_argument("--s3-interval", type=float, default=1800.0)
    ap.add_argument("--config", default=None, help="optional JSON config (probes override)")
    ap.add_argument("--once", action="store_true", help="run a single tick and exit")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    probes = cfg.get("probes") or DEFAULT_PROBES
    bs = Brainstem(Path(args.root), args.tick, parse_hhmm(args.s1), args.idle_min,
                   args.idle_load, args.s3_interval, probes)
    if args.once:
        bs.run_once(bs.load_state())
        return 0
    bs.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
