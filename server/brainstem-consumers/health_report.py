#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hippocampus 脑干消费者 C-S3（健康上报）。

章程 `docs/脑干设计-20261014.md`（阶段 1）：订阅脑干信号
  S3（周期服务器健康快照，默认 30min）+ S4（up<->down 状态转移），
聚合成一份**可读健康报告**（默认 `.../brainstem-consumers/reports/health-YYYY-MM-DD.md`）。

硬约束（章程 §2）：
  - **只读** 脑干信号 `signals/<type>/current.json`，读完**删除**（delete-to-rearm，
    `.path` 单元据此重新武装——对齐 `consume-s1-dream.sh` 教训）；
  - **只写** 独立报告目录 + 自身日志；**不写 mem0 池 / git-memory**；
  - **不 import** 脑干 / 网关 / 器官内部实现；**零外部依赖**（stdlib-only）；
  - **fail-soft**：单条信号出错只落日志、不崩、不影响其它信号。

用法：
  health_report.py --root /opt/hippocampus/brainstem \
                   --out  /opt/hippocampus/brainstem-consumers/reports \
                   [--log /opt/hippocampus/brainstem-consumers/logs/consumer-health.log]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

SIGNALS = ("s3", "s4")   # 本消费者订阅的信号（S1 归做梦 job；S2 归 C-S2；S5 仅接口）


def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


def parse_ts(ts) -> dt.datetime:
    """解析脑干信号的 ISO 时间（`now_local().isoformat()`）；失败则回退当前本地时间。"""
    try:
        return dt.datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return now_local()


def line_for(sig: dict) -> str:
    """把一条信号渲染成报告里的一行（北京时间 HH:MM:SS + 内容）。"""
    when = parse_ts(sig.get("ts", "")).strftime("%H:%M:%S")
    stype = sig.get("type", "?")
    if stype == "s3":
        down = sig.get("down") or []
        down_s = ",".join(str(x) for x in down) if down else "-"
        return f"- {when}  [快照]  health={sig.get('health', '?')}  down={down_s}"
    if stype == "s4":
        return (f"- {when}  [转移]  {sig.get('name', '?')}  "
                f"{sig.get('from', '?')} -> {sig.get('to', '?')}")
    return f"- {when}  [{stype}]  {json.dumps(sig, ensure_ascii=False)}"


def report_path(out_dir: Path, day: str) -> Path:
    return Path(out_dir) / f"health-{day}.md"


def ensure_header(path: Path, day: str) -> None:
    """报告文件不存在时写入标题 + 说明块（只在首建时写一次）。"""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# 健康报告 — {day}（北京时间）\n\n"
        "> 消费者 C-S3（brainstem-consumers）｜来源：脑干信号 S3 快照 + S4 状态转移\n"
        "> 只读聚合；不写记忆池 / git-memory。\n\n",
        encoding="utf-8",
    )


def append_lines(path: Path, day: str, lines) -> None:
    ensure_header(path, day)
    with path.open("a", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(ln + "\n")


def process_signal(root: Path, out_dir: Path, stype: str, logger) -> int:
    """处理一个信号类型：读 current.json -> 追加报告 -> 删信号（重申）。返回处理条数（0/1）。"""
    cur = Path(root) / "signals" / stype / "current.json"
    if not cur.is_file():
        return 0
    try:
        sig = json.loads(cur.read_text(encoding="utf-8"))
    except Exception as e:                              # 坏信号：落日志 + 清掉（防 .path 卡死）
        logger(f"WARN {stype}: unreadable signal ({e!r}); clearing")
        try:
            cur.unlink()
        except OSError as e2:
            logger(f"WARN {stype}: cannot clear signal ({e2!r})")
        return 0
    day = parse_ts(sig.get("ts", "")).strftime("%Y-%m-%d")
    line = line_for(sig)
    try:
        append_lines(report_path(out_dir, day), day, [line])
    except Exception as e:
        logger(f"ERROR {stype}: cannot write report ({e!r})")
        return 0
    try:
        cur.unlink()                                    # delete-to-rearm
    except OSError as e:
        logger(f"WARN {stype}: cannot clear signal ({e!r})")
    logger(f"{stype}: {line}")
    return 1


def run(root: Path, out_dir: Path, logger) -> int:
    n = 0
    for stype in SIGNALS:
        try:
            n += process_signal(root, out_dir, stype, logger)
        except Exception as e:                          # fail-soft：一条出错不影响其它
            logger(f"ERROR {stype}: unexpected ({e!r})")
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Hippocampus brainstem consumer C-S3 (health report)")
    ap.add_argument("--root", default="/opt/hippocampus/brainstem")
    ap.add_argument("--out", default="/opt/hippocampus/brainstem-consumers/reports")
    ap.add_argument("--log", default=None, help="optional consumer log file")
    args = ap.parse_args(argv)

    def logger(msg: str) -> None:
        line = f"[{now_local().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line, flush=True)                        # -> journald
        if args.log:
            try:
                p = Path(args.log)
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass

    n = run(Path(args.root), Path(args.out), logger)
    logger(f"done: {n} signal(s) consumed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
