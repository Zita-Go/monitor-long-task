# monitor-long-task

[![CI](https://github.com/Zita-Go/monitor-long-task/actions/workflows/ci.yml/badge.svg)](https://github.com/Zita-Go/monitor-long-task/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/tag/Zita-Go/monitor-long-task?sort=semver)](https://github.com/Zita-Go/monitor-long-task/tags)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/github/license/Zita-Go/monitor-long-task)](LICENSE)

A Codex skill that supervises long-running work, records structured state, and sends Feishu/Lark notifications only after verified success.

这是一个面向 Codex 的长任务监控 Skill：Agent 启动后台任务后即可结束当前回合；任务真实完成时发送飞书/Lark，也可以选择事件驱动地续接原 Codex 会话。

## 30 秒快速开始

下面假设你已经有飞书/Lark 自定义机器人 webhook；如果没有，先看[创建 webhook](#创建-webhook)。

```bash
git clone https://github.com/Zita-Go/monitor-long-task.git
cd monitor-long-task
bash scripts/install.sh

MONITOR="${CODEX_HOME:-$HOME/.codex}/skills/monitor-long-task/scripts/long_task_monitor.py"
python3 "$MONITOR" configure

python3 "$MONITOR" launch \
  --project "monitor-long-task" \
  --summary "Webhook 联调" \
  --cwd "$PWD" \
  --timeout-minutes 1 \
  -- true
```

配置时输入的 webhook 不会回显。最后一个命令成功后，群里应收到：

```text
【monitor-long-task】：完成Webhook 联调
```

## 选择模式

| 你的任务 | 使用方式 | 是否轮询 |
|---|---|---|
| 可以保持前台运行的构建、测试、训练命令 | `launch` 托管命令 | 否，等待进程退出 |
| 已在外部启动、会自行 daemonize，或只能检查产物的任务 | `watch` 检查完成条件 | 是，只轮询显式检查命令 |
| 只需要飞书/Lark 通知 | 不加会话续接参数 | 不涉及 |
| 结束后让原 Codex 会话继续处理 | 加 `--resume-origin-session` | 否，终态时单次派发 |

默认行为：

- 原会话续接默认关闭，避免产生未经请求的额外 Codex turn。
- 飞书/Lark 只在任务真实成功后发送；失败和超时不会伪装成“完成”。
- 监控超时不会自动杀死真实任务。
- 原会话续接只派发一次，不使用 `--last`，不轮询，也不自动重试。

## 目录

- [要求](#要求)
- [安装、更新与卸载](#安装更新与卸载)
- [创建 webhook](#创建-webhook)
- [基本使用](#基本使用)
- [飞书/Lark 通知内容](#飞书lark-通知内容)
- [事件驱动续接原 Codex 会话](#事件驱动续接原-codex-会话)
- [状态与故障排查](#状态与故障排查)
- [安全边界](#安全边界)
- [开发与验证](#开发与验证)

## 要求

- Python 3.10 或更高版本
- Codex CLI、IDE 或 Desktop 的本地执行环境
- Linux 或 macOS；Windows 后台进程行为尚未验证
- 可访问飞书或 Lark 自定义机器人 webhook

## 安装、更新与卸载

Skill 是按 Codex host 安装的。使用多个远程主机时，需要在每台实际运行任务的主机上分别安装；webhook 和任务状态也各自保存在该主机的 `CODEX_HOME` 中。

### 安装

```bash
git clone https://github.com/Zita-Go/monitor-long-task.git
cd monitor-long-task
bash scripts/install.sh
```

也可以直接告诉 Codex：

```text
请从 https://github.com/Zita-Go/monitor-long-task 的
skills/monitor-long-task 路径安装 monitor-long-task Skill。
```

安装后重新加载 Codex 任务，使 Skill 列表刷新。安装器不会覆盖已有 Skill。

### 更新

```bash
cd monitor-long-task
git pull --ff-only
bash scripts/install.sh --update
```

更新前的 Skill 会移动到：

```text
${CODEX_HOME:-$HOME/.codex}/skills/.backups/monitor-long-task-<UTC时间>
```

Webhook 和 `long-task-monitors/` 状态目录不会被修改。

### 可恢复卸载

```bash
bash scripts/install.sh --uninstall
```

安装器不会直接删除 Skill，而是将其移动到 `skills/.disabled/`。Webhook 和历史状态仍会保留，方便恢复或审计。

查看所有安装器选项：

```bash
bash scripts/install.sh --help
```

## 创建 webhook

本项目使用群聊里的“自定义机器人”入站 webhook，不需要创建企业自建应用。

1. 新建或打开一个用于接收通知的群聊。
2. 打开群设置，进入“群机器人”或“机器人”，选择“添加机器人”。
3. 选择“自定义机器人”，填写名称，例如 `Codex Long Task Monitor`。
4. 配置安全策略：
   - 简单场景：启用关键词校验并填写 `完成`；所有成功消息都包含这个词。
   - 有固定公网出口时：可以使用 IP 白名单。
   - 当前版本不生成签名参数，因此不要启用签名校验。
5. 创建机器人并复制 webhook 地址。

飞书格式：

```text
https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

Lark 格式使用 `open.larksuite.com`。界面和安全设置以官方文档为准：[飞书自定义机器人](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)、[Lark Custom Bot](https://open.larksuite.com/document/client-docs/bot-v3/add-custom-bot)。

运行交互式配置命令，将 webhook 保存到权限为 `0600` 的私有文件：

```bash
MONITOR="${CODEX_HOME:-$HOME/.codex}/skills/monitor-long-task/scripts/long_task_monitor.py"
python3 "$MONITOR" configure
```

默认位置：

```text
${CODEX_HOME:-$HOME/.codex}/secrets/feishu-long-task-webhook
```

使用 `configure --force` 可以替换现有地址。不要把真实 webhook 放进命令参数、聊天内容、Issue 或 Git 历史。

## 基本使用

先定义脚本路径：

```bash
MONITOR="${CODEX_HOME:-$HOME/.codex}/skills/monitor-long-task/scripts/long_task_monitor.py"
```

### `launch`：托管前台命令

```bash
python3 "$MONITOR" launch \
  --project "demo-project" \
  --summary "全量测试" \
  --cwd "/absolute/path/to/demo-project" \
  --timeout-minutes 240 \
  -- python3 -m unittest discover -v
```

被托管命令应保持前台运行。退出码为 `0` 才视为成功；非零退出码记录为 `failed`。会自行 daemonize 的程序应改用 `watch`。

### `watch`：检查外部完成条件

先确认检查命令当前返回非 `0`，启动外部任务后再运行：

```bash
python3 "$MONITOR" watch \
  --project "demo-project" \
  --summary "模型训练" \
  --cwd "/absolute/path/to/demo-project" \
  --interval-seconds 30 \
  --timeout-minutes 720 \
  -- sh -lc 'test -s artifacts/final_model/config.json'
```

`watch` 是唯一主动轮询模式。检查命令必须只读、幂等，并且仅在目标真实达成时返回 `0`。

### 查看状态

```bash
python3 "$MONITOR" status <task_id>
python3 "$MONITOR" list --limit 20
python3 "$MONITOR" retry-notification <task_id>
```

## 飞书/Lark 通知内容

监控器不会因为 Agent 当前回合结束就发送消息。只有 `launch` 命令退出码为 `0`，或 `watch` 完成检查返回 `0` 后才发送纯文本：

```text
【<项目名>】：完成<简洁结果表述>
```

- `<项目名>` 来自 `--project`；省略时从 Git 根目录名或 `--cwd` 推断。
- `<简洁结果表述>` 来自 `--summary`；脚本会去掉重复的“完成”前缀、合并空白，并限制为 80 个字符。

Webhook payload：

```json
{
  "msg_type": "text",
  "content": {
    "text": "【demo-project】：完成全量测试"
  }
}
```

通知不会包含命令、日志、工作目录、原始用户提示或 webhook。`failed`、`start_failed`、`monitor_failed` 和 `timed_out` 不发送“完成”；`completed_notification_failed` 表示任务成功但消息未送达，可以执行 `retry-notification`。

## 事件驱动续接原 Codex 会话

该功能默认关闭。只有用户要求任务结束后回到原会话时，Agent 才应加入：

```bash
--resume-origin-session \
--session-message '后台任务已进入 {status}。请读取 {summary_file} 和 {task_log}，基于真实结果自行继续原任务；不要重新启动该任务。'
```

完整示例：

```bash
python3 "$MONITOR" launch \
  --project "demo-project" \
  --summary "全量评测" \
  --cwd "/absolute/path/to/demo-project" \
  --timeout-minutes 240 \
  --resume-origin-session \
  --session-message '后台任务已进入 {status}。请读取 {summary_file}，自行决定下一步。' \
  -- python3 run_eval.py
```

消息由启动任务的 Agent 决定，不由脚本写死。可用占位符：

| 占位符 | 内容 |
|---|---|
| `{status}` | `completed`、`failed`、`timed_out` 等终态 |
| `{project}`、`{summary}`、`{task_id}` | 任务标识 |
| `{state_file}`、`{summary_file}`、`{task_log}` | 真实结果路径 |
| `{exit_code}`、`{error}` | 可用时的退出码或错误 |
| `{notification_status}` | 飞书/Lark 通知状态 |

Worker 在终态时读取启动环境中的精确 `CODEX_THREAD_ID`，单次执行官方 [`codex exec resume`](https://learn.chatgpt.com/docs/developer-commands#codex-exec)：

```text
codex exec resume <原线程ID> -
```

消息经 stdin 传递，不出现在进程参数中。此路径不创建 Automation、不轮询、不使用 `--last`，也不自动重试。它会启动一个新的 Codex turn，可能产生额外模型用量。

## 状态与故障排查

状态默认保存在：

```text
${CODEX_HOME:-$HOME/.codex}/long-task-monitors/<task_id>/
```

| 状态 | 含义 |
|---|---|
| `completed` | 任务成功且飞书/Lark 已发送 |
| `completed_notification_failed` | 任务成功，但飞书/Lark 未送达 |
| `failed` | 被托管命令返回非零退出码 |
| `start_failed` | 无法启动被托管命令 |
| `monitor_failed` | 监控器自身失败 |
| `timed_out` | 监控超时；实际任务不会被自动终止 |

关键文件：

| 文件 | 用途 |
|---|---|
| `state.json` | 最新结构化状态 |
| `summary.json` | 终态摘要 |
| `task.log` | 被托管命令或检查命令输出 |
| `monitor.log` | 后台 worker 自身日志 |
| `session-resume.log` | 原会话续接进程输出 |

### 飞书错误

| 错误码 | 含义 | 处理方式 |
|---|---|---|
| `19001` | webhook 无效 | 重新复制机器人 webhook |
| `19024` | 缺少安全关键词 | 将关键词设为 `完成`，或调整安全策略 |
| `19022` | 来源 IP 不在白名单 | 加入真实出口 IP，或改用关键词校验 |
| `19021` | 签名不匹配 | 当前版本应关闭签名校验 |

### 原会话没有恢复

- `origin thread id is required`：当前环境没有 `CODEX_THREAD_ID`；可以不启用续接，或显式传入 `--origin-thread-id <UUID>`。
- `codex binary was not found`：确认 `codex` 位于 `PATH`，或传入 `--codex-binary /absolute/path/to/codex`。
- `origin_session_resume.status=dispatched` 只表示续接进程已启动；最终 CLI 错误查看 `session-resume.log`。
- 原会话仍有活动 turn 时，续接可能失败；不会自动轮询或重试。

### 安装或更新失败

- 已安装时再次执行默认安装会拒绝覆盖；使用 `--update`。
- 更新会先完整 staging 新版本，再移动旧版本；失败时尽可能恢复旧 Skill。
- 安装器不会修改 webhook 和长任务状态目录。

## 安全边界

- Webhook 文件必须是普通文件，权限不得宽于 `0600`。
- 只接受官方 `open.feishu.cn` 或 `open.larksuite.com` 机器人地址。
- Webhook 不会写入任务状态或日志。
- 命令与会话消息模板保存在权限为 `0600` 的 `request.json`；不要把凭据放进参数或模板。
- 超时只停止监控等待，不会擅自杀死实际任务。
- 原会话续接使用精确 UUID；绝不以“最近会话”代替。

## 开发与验证

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
python3 -m compileall -q skills tests
bash -n scripts/install.sh
```

Skill 元数据还可以使用 Codex 自带的 `skill-creator/scripts/quick_validate.py` 校验。

## License

[MIT](LICENSE)
