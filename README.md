# monitor-long-task

A Codex skill that supervises detached long-running commands or explicit completion checks, persists structured state, and sends a Feishu/Lark notification only after verified success.

这是一个面向 Codex 的长任务监控 Skill。它适合运行时间超过 10 分钟的构建、测试、实验、导入、下载、迁移和批处理任务，让 Agent 启动后台监控后结束当前回合，并在真实成功后发送飞书或 Lark 通知。

## 特点

- `launch`：托管前台命令，只有退出码为 `0` 才通知。
- `watch`：轮询幂等、只读的外部完成检查。
- 不依赖 Codex 的 `agent-turn-complete` 事件，避免把普通回合结束误判成外部任务成功。
- 将 `state.json`、终态 `summary.json` 和任务日志写入私有状态目录。
- 区分任务失败、监控超时和通知失败。
- 成功消息固定为 `【项目】：完成简洁表述`。
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

## 配置 webhook

不要把 webhook 写进仓库、命令参数或聊天内容。运行交互式配置命令，输入内容不会回显：

```bash
MONITOR="${CODEX_HOME:-$HOME/.codex}/skills/monitor-long-task/scripts/long_task_monitor.py"
python3 "$MONITOR" configure
```

默认密钥位置为 `${CODEX_HOME:-$HOME/.codex}/secrets/feishu-long-task-webhook`，文件权限为 `0600`。如需替换现有配置，使用 `configure --force`。

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
