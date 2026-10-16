# 脑干消费者（Brainstem Consumers）

给脑干（第四器官）**已发、但 v1 无人消费**的机械信号接线真实消费者。设计见
`docs/脑干设计-20261014.md`。**脑干本体零改动**（仍是纯发布者）；消费者是独立进程，
**只订阅信号 → 做事 → 落报告**。

## 消费者

| # | 信号 | 消费者 | 动作 |
|---|---|---|---|
| C-S3 | S3（周期健康快照）+ S4（up↔down 转移） | `health_report.py` | 聚合出**可读健康报告**（`reports/health-YYYY-MM-DD.md`） |
| C-S2 | S2（服务器空闲） | `collect_report.py` | 只读采集本机材料 → **经隧道调一次本地 LLM** → 空闲信息收集报告（`reports/collect-YYYY-MM-DD.md`） |

> C-S2 = 章程 §3-3「无外网极小版」：**不联网、不给模型任何工具**，唯一网络出口 = `dream-engine-tunnel`
> （loopback `127.0.0.1:18888`）。放开外网须先接 `路线与判据.md` §9.2 授权 + 三道关（另立章程）。

## 硬约束（章程 §2）

- **只读** 脑干信号 `signals/<type>/current.json`，读完**删除**（delete-to-rearm）；
- **只写** 独立报告目录 + 自身日志/状态；**不写 mem0 池 / git-memory**；
- **不 import** 脑干 / 网关 / 器官内部实现；**零外部依赖**（stdlib-only）；
- **fail-soft**：单条信号出错只落日志、不崩。
- C-S2 额外护栏（章程 §5）：**预算 cap**（材料字符 / `max_tokens` / 超时）+ **步数 cap**
  （恰好 1 次 LLM 调用，不重试）+ **限频**（`--min-interval-hours`，默认 6h）+ 产出只落报告目录。

## 布局

```
health_report.py                  C-S3 消费者（stdlib-only）
collect_report.py                 C-S2 消费者（stdlib-only；唯一网络出口 = loopback 隧道）
tests/                            单测（PC + 102 双跑）
reports/health-YYYY-MM-DD.md      健康报告（按信号日期分文件）
reports/collect-YYYY-MM-DD.md     空闲信息收集报告（按信号日期分文件）
logs/consumer-{health,collect}.log 消费者日志
state/consumer-s2.json            C-S2 限频状态（上次运行时间 / 次数 / 结果）
```

## 单元（systemd，102）

- `brainstem-health.service` — oneshot；由下面两个 `.path` 触发，处理 S3+S4。
- `brainstem-s3-health.path` / `brainstem-s4-health.path` — `PathExists` 订阅
  `signals/{s3,s4}/current.json` → 触发 `brainstem-health.service`。
- `brainstem-collect.service` — oneshot（`TimeoutStartSec=240`）；由 `brainstem-s2-collect.path` 触发。
- `brainstem-s2-collect.path` — `PathExists` 订阅 `signals/s2/current.json` → 触发 `brainstem-collect.service`。

## 运行 / 验收

```bash
# 单趟（调试）C-S3
/usr/bin/python3 /opt/hippocampus/brainstem-consumers/health_report.py \
  --root /opt/hippocampus/brainstem \
  --out  /opt/hippocampus/brainstem-consumers/reports \
  --log  /opt/hippocampus/brainstem-consumers/logs/consumer-health.log

# 单趟（调试）C-S2（--min-interval-hours 0 = 跳过限频；base-url 指隧道末端）
/usr/bin/python3 /opt/hippocampus/brainstem-consumers/collect_report.py \
  --root /opt/hippocampus/brainstem --out /opt/hippocampus/brainstem-consumers/reports \
  --gm /opt/hippocampus/git-memory --state /opt/hippocampus/brainstem-consumers/state \
  --log /opt/hippocampus/brainstem-consumers/logs/consumer-collect.log \
  --base-url http://127.0.0.1:18888/v1 --min-interval-hours 0

# 部署 / 验收（本区 scripts\brainstem-consumers-deploy.sh / -verify.sh，scp 后跑）
```

## 回滚

```bash
sudo -n systemctl disable --now brainstem-s2-collect.path brainstem-s3-health.path brainstem-s4-health.path
# C-S2 额外：删 reports/collect-*.md 与 state/consumer-s2.json（消费者自身产物，无需回滚脑干）
```

---
*本文由 DeepSeek V4.1 Flash AI 辅助完成*
