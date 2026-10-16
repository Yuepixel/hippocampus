# 脑干（Brainstem，第四器官）

Hippocampus 的**第四器官**——在现役器官机 **102** 常驻的**极小循环计时程序**：按机械
条件（时间 / 空闲 / 健康）发出信号，并做自身生命维持（liveness）与最小反射。

- **架构不变量**：只发机械信号 / 做机械反射，**永不判断**；**纯发布者**——只往
  `signals/` 写文件，永不 import 网关 / 器官内部实现、不被其反向依赖。
- 章程 / 设计：`docs/脑干设计-20261014.md`。

## 信号（章程 §3）

| # | 触发（机械） | 消费者 |
|---|---|---|
| S1 | 每日 04:00 北京时间 | **做梦 job**（`brainstem-s1-dream.path` → `dream.service`） |
| S2 | `loadavg` < 阈值持续 ≥ idle_min 分钟 | **C-S2 空闲信息收集**（`brainstem-consumers`，2026-10-16；无外网 / 无工具） |
| S3 | 周期健康快照（默认 30min） | **C-S3 健康上报**（`brainstem-consumers`，2026-10-16） |
| S4 | 目标探活状态转移（up↔down） | **C-S3 健康上报**（`brainstem-consumers`）（只读探测、不拉起） |
| S5 | 反射：`inbox/reflex/*.json` | v1 仅接口（无遏制动作） |

## 布局

```
brainstem.py          脑干本体（stdlib-only）
consume-s1-dream.sh   S1 消费者（root；触发 dream.service 后清信号）
tests/                单测（PC + 102 双跑）
signals/<type>/       <ts>.json（历史）+ current.json（最新，供 .path 订阅）
state/                heartbeat.json + brainstem.json（tick/去重状态，跨重启持久）
logs/                 brainstem.log + consumer-s1.log
inbox/reflex/         S5 反射收件箱
```

## 运行 / 验收

```bash
# 常驻（systemd）
systemctl status brainstem.service
# 单趟（调试）
/usr/bin/python3 brainstem.py --once --root /opt/hippocampus/brainstem
# 验收探针（本区 scripts\brainstem-verify.sh，scp 后跑）
```

触发交接：`bash scripts\brainstem-dream-handover.sh on|off`（S1 接管 / 归还 `dream.timer`）。

---
*本文由 DeepSeek-Flash AI 辅助完成*
