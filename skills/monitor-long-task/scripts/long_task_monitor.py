#!/usr/bin/env python3
"""Run or observe a detached long task and notify Feishu after real success."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


__version__ = "0.3.0"
VERSION = 1


def default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


DEFAULT_CODEX_HOME = default_codex_home()
DEFAULT_STATE_DIR = DEFAULT_CODEX_HOME / "long-task-monitors"
DEFAULT_WEBHOOK_FILE = DEFAULT_CODEX_HOME / "secrets" / "feishu-long-task-webhook"
FEISHU_WEBHOOK_PREFIXES = (
    "https://open.feishu.cn/open-apis/bot/v2/hook/",
    "https://open.larksuite.com/open-apis/bot/v2/hook/",
)
SESSION_MESSAGE_FIELDS = frozenset(
    {
        "error",
        "exit_code",
        "notification_status",
        "project",
        "state_file",
        "status",
        "summary",
        "summary_file",
        "task_id",
        "task_log",
    }
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def atomic_write_private_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_private_text(path, payload)


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"JSON object expected: {path}")
    return value


def one_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_project(value: str) -> str:
    project = one_line(value).strip("【】[]：:")
    if not project:
        raise ValueError("project name must not be empty")
    return project[:80]


def normalize_summary(value: str) -> str:
    summary = one_line(value)
    summary = re.sub(r"^完成[：:\s]*", "", summary)
    if not summary:
        raise ValueError("summary must not be empty")
    if len(summary) > 80:
        summary = summary[:79].rstrip() + "…"
    return summary


def normalize_thread_id(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise ValueError("origin thread id is required")
    try:
        return str(uuid.UUID(candidate))
    except ValueError as exc:
        raise ValueError("origin thread id must be a UUID") from exc


def validate_session_message_template(value: str) -> str:
    template = value.strip()
    if not template:
        raise ValueError("session message is required when origin-session resume is enabled")
    for _literal, field_name, format_spec, conversion in string.Formatter().parse(template):
        if field_name is None:
            continue
        if field_name not in SESSION_MESSAGE_FIELDS:
            allowed = ", ".join(sorted(SESSION_MESSAGE_FIELDS))
            raise ValueError(f"unknown session message field {field_name!r}; allowed: {allowed}")
        if format_spec or conversion:
            raise ValueError("session message fields do not support format specs or conversions")
    return template


def infer_project(cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        root = Path(result.stdout.strip())
        if root.name:
            return normalize_project(root.name)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return normalize_project(cwd.name or "未命名项目")


def validate_webhook_url(value: str) -> str:
    url = value.strip()
    if any(character.isspace() for character in url) or not any(
        url.startswith(prefix) for prefix in FEISHU_WEBHOOK_PREFIXES
    ):
        raise ValueError("webhook must be an official Feishu or Lark bot URL")
    return url


def load_webhook_url(path: Path) -> str:
    resolved = path.expanduser().resolve()
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"webhook path is not a regular file: {resolved}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise PermissionError(f"webhook file permissions must be 0600 or stricter: {resolved}")
    return validate_webhook_url(resolved.read_text(encoding="utf-8"))


def save_webhook_url(path: Path, value: str, force: bool = False) -> Path:
    resolved = path.expanduser().resolve()
    url = validate_webhook_url(value)
    resolved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(resolved.parent, 0o700)
    if resolved.exists() and not force:
        raise FileExistsError(f"webhook file already exists: {resolved}; use --force to replace it")
    atomic_write_private_text(resolved, url + "\n")
    os.chmod(resolved, 0o600)
    return resolved


def send_feishu(webhook_file: Path, message: str) -> dict[str, Any]:
    url = load_webhook_url(webhook_file)
    payload = json.dumps(
        {"msg_type": "text", "content": {"text": message}},
        ensure_ascii=False,
    ).encode("utf-8")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    delays = (0, 2, 5, 10, 20)
    last_error = "unknown notification error"

    for attempt, delay in enumerate(delays, start=1):
        if delay:
            time.sleep(delay)
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with opener.open(request, timeout=15) as response:
                body = response.read(64 * 1024).decode("utf-8", errors="replace")
                status_code = response.status
            parsed = json.loads(body)
            api_codes = [parsed[key] for key in ("code", "StatusCode") if key in parsed]
            if status_code == 200 and api_codes and all(code == 0 for code in api_codes):
                return {
                    "status": "sent",
                    "attempts": attempt,
                    "http_status": status_code,
                    "sent_at": utc_now(),
                }
            last_error = f"unexpected Feishu response: HTTP {status_code}, codes={api_codes!r}"
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last_error = str(exc).replace(url, "<webhook>")

    return {
        "status": "failed",
        "attempts": len(delays),
        "error": one_line(last_error)[:500],
        "failed_at": utc_now(),
    }


def task_message(request: dict[str, Any]) -> str:
    return f"【{request['project']}】：完成{request['summary']}"


def task_paths(task_dir: Path) -> dict[str, str]:
    return {
        "state_file": str(task_dir / "state.json"),
        "summary_file": str(task_dir / "summary.json"),
        "task_log": str(task_dir / "task.log"),
        "monitor_log": str(task_dir / "monitor.log"),
        "session_resume_log": str(task_dir / "session-resume.log"),
    }


def write_state(task_dir: Path, state: dict[str, Any], terminal: bool = False) -> None:
    state["updated_at"] = utc_now()
    atomic_write_json(task_dir / "state.json", state)
    if terminal:
        atomic_write_json(task_dir / "summary.json", state)


def render_session_message(request: dict[str, Any], state: dict[str, Any]) -> str:
    config = request["origin_session_resume"]
    notification = state.get("notification")
    notification_status = notification.get("status", "not_sent") if isinstance(notification, dict) else "not_sent"
    values = {
        "error": state.get("error", ""),
        "exit_code": state.get("exit_code", ""),
        "notification_status": notification_status,
        "project": request["project"],
        "state_file": state["state_file"],
        "status": state["status"],
        "summary": request["summary"],
        "summary_file": state["summary_file"],
        "task_id": request["task_id"],
        "task_log": state["task_log"],
    }
    return config["message_template"].format_map(values).strip()


def dispatch_origin_session(
    task_dir: Path,
    request: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    config = request["origin_session_resume"]
    message = render_session_message(request, state)
    log_path = task_dir / "session-resume.log"
    try:
        with log_path.open("ab", buffering=0) as log_handle:
            os.chmod(log_path, 0o600)
            process = subprocess.Popen(
                [
                    config["codex_binary"],
                    "exec",
                    "resume",
                    config["thread_id"],
                    "-",
                ],
                cwd=request["cwd"],
                stdin=subprocess.PIPE,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
                close_fds=True,
            )
            if process.stdin is None:
                raise RuntimeError("codex resume process did not expose stdin")
            process.stdin.write(message + "\n")
            process.stdin.close()
        return {
            "status": "dispatched",
            "method": "codex-exec-resume",
            "thread_id": config["thread_id"],
            "pid": process.pid,
            "dispatched_at": utc_now(),
            "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
            "log": str(log_path),
        }
    except Exception as exc:
        return {
            "status": "failed",
            "method": "codex-exec-resume",
            "thread_id": config["thread_id"],
            "failed_at": utc_now(),
            "error": one_line(str(exc))[:500],
            "log": str(log_path),
        }


def finalize_terminal_state(
    task_dir: Path,
    request: dict[str, Any],
    state: dict[str, Any],
) -> None:
    write_state(task_dir, state, terminal=True)
    if "origin_session_resume" not in request or "origin_session_resume" in state:
        return
    state["origin_session_resume"] = dispatch_origin_session(task_dir, request, state)
    write_state(task_dir, state, terminal=True)


def initial_state(task_dir: Path, request: dict[str, Any], status: str) -> dict[str, Any]:
    return {
        "version": VERSION,
        "task_id": request["task_id"],
        "mode": request["mode"],
        "project": request["project"],
        "summary": request["summary"],
        "status": status,
        "created_at": request["created_at"],
        "started_at": utc_now(),
        "cwd": request["cwd"],
        "monitor_pid": os.getpid(),
        **task_paths(task_dir),
    }


def complete_and_notify(
    task_dir: Path,
    request: dict[str, Any],
    state: dict[str, Any],
) -> None:
    state["status"] = "notifying"
    state["finished_at"] = utc_now()
    write_state(task_dir, state)
    try:
        notification = send_feishu(Path(request["webhook_file"]), task_message(request))
    except Exception as exc:
        notification = {
            "status": "failed",
            "attempts": 0,
            "error": one_line(str(exc))[:500],
            "failed_at": utc_now(),
        }
    state["notification"] = notification
    state["status"] = "completed" if notification["status"] == "sent" else "completed_notification_failed"
    finalize_terminal_state(task_dir, request, state)


def run_managed_command(task_dir: Path, request: dict[str, Any]) -> int:
    state = initial_state(task_dir, request, "starting")
    write_state(task_dir, state)
    try:
        with (task_dir / "task.log").open("ab", buffering=0) as task_log:
            process = subprocess.Popen(
                request["command"],
                cwd=request["cwd"],
                stdin=subprocess.DEVNULL,
                stdout=task_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
            state["task_pid"] = process.pid
            state["status"] = "running"
            write_state(task_dir, state)
            try:
                exit_code = process.wait(timeout=request["timeout_seconds"])
            except subprocess.TimeoutExpired:
                state.update(
                    {
                        "status": "timed_out",
                        "finished_at": utc_now(),
                        "task_process_left_running": process.poll() is None,
                    }
                )
                finalize_terminal_state(task_dir, request, state)
                return 0
    except (OSError, ValueError) as exc:
        state.update(
            {
                "status": "start_failed",
                "finished_at": utc_now(),
                "error": one_line(str(exc))[:500],
            }
        )
        finalize_terminal_state(task_dir, request, state)
        return 1

    state["exit_code"] = exit_code
    if exit_code != 0:
        state.update({"status": "failed", "finished_at": utc_now()})
        finalize_terminal_state(task_dir, request, state)
        return exit_code

    complete_and_notify(task_dir, request, state)
    return 0


def append_check_log(task_dir: Path, check_number: int, result: subprocess.CompletedProcess[bytes]) -> None:
    with (task_dir / "task.log").open("ab", buffering=0) as handle:
        header = f"\n[{utc_now()}] check={check_number} exit={result.returncode}\n".encode("utf-8")
        handle.write(header)
        handle.write(result.stdout[:8192])
        if len(result.stdout) > 8192:
            handle.write(b"\n[output truncated]\n")


def watch_completion(task_dir: Path, request: dict[str, Any]) -> int:
    state = initial_state(task_dir, request, "monitoring")
    state["checks"] = 0
    write_state(task_dir, state)
    deadline = time.monotonic() + request["timeout_seconds"]

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            state.update({"status": "timed_out", "finished_at": utc_now()})
            finalize_terminal_state(task_dir, request, state)
            return 0

        state["checks"] += 1
        state["last_check_at"] = utc_now()
        try:
            result = subprocess.run(
                request["command"],
                cwd=request["cwd"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=min(request["check_timeout_seconds"], remaining),
            )
            append_check_log(task_dir, state["checks"], result)
            state["last_check_exit_code"] = result.returncode
            write_state(task_dir, state)
            if result.returncode == 0:
                complete_and_notify(task_dir, request, state)
                return 0
        except subprocess.TimeoutExpired as exc:
            state["last_check_exit_code"] = None
            state["last_check_error"] = f"check timed out after {exc.timeout} seconds"
            write_state(task_dir, state)
        except OSError as exc:
            state.update(
                {
                    "status": "monitor_failed",
                    "finished_at": utc_now(),
                    "error": one_line(str(exc))[:500],
                }
            )
            finalize_terminal_state(task_dir, request, state)
            return 1

        time.sleep(min(request["interval_seconds"], max(0, deadline - time.monotonic())))


def run_worker(task_dir: Path) -> int:
    request_path = task_dir / "request.json"
    request: dict[str, Any] | None = None
    try:
        request = read_json(request_path)
        if request["mode"] == "launch":
            return run_managed_command(task_dir, request)
        if request["mode"] == "watch":
            return watch_completion(task_dir, request)
        raise ValueError(f"unknown worker mode: {request.get('mode')!r}")
    except Exception as exc:  # The detached worker must always leave diagnostic state.
        fallback = {
            "version": VERSION,
            "task_id": task_dir.name,
            "status": "monitor_failed",
            "finished_at": utc_now(),
            "error": one_line(str(exc))[:500],
            **task_paths(task_dir),
        }
        if request is None:
            write_state(task_dir, fallback, terminal=True)
        else:
            fallback.setdefault("project", request.get("project", ""))
            fallback.setdefault("summary", request.get("summary", ""))
            finalize_terminal_state(task_dir, request, fallback)
        return 1


def command_from_remainder(values: list[str]) -> list[str]:
    command = list(values)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise ValueError("a command is required after --")
    return command


def resolve_codex_binary(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise ValueError("codex binary is required for origin-session resume")
    if os.sep in candidate:
        resolved = Path(candidate).expanduser().resolve()
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise ValueError(f"codex binary is not executable: {resolved}")
        return str(resolved)
    resolved_command = shutil.which(candidate)
    if not resolved_command:
        raise ValueError(f"codex binary was not found on PATH: {candidate}")
    return resolved_command


def build_origin_session_resume(args: argparse.Namespace) -> dict[str, str] | None:
    if not args.resume_origin_session:
        if args.session_message:
            raise ValueError("--session-message requires --resume-origin-session")
        return None
    return {
        "thread_id": normalize_thread_id(args.origin_thread_id),
        "message_template": validate_session_message_template(args.session_message or ""),
        "codex_binary": resolve_codex_binary(args.codex_binary),
    }


def create_task(args: argparse.Namespace, mode: str) -> int:
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise ValueError(f"cwd is not a directory: {cwd}")
    state_root = Path(args.state_dir).expanduser().resolve()
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    webhook_file = Path(args.webhook_file).expanduser().resolve()
    load_webhook_url(webhook_file)
    timeout_seconds = int(args.timeout_minutes * 60)
    if timeout_seconds < 60:
        raise ValueError("timeout must be at least one minute")
    command = command_from_remainder(args.command)
    origin_session_resume = build_origin_session_resume(args)
    project = normalize_project(args.project) if args.project else infer_project(cwd)
    summary = normalize_summary(args.summary)
    task_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(4)
    task_dir = state_root / task_id
    task_dir.mkdir(mode=0o700)
    request: dict[str, Any] = {
        "version": VERSION,
        "task_id": task_id,
        "mode": mode,
        "project": project,
        "summary": summary,
        "created_at": utc_now(),
        "cwd": str(cwd),
        "timeout_seconds": timeout_seconds,
        "command": command,
        "webhook_file": str(webhook_file),
    }
    if origin_session_resume is not None:
        request["origin_session_resume"] = origin_session_resume
    if mode == "watch":
        if args.interval_seconds < 1:
            raise ValueError("interval must be at least one second")
        if args.check_timeout_seconds < 1:
            raise ValueError("check timeout must be at least one second")
        request["interval_seconds"] = args.interval_seconds
        request["check_timeout_seconds"] = args.check_timeout_seconds
    atomic_write_json(task_dir / "request.json", request)

    monitor_log_path = task_dir / "monitor.log"
    with monitor_log_path.open("ab", buffering=0) as monitor_log:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "_worker", str(task_dir)],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=monitor_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    (task_dir / "monitor.pid").write_text(f"{process.pid}\n", encoding="ascii")
    os.chmod(task_dir / "monitor.pid", 0o600)

    deadline = time.monotonic() + 3
    state: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        state_path = task_dir / "state.json"
        if state_path.exists():
            state = read_json(state_path)
            break
        if process.poll() is not None:
            break
        time.sleep(0.05)

    if state is None:
        state = {
            "task_id": task_id,
            "status": "starting" if process.poll() is None else "monitor_failed",
            "monitor_pid": process.pid,
            **task_paths(task_dir),
        }
    print_json(state)
    return 0 if state["status"] in {"starting", "running", "monitoring", "notifying", "completed"} else 1


def show_status(args: argparse.Namespace) -> int:
    task_dir = Path(args.state_dir).expanduser().resolve() / args.task_id
    state_path = task_dir / "state.json"
    if not state_path.is_file():
        raise FileNotFoundError(f"state not found for task: {args.task_id}")
    state = read_json(state_path)
    print_json(state)
    return 0


def list_tasks(args: argparse.Namespace) -> int:
    state_root = Path(args.state_dir).expanduser().resolve()
    tasks: list[dict[str, Any]] = []
    if state_root.is_dir():
        for task_dir in sorted(state_root.iterdir(), reverse=True):
            state_path = task_dir / "state.json"
            if state_path.is_file():
                try:
                    tasks.append(read_json(state_path))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
            if len(tasks) >= args.limit:
                break
    print_json({"tasks": tasks})
    return 0


def retry_notification(args: argparse.Namespace) -> int:
    task_dir = Path(args.state_dir).expanduser().resolve() / args.task_id
    request = read_json(task_dir / "request.json")
    state = read_json(task_dir / "state.json")
    if state.get("status") != "completed_notification_failed":
        raise ValueError(f"task is not eligible for notification retry: {state.get('status')!r}")
    notification = send_feishu(Path(request["webhook_file"]), task_message(request))
    state["notification"] = notification
    state["status"] = "completed" if notification["status"] == "sent" else "completed_notification_failed"
    write_state(task_dir, state, terminal=True)
    print_json(state)
    return 0 if state["status"] == "completed" else 1


def configure_webhook(args: argparse.Namespace) -> int:
    if sys.stdin.isatty():
        value = getpass.getpass("Feishu/Lark webhook URL: ")
    else:
        value = sys.stdin.readline()
    if not value.strip():
        raise ValueError("no webhook URL was provided")
    saved_path = save_webhook_url(Path(args.webhook_file), value, force=args.force)
    print_json(
        {
            "ok": True,
            "webhook_file": str(saved_path),
            "permissions": "0600",
        }
    )
    return 0


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", help="project label; defaults to the Git root directory name")
    parser.add_argument("--summary", required=True, help="concise result phrase without a leading 完成")
    parser.add_argument("--cwd", default=os.getcwd(), help="working directory for the task or check")
    parser.add_argument("--timeout-minutes", type=float, default=1440, help="monitor timeout; default: 1440")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR), help="private task state directory")
    parser.add_argument("--webhook-file", default=str(DEFAULT_WEBHOOK_FILE), help="0600 file containing webhook URL")
    parser.add_argument(
        "--resume-origin-session",
        action="store_true",
        help="dispatch one codex exec resume event to the exact origin thread at terminal state",
    )
    parser.add_argument(
        "--origin-thread-id",
        default=os.environ.get("CODEX_THREAD_ID", ""),
        help="origin Codex thread UUID; defaults to CODEX_THREAD_ID",
    )
    parser.add_argument(
        "--session-message",
        help="agent-authored resume message template; required with --resume-origin-session",
    )
    parser.add_argument(
        "--codex-binary",
        default="codex",
        help="Codex executable used for the one-shot resume event",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="command and arguments after --")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    configure = subparsers.add_parser("configure", help="securely save a Feishu/Lark webhook URL")
    configure.add_argument("--webhook-file", default=str(DEFAULT_WEBHOOK_FILE))
    configure.add_argument("--force", action="store_true", help="replace an existing webhook file")
    configure.set_defaults(handler=configure_webhook)

    launch = subparsers.add_parser("launch", help="run a command under a detached monitor")
    add_common_arguments(launch)
    launch.set_defaults(handler=lambda args: create_task(args, "launch"))

    watch = subparsers.add_parser("watch", help="poll a read-only completion command")
    add_common_arguments(watch)
    watch.add_argument("--interval-seconds", type=float, default=30, help="seconds between checks")
    watch.add_argument("--check-timeout-seconds", type=float, default=30, help="timeout for each check")
    watch.set_defaults(handler=lambda args: create_task(args, "watch"))

    status_parser = subparsers.add_parser("status", help="print one task state")
    status_parser.add_argument("task_id")
    status_parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    status_parser.set_defaults(handler=show_status)

    list_parser = subparsers.add_parser("list", help="list recent task states")
    list_parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    list_parser.add_argument("--limit", type=int, default=20)
    list_parser.set_defaults(handler=list_tasks)

    retry = subparsers.add_parser("retry-notification", help="retry Feishu notification for a completed task")
    retry.add_argument("task_id")
    retry.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    retry.set_defaults(handler=retry_notification)
    return parser


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "_worker":
        return run_worker(Path(sys.argv[2]).expanduser().resolve())
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.handler(args)
    except Exception as exc:
        print_json({"ok": False, "error": one_line(str(exc))[:500]})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
