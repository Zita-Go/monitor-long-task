---
name: monitor-long-task
description: Run and monitor detached long-running commands or explicit completion checks, persist structured state, send a Feishu/Lark webhook after verified success, and deliver one follow-up to the exact originating Codex session after its active turn becomes idle. Use for builds, tests, experiments, imports, downloads, migrations, batch jobs, or external convergence expected to take at least 10 minutes or continue beyond the current Codex turn; do not use for ordinary short commands.
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

## 默认：结束后唤醒原会话

从 Codex 会话启动长任务且环境中存在精确 `CODEX_THREAD_ID` 时，默认启用 `--resume-origin-session`。只有用户明确要求“只发飞书”“不要回原会话”或同等意图时才关闭。这是终态事件触发的一次性续接，不要创建 Automation、定时任务，不要根据输出静默时间判断会话是否空闲。

1. 确认环境中存在精确的 `CODEX_THREAD_ID`，不要使用 `--last`。缺少线程 ID 时继续执行任务但不启用续接，并向用户如实说明。
2. 根据当前任务上下文自行编写 `--session-message`；脚本不固定消息内容。
3. 消息应让恢复后的 Agent 读取真实状态并决定下一步，避免预先声称成功。可使用 `{status}`、`{summary_file}`、`{task_log}`、`{exit_code}`、`{error}` 等占位符。

示例参数：

```bash
--resume-origin-session \
--session-message '后台任务已进入 {status}。请读取 {summary_file} 和 {task_log}，基于真实结果自行继续原任务；不要重新启动该任务。'
```

任务进入任一终态时，worker 按以下顺序处理：

1. 如果有可用的 Codex App Server Unix socket，读取精确线程的正式状态。只有 `status.type=active` 表示 Agent 正在运行；仅打开或聚焦一个无运行 turn 的会话仍是 `idle`。
2. App Server 支持持久消息队列时，将消息一次性加入 `thread/queue/add`；由 App Server 在该线程 idle 后启动。
3. 较旧 App Server 不支持队列时，订阅 `thread/status/changed`；active 时阻塞等待事件，idle 时才执行一次 `codex exec resume "$CODEX_THREAD_ID" -`。
4. 连接中断时只做有上限的传输重连；连接正常时绝不轮询线程状态。
5. 没有 managed App Server socket 的独立 CLI 环境无法读取跨进程实时状态，此时保留原有的一次性 resume，并在状态中明确记录 idle gate 不可用。

消息始终从标准输入传递。续接失败只影响 `origin_session_resume`，不能改写长任务或飞书通知的真实结果。

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

3. 向用户报告任务 ID、状态文件和日志路径。后台 worker 不占用 Agent 的终端等待；若当前请求还有无需等待长任务即可完成的分析或操作，继续处理。没有其他可做工作时再结束当前回合，Webhook 与原会话续接均不依赖本回合保持运行。

## 状态纪律

- `completed`：任务成功且飞书返回成功。
- `completed_notification_failed`：任务成功但通知失败；使用 `retry-notification <task_id>` 重试，不能说通知已发送。
- `failed`、`start_failed`、`monitor_failed`：任务未完成，不发送“完成”消息。
- `timed_out`：监控超时；托管任务不会被自动终止。检查真实进程状态后再决定是否继续监控或终止。
- `origin_session_resume.status=waiting_for_idle`：已持久化消息摘要，正在连接 App Server 或等待精确线程的 idle 事件。
- `origin_session_resume.status=queued`：新版 App Server 的持久队列已接受消息，将在原线程 idle 后启动。
- `origin_session_resume.status=dispatched`：旧版 App Server 路径已在确认 idle 后启动一次精确续接，或无 socket 的独立 CLI 路径已执行单次兼容派发；后续 CLI 结果查看 `session-resume.log` 和 `idle_gate`。
- `origin_session_resume.status=failed`：idle 等待超时、线程处于 `systemError` 或续接无法派发；不把它误报为成功。
- 状态、终态摘要和日志默认保存在 `${CODEX_HOME:-$HOME/.codex}/long-task-monitors/<task_id>/`。

不要读取、打印或提交 webhook 密钥文件。脚本仅接受官方 Feishu/Lark 机器人 URL，并要求密钥文件权限不宽于 `0600`。
