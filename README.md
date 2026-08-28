# monitor-long-task

A Codex skill that supervises detached long-running commands or explicit completion checks, persists structured state, and sends a Feishu/Lark notification only after verified success.

这是一个面向 Codex 的长任务监控 Skill。它适合运行时间超过 10 分钟的构建、测试、实验、导入、下载、迁移和批处理任务，让 Agent 启动后台监控后结束当前回合，并在真实成功后发送飞书或 Lark 通知。

## 特点

- `launch`：托管前台命令，只有退出码为 `0` 才通知。
- `watch`：轮询幂等、只读的外部完成检查。
- 不依赖 Codex 的 `agent-turn-complete` 事件，避免把普通回合结束误判成外部任务成功。
- 将 `state.json`、终态 `summary.json` 和任务日志写入私有状态目录。
- 区分任务失败、监控超时和通知失败。
- 成功消息采用固定、简洁且不泄露日志的文本格式，详见[通知内容](#通知内容)。
- 可选地在终态事件发生时单次续接精确的原 Codex 会话，不使用轮询。
- 仅使用 Python 标准库，无第三方运行时依赖。

## 要求

- Python 3.10 或更高版本
- Codex CLI、IDE 或 Desktop 的本地执行环境
- Linux 或 macOS；当前后台进程行为尚未在 Windows 上验证
- 可访问飞书或 Lark 自定义机器人 webhook

## 安装

```bash
git clone https://github.com/Zita-Go/monitor-long-task.git
cd monitor-long-task
bash scripts/install.sh
```

也可以让 Codex 从本仓库的 `skills/monitor-long-task` 路径安装该 Skill。

安装脚本不会覆盖已有 Skill。安装后，重新加载 Codex 任务以刷新 Skill 列表。

## 创建飞书 webhook

本项目使用的是“群自定义机器人”的入站 webhook，不需要创建飞书企业自建应用。飞书界面的具体文字可能随客户端版本略有变化，完整说明以[飞书官方《自定义机器人使用指南》](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)为准。

1. 在飞书中新建或打开一个用于接收通知的群聊。
2. 打开群设置，进入“群机器人”或“机器人”，选择“添加机器人”。
3. 选择“自定义机器人”，填写名称，例如 `Codex Long Task Monitor`。
4. 配置安全设置：
   - 最简单：启用关键词校验并填写 `完成`，本项目的所有成功消息都包含这个词；
   - 有固定公网出口时：也可以使用 IP 白名单；
   - 当前版本尚未生成签名参数，因此不要启用“签名校验”，否则飞书会拒绝消息。
5. 完成创建并复制形如以下格式的 webhook 地址：

```text
https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

Webhook 本身相当于发送凭据。不要把真实地址写进 README、Issue、命令参数、Git 历史或发给 Agent；如果地址已经泄露，应在飞书中删除或重新生成机器人 webhook。

## 保存 webhook

不要把 webhook 写进仓库、命令参数或聊天内容。运行交互式配置命令，输入内容不会回显：

```bash
MONITOR="${CODEX_HOME:-$HOME/.codex}/skills/monitor-long-task/scripts/long_task_monitor.py"
python3 "$MONITOR" configure
```

默认密钥位置为 `${CODEX_HOME:-$HOME/.codex}/secrets/feishu-long-task-webhook`，文件权限为 `0600`。如需替换现有配置，使用 `configure --force`。

### 验证 webhook

配置完成后，可以托管一个立即成功的命令进行端到端测试：

```bash
python3 "$MONITOR" launch \
  --project "monitor-long-task" \
  --summary "Webhook 联调" \
  --cwd "$PWD" \
  --timeout-minutes 1 \
  -- true
```

飞书中应收到：

```text
【monitor-long-task】：完成Webhook 联调
```

常见飞书错误：

| 错误码 | 含义 | 处理方式 |
|---|---|---|
| `19001` | webhook 地址无效 | 重新复制机器人 webhook |
| `19024` | 消息未包含安全关键词 | 将机器人关键词设为 `完成`，或调整安全策略 |
| `19022` | 来源 IP 不在白名单 | 加入实际出口 IP，或改用关键词校验 |
| `19021` | 签名不匹配 | 当前版本应关闭签名校验 |

## 通知内容

监控器不会在 Agent 结束当前回合时发送消息。只有满足以下任一真实成功条件后才发送：

- `launch` 托管的命令结束，且退出码为 `0`；
- `watch` 的完成检查返回 `0`。

飞书或 Lark 收到的是一条纯文本消息，不是卡片。格式固定为：

```text
【<项目名>】：完成<简洁结果表述>
```

字段来源：

- `<项目名>` 来自 `--project`；省略时从 Git 仓库根目录名或 `--cwd` 目录名推断。
- `<简洁结果表述>` 来自 `--summary`；脚本会去掉重复的“完成”前缀、合并换行与多余空白，并限制在 80 个字符内。

例如：

```bash
python3 "$MONITOR" launch \
  --project "demo-project" \
  --summary "TB2.1 全量评测" \
  --cwd "/absolute/path/to/demo-project" \
  --timeout-minutes 240 \
  -- python3 run_eval.py
```

成功后实际收到：

```text
【demo-project】：完成TB2.1 全量评测
```

对应的飞书/Lark webhook payload 为：

```json
{
  "msg_type": "text",
  "content": {
    "text": "【demo-project】：完成TB2.1 全量评测"
  }
}
```

通知中不会包含命令、日志、工作目录、用户原始提示或 webhook。`failed`、`start_failed`、`monitor_failed` 和 `timed_out` 不发送“完成”消息；`completed_notification_failed` 表示任务已经成功，但消息未送达，可使用 `retry-notification` 重发同一条消息。

## 事件驱动续接原 Codex 会话

如果希望任务结束后让启动它的 Codex 会话继续处理结果，可以显式启用一次性续接：

```bash
python3 "$MONITOR" launch \
  --project "demo-project" \
  --summary "全量评测" \
  --cwd "/absolute/path/to/demo-project" \
  --timeout-minutes 240 \
  --resume-origin-session \
  --session-message '后台任务已进入 {status}。请读取 {summary_file} 和 {task_log}，基于真实结果自行继续原任务；不要重新启动该任务。' \
  -- python3 run_eval.py
```

这条消息不是脚本写死的。启动监控的 Agent 应根据任务自行决定 `--session-message`，并可引用以下字段：

| 占位符 | 内容 |
|---|---|
| `{status}` | 最终状态，例如 `completed`、`failed` 或 `timed_out` |
| `{project}`、`{summary}` | 启动时的项目名和结果短语 |
| `{task_id}` | 监控任务 ID |
| `{state_file}`、`{summary_file}`、`{task_log}` | 真实状态、终态摘要和任务日志路径 |
| `{exit_code}`、`{error}` | 可用时的退出码或监控错误 |
| `{notification_status}` | 飞书/Lark 通知状态 |

实现是事件驱动的：worker 在任务进入终态时，从启动环境捕获的精确 `CODEX_THREAD_ID` 调用官方文档中的 [`codex exec resume`](https://learn.chatgpt.com/docs/developer-commands#codex-exec)，并且只执行一次：

```text
codex exec resume <原线程ID> -
```

Agent 编写的消息通过标准输入传递，不出现在进程参数中。该路径不会创建 Automation、定时任务或轮询，也绝不使用 `--last`。无论成功、失败还是超时，每个监控任务最多派发一次；派发信息记录在 `origin_session_resume` 字段，续接进程的后续输出写入私有 `session-resume.log`。

注意：续接会启动一个新的 Codex turn，可能产生额外模型用量。`origin_session_resume.status=dispatched` 只证明续接进程已经启动，不等于该 turn 最终成功；原会话仍处于活动 turn 等后续错误应查看 `session-resume.log`。缺少 `CODEX_THREAD_ID`、找不到 `codex` 命令或进程无法启动时记录为 `failed`。任何续接问题都不会影响已经落盘的任务状态或飞书通知，也不会循环重试。

## 使用

### 托管长命令

```bash
python3 "$MONITOR" launch \
  --project "my-project" \
  --summary "全量测试" \
  --cwd "/absolute/path/to/project" \
  --timeout-minutes 240 \
  -- python3 -m unittest discover -v
```

`launch` 要求被托管命令保持前台运行。会自行 daemonize 的程序应改用 `watch`。

### 监控外部条件

先确认检查命令当前返回非 `0`，启动外部任务后立即运行：

```bash
python3 "$MONITOR" watch \
  --project "my-project" \
  --summary "模型训练" \
  --cwd "/absolute/path/to/project" \
  --interval-seconds 30 \
  --timeout-minutes 720 \
  -- sh -lc 'test -s artifacts/final_model/config.json'
```

检查命令必须只读，并且仅在目标真实达成时返回 `0`。

### 查看状态

```bash
python3 "$MONITOR" status <task_id>
python3 "$MONITOR" list --limit 20
python3 "$MONITOR" retry-notification <task_id>
```

状态默认保存在 `${CODEX_HOME:-$HOME/.codex}/long-task-monitors/<task_id>/`。

| 状态 | 含义 |
|---|---|
| `completed` | 任务成功且通知发送成功 |
| `completed_notification_failed` | 任务成功，但通知失败 |
| `failed` | 被托管命令返回非零退出码 |
| `start_failed` | 无法启动被托管命令 |
| `monitor_failed` | 监控器自身失败 |
| `timed_out` | 监控超过时限；被托管进程不会被自动终止 |

## 安全边界

- webhook 文件必须为普通文件，且权限不得宽于 `0600`。
- 只接受官方 `open.feishu.cn` 或 `open.larksuite.com` 机器人地址。
- webhook 不会写入任务状态或日志。
- 命令参数会保存在权限为 `0600` 的 `request.json`；不要把凭据放入命令参数。
- `watch` 检查由 Agent 或用户提供，应保持只读和幂等。
- 超时只终止监控等待，不会擅自杀死实际任务。

## 开发与验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q skills tests
bash -n scripts/install.sh
```

Skill 元数据可使用 Codex 自带的 `skill-creator/scripts/quick_validate.py` 进一步校验。

## License

[MIT](LICENSE)
