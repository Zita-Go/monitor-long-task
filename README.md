# monitor-long-task

[![CI](https://github.com/Zita-Go/monitor-long-task/actions/workflows/ci.yml/badge.svg)](https://github.com/Zita-Go/monitor-long-task/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/tag/Zita-Go/monitor-long-task?sort=semver)](https://github.com/Zita-Go/monitor-long-task/tags)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/github/license/Zita-Go/monitor-long-task)](LICENSE)

A Codex skill for detached long-running tasks, verified Feishu/Lark notifications, and event-driven continuation in the exact originating Codex session.

这是一个面向 Codex 的长任务 Skill：安装并配置一次后，用户只需正常描述任务。Agent 会选择监控方式、启动后台任务，并在真实终态发生时发送飞书/Lark；需要时还可以无轮询地续接启动任务的原 Codex 会话。

## 目录

- [快速开始](#快速开始)
- [事件驱动续接原 Codex 会话](#事件驱动续接原-codex-会话)
- [Agent 如何工作](#agent-如何工作)
- [安装、更新与卸载](#安装更新与卸载)
- [创建 webhook](#创建-webhook)
- [高级 CLI 参考](#高级-cli-参考)
- [通知内容](#通知内容)
- [状态与故障排查](#状态与故障排查)
- [安全边界](#安全边界)

## 快速开始

### 1. 安装 Skill

```bash
git clone https://github.com/Zita-Go/monitor-long-task.git
cd monitor-long-task
bash scripts/install.sh
```

安装后重新加载 Codex 任务，使 Skill 列表刷新。

### 2. 只配置一次 webhook

Webhook 是凭据，不能发到聊天中。请在自己的终端运行：

```bash
MONITOR="${CODEX_HOME:-$HOME/.codex}/skills/monitor-long-task/scripts/long_task_monitor.py"
python3 "$MONITOR" configure
```

输入不会回显。配置默认保存在 `${CODEX_HOME:-$HOME/.codex}/secrets/feishu-long-task-webhook`，权限为 `0600`。

没有 webhook 时，先看[创建 webhook](#创建-webhook)。

### 3. 使用自然语言发起任务

只需要完成通知：

```text
帮我跑完整评测，这是长任务，结束后飞书通知我。
```

任务结束后还要回到当前会话继续处理：

```text
启动训练；任务结束后回到当前会话，读取最终产物并继续汇总。
```

等待外部系统产生结果：

```text
启动导入任务并等待最终产物出现，完成后飞书通知我。
```

Agent 会自行决定项目名、摘要、超时时间、使用 `launch` 还是 `watch`，以及是否启用原会话续接。若任务意图不明显，也可以显式提到 `$monitor-long-task`。

成功时飞书/Lark 收到：

```text
【<项目名>】：完成<简洁结果表述>
```

例如：

```text
【demo-project】：完成TB2.1 全量评测
```

## 事件驱动续接原 Codex 会话

当用户要求“任务结束后回到原会话继续处理”时，Agent 会启用 `--resume-origin-session` 并根据任务上下文自行编写续接消息。任务进入成功、失败或超时等终态后，worker 使用启动时捕获的精确 `CODEX_THREAD_ID`，单次调用官方 [`codex exec resume`](https://learn.chatgpt.com/docs/developer-commands#codex-exec)：

```text
codex exec resume <原线程ID> -
```

消息经 stdin 传递，不出现在进程参数中。新的 Codex turn 会回到原会话，读取状态、检查产物并自行决定后续工作。

这个过程：

- 由任务终态事件直接触发；
- 不创建 Automation 或定时任务；
- 不轮询原会话；
- 不使用容易误选会话的 `--last`；
- 每个监控任务最多派发一次；
- 失败时只记录日志，不自动重试。

续接消息不是脚本写死的。Agent 可在消息中引用 `{status}`、`{summary_file}`、`{task_log}`、`{exit_code}` 和 `{error}` 等终态字段。例如：

```bash
--resume-origin-session \
--session-message '后台任务已进入 {status}。请读取 {summary_file} 和 {task_log}，基于真实结果自行继续原任务；不要重新启动该任务。'
```

续接会产生一个额外 Codex turn，因此默认关闭。只有用户表达“回到原会话继续处理”的意图时，Agent 才应启用。

## Agent 如何工作

```mermaid
flowchart LR
    U[用户自然语言请求] --> A[Codex Agent]
    A --> M[monitor-long-task]
    M --> T[后台长任务]
    T -->|终态事件| M
    M -->|验证成功| F[飞书 / Lark]
    M -->|可选：单次 resume| C[精确原 Codex 会话]
```

普通用户不需要选择 CLI 参数。Agent 根据意图处理：

| 用户意图 | Agent 行为 | 是否轮询 |
|---|---|---|
| 托管本地构建、测试或训练命令 | 使用 `launch` 等待进程退出 | 否 |
| 等待外部任务、明确标记或最终产物 | 使用 `watch` 检查完成条件 | 是，只检查显式外部条件 |
| 只要求完成后通知 | 不启用原会话续接 | 不涉及 |
| 要求结束后继续分析或汇总 | 启用 `--resume-origin-session` | 否，终态时单次派发 |
| 普通短命令 | 不使用该 Skill | 不涉及 |

默认行为：

- 飞书/Lark 只在验证成功后发送；失败和超时不伪装成“完成”。
- 原会话续接默认关闭，避免未经请求的额外 Codex turn。
- 原会话续接本身从不轮询；`watch` 只服务于无法直接托管的外部条件。
- 超时不会自动杀死仍在运行的真实任务。

## 安装、更新与卸载

### 要求

- Python 3.10 或更高版本
- Codex CLI、IDE 或 Desktop 的本地执行环境
- Linux 或 macOS；Windows 后台进程行为尚未验证
- 可访问飞书或 Lark 自定义机器人 webhook

Skill 按 Codex host 安装。使用多个远程主机时，需要在每台实际运行任务的主机上分别安装；webhook 和任务状态也各自保存在该主机的 `CODEX_HOME` 中。

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

安装器不会覆盖已有 Skill。

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

查看所有选项：

```bash
bash scripts/install.sh --help
```

## 创建 webhook

本项目使用群聊中的“自定义机器人”入站 webhook，不需要创建企业自建应用。

1. 新建或打开一个用于接收通知的群聊。
2. 在群设置中进入“群机器人”或“机器人”，选择“添加机器人”。
3. 选择“自定义机器人”，填写名称，例如 `Codex Long Task Monitor`。
4. 配置安全策略：
   - 简单场景：启用关键词校验并填写 `完成`；所有成功消息都包含这个词。
   - 有固定公网出口时：可以使用 IP 白名单。
   - 当前版本不生成签名参数，不要启用签名校验。
5. 创建机器人并复制 webhook 地址。

飞书格式：

```text
https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

Lark 使用 `open.larksuite.com`。界面和安全设置以官方文档为准：[飞书自定义机器人](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)、[Lark Custom Bot](https://open.larksuite.com/document/client-docs/bot-v3/add-custom-bot)。

运行交互式配置命令：

```bash
MONITOR="${CODEX_HOME:-$HOME/.codex}/skills/monitor-long-task/scripts/long_task_monitor.py"
python3 "$MONITOR" configure
```

使用 `configure --force` 可以替换现有地址。不要把真实 webhook 放进命令参数、聊天内容、Issue 或 Git 历史；地址泄露后应立即重新生成。

### 可选：验证 webhook

普通使用无需运行测试命令。排查安装时，可以执行：

```bash
python3 "$MONITOR" launch \
  --project "monitor-long-task" \
  --summary "Webhook 联调" \
  --cwd "$PWD" \
  --timeout-minutes 1 \
  -- true
```

## 高级 CLI 参考

以下命令主要供 Agent、集成开发和故障排查使用。

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

被托管命令应保持前台运行。退出码为 `0` 才视为成功；会自行 daemonize 的程序应改用 `watch`。

### `watch`：检查外部完成条件

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

## 通知内容

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

通知不包含命令、日志、工作目录、原始用户提示或 webhook。`failed`、`start_failed`、`monitor_failed` 和 `timed_out` 不发送“完成”；`completed_notification_failed` 表示任务成功但消息未送达，可以执行 `retry-notification`。

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
