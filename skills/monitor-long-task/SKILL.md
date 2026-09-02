---
name: monitor-long-task
description: Monitor detached commands or explicit completion checks, persist structured state, notify Feishu/Lark after verified success, and deliver one follow-up to the exact originating Codex session after its active turn becomes idle. Use for builds, tests, experiments, imports, downloads, migrations, batch jobs, or external convergence expected to run at least 10 minutes or outlive the current Codex turn; do not use for ordinary short commands.
---

# 长任务完成监控

使用 `scripts/long_task_monitor.py` 启动独立 worker。只有被托管命令退出码为 0，或显式完成检查返回 0，才视为成功；如实记录失败、超时和通知失败。

## 准备

解析安装路径，不要假设用户目录为 `/root`：

```bash
MONITOR="${CODEX_HOME:-$HOME/.codex}/skills/monitor-long-task/scripts/long_task_monitor.py"
test -f "$MONITOR"
```

脚本默认读取 `${CODEX_HOME:-$HOME/.codex}/secrets/feishu-long-task-webhook`。缺少配置时，让用户在自己的终端运行以下命令；不要索要或读取 webhook：

```bash
python3 "$MONITOR" configure
```

## 选择模式

- 本地前台命令：使用 `launch` 托管；不要提前单独启动同一命令。
- 外部任务或会自行 daemonize 的命令：先定义幂等、只读、仅在真实完成时返回 0 的检查，再使用 `watch`。启动任务前确认检查返回非 0，启动后立即启动 monitor。
- 预计不足 10 分钟的普通命令：直接执行，不使用本 Skill。

## 默认续接原会话

存在精确 `CODEX_THREAD_ID` 时，默认启用 `--resume-origin-session`；只有用户明确要求“只发飞书”“不要回原会话”时才关闭。缺少线程 ID 时继续任务但不续接，绝不使用 `--last`。

根据当前任务编写简短的 `--session-message`，要求恢复后的 Agent 读取真实状态并自行决定下一步，不要预先声称成功。可使用 `{status}`、`{summary_file}`、`{task_log}`、`{exit_code}`、`{error}` 等占位符：

```bash
--resume-origin-session \
--session-message '后台任务已进入 {status}。请读取 {summary_file} 和 {task_log}，基于真实结果继续原任务；不要重新启动该任务。'
```

让脚本处理会话空闲判定：新版 App Server 使用持久队列，旧版使用 `thread/status/changed` 事件。只有 `active` 表示 turn 正在运行；仅打开或聚焦的空闲会话不算 active。不要另建 Automation、定时任务或基于输出静默时间的轮询。没有 managed App Server socket 时，脚本会记录并使用单次兼容派发。

## 启动托管命令

选择简洁项目名和不含“完成”前缀的结果短语：

```bash
python3 "$MONITOR" launch \
  --project "<项目名>" \
  --summary "<简洁结果表述>" \
  --cwd "<任务工作目录>" \
  --timeout-minutes 240 \
  -- <命令> <参数...>
```

让被托管命令保持前台运行。成功通知固定为 `【<项目名>】：完成<简洁结果表述>`。

## 监控外部条件

```bash
python3 "$MONITOR" watch \
  --project "<项目名>" \
  --summary "<简洁结果表述>" \
  --cwd "<检查工作目录>" \
  --interval-seconds 30 \
  --timeout-minutes 240 \
  -- <检查命令> <参数...>
```

优先检查进程退出状态、明确完成标记或经过校验的最终产物；避免模糊日志关键词。只有 `watch` 可以轮询，而且只轮询显式外部条件。

## 启动后

1. 从启动 JSON 确认 `task_id` 和 `running` 或 `monitoring` 状态。
2. 只做一次状态核验：`python3 "$MONITOR" status <task_id>`。
3. 报告任务 ID、状态文件和日志路径。继续处理不依赖长任务结果的工作；没有其他工作时结束回合，不要持续盯终端或反复查状态。

## 状态纪律

- `completed`：任务成功且飞书已发送。
- `completed_notification_failed`：任务成功但飞书失败；可用 `retry-notification <task_id>` 重试，不能说已发送。
- `failed`、`start_failed`、`monitor_failed`：任务未完成，不发送“完成”消息。
- `timed_out`：监控超时，不自动终止仍在运行的真实任务。
- `origin_session_resume` 独立记录续接：`waiting_for_idle` 表示等待；`queued` 或 `dispatched` 表示已接受或已启动一次；`failed` 表示续接失败。不要对 `queued` 或 `dispatched` 再次手动续接。

状态和日志位于 `${CODEX_HOME:-$HOME/.codex}/long-task-monitors/<task_id>/`。不要打印或提交 webhook；脚本只接受官方 Feishu/Lark 地址，并要求密钥文件权限不宽于 `0600`。
