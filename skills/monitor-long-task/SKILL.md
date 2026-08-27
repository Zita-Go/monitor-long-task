---
name: monitor-long-task
description: Run and monitor detached long-running commands or explicit completion checks, persist structured state, and send a Feishu/Lark webhook only after verified success. Use for builds, tests, experiments, imports, downloads, migrations, batch jobs, or external convergence expected to take at least 10 minutes or continue beyond the current Codex turn; do not use for ordinary short commands.
---

# 长任务完成监控

使用 `scripts/long_task_monitor.py` 启动独立监控进程。只把退出码为 0 或显式检查命令返回 0 视为完成；失败、超时和通知失败必须如实记录，不能宣称完成。

## 准备

解析安装目录，不要假设用户目录为 `/root`：

```bash
MONITOR="${CODEX_HOME:-$HOME/.codex}/skills/monitor-long-task/scripts/long_task_monitor.py"
test -f "$MONITOR"
```

脚本默认从 `${CODEX_HOME:-$HOME/.codex}/secrets/feishu-long-task-webhook` 读取 webhook。缺少配置时，让用户在自己的终端安全运行以下命令；不要要求用户把 webhook 发到对话中：

```bash
python3 "$MONITOR" configure
```

## 选择模式

- 本地前台命令：使用 `launch`，让监控器托管命令。不要先单独启动同一命令。
- 外部任务或会自行 daemonize 的命令：先确定幂等、只读的完成检查，再使用 `watch`。启动任务前确认检查当前返回非 0，启动任务后立即启动监控。
- 预计不足 10 分钟的普通命令：直接执行，不启动监控。

## 启动托管命令

根据 Git 根目录或当前目录确定项目名，并写一个不含“完成”前缀的简洁结果短语：

```bash
python3 "$MONITOR" launch \
  --project "<项目名>" \
  --summary "<简洁结果表述>" \
  --cwd "<任务工作目录>" \
  --timeout-minutes 240 \
  -- <命令> <参数...>
```

让被托管命令保持前台运行。成功通知固定为 `【<项目名>】：完成<简洁结果表述>`。

## 监控外部完成条件

完成检查必须仅在目标真实达成时返回 0，并且不能改变外部状态：

```bash
python3 "$MONITOR" watch \
  --project "<项目名>" \
  --summary "<简洁结果表述>" \
  --cwd "<检查工作目录>" \
  --interval-seconds 30 \
  --timeout-minutes 240 \
  -- <检查命令> <参数...>
```

复杂条件可显式使用 `sh -lc '<只读检查>'`。避免匹配模糊日志关键词；优先检查进程退出状态、明确完成标记、最终产物及其校验结果。

## 启动后核验

1. 读取启动 JSON，确认获得 `task_id`，且状态为 `running` 或 `monitoring`。
2. 做一次状态核验，不要持续轮询：

```bash
python3 "$MONITOR" status <task_id>
```

3. 向用户报告任务 ID、状态文件和日志路径，然后结束当前回合。Webhook 发送不依赖 Codex 回合继续运行。

## 状态纪律

- `completed`：任务成功且飞书返回成功。
- `completed_notification_failed`：任务成功但通知失败；使用 `retry-notification <task_id>` 重试，不能说通知已发送。
- `failed`、`start_failed`、`monitor_failed`：任务未完成，不发送“完成”消息。
- `timed_out`：监控超时；托管任务不会被自动终止。检查真实进程状态后再决定是否继续监控或终止。
- 状态、终态摘要和日志默认保存在 `${CODEX_HOME:-$HOME/.codex}/long-task-monitors/<task_id>/`。

不要读取、打印或提交 webhook 密钥文件。脚本仅接受官方 Feishu/Lark 机器人 URL，并要求密钥文件权限不宽于 `0600`。
