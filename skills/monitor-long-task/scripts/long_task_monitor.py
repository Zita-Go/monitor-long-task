#!/usr/bin/env python3
"""Monitor a detached long task, notify Feishu, and resume its origin session safely."""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import stat
import string
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


__version__ = "0.4.1"
VERSION = 1


def default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


DEFAULT_CODEX_HOME = default_codex_home()
DEFAULT_STATE_DIR = DEFAULT_CODEX_HOME / "long-task-monitors"
DEFAULT_WEBHOOK_FILE = DEFAULT_CODEX_HOME / "secrets" / "feishu-long-task-webhook"
DEFAULT_APP_SERVER_SOCKET = DEFAULT_CODEX_HOME / "app-server-control" / "app-server-control.sock"
APP_SERVER_MAX_HEADER_BYTES = 64 * 1024
APP_SERVER_MAX_MESSAGE_BYTES = 16 * 1024 * 1024
APP_SERVER_RECONNECT_DELAYS = (1, 2, 5, 10, 30, 60)
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


class AppServerTransportError(RuntimeError):
    """The managed App Server connection was lost or could not be opened."""


class AppServerProtocolError(RuntimeError):
    """The managed App Server sent an invalid or unsupported response."""


class AppServerRequestError(AppServerProtocolError):
    """The managed App Server rejected a JSON-RPC request."""

    def __init__(self, method: str, error: dict[str, Any]) -> None:
        self.method = method
        self.code = error.get("code")
        self.message = str(error.get("message", "request failed"))
        super().__init__(f"{method} failed ({self.code}): {self.message}")


class OriginSessionIdleTimeout(RuntimeError):
    """The origin thread did not become idle before its delivery deadline."""


class OriginQueueOutcomeUnknown(RuntimeError):
    """A queue mutation may have succeeded, so retrying could duplicate it."""


class UnixWebSocketConnection:
    """Minimal RFC 6455 client for a local App Server Unix socket."""

    _GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, path: Path, timeout_seconds: float) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.socket: socket.socket | None = None
        self.buffer = bytearray()

    def __enter__(self) -> "UnixWebSocketConnection":
        self.connect()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def connect(self) -> None:
        connection: socket.socket | None = None
        try:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(self.timeout_seconds)
            connection.connect(str(self.path))
        except (AttributeError, OSError) as exc:
            if connection is not None:
                connection.close()
            raise AppServerTransportError(str(exc)) from exc

        websocket_key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {websocket_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        try:
            connection.sendall(request)
            header = self._read_http_header(connection)
            self._validate_upgrade_response(header, websocket_key)
        except (OSError, AppServerProtocolError, AppServerTransportError) as exc:
            connection.close()
            if isinstance(exc, (AppServerProtocolError, AppServerTransportError)):
                raise
            raise AppServerTransportError(str(exc)) from exc
        self.socket = connection

    def close(self) -> None:
        if self.socket is not None:
            try:
                self.socket.close()
            finally:
                self.socket = None
                self.buffer.clear()

    def _read_http_header(self, connection: socket.socket) -> bytes:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            if len(data) >= APP_SERVER_MAX_HEADER_BYTES:
                raise AppServerProtocolError("App Server WebSocket response header is too large")
            chunk = connection.recv(4096)
            if not chunk:
                raise AppServerTransportError("App Server closed during WebSocket upgrade")
            data.extend(chunk)
        header, remainder = bytes(data).split(b"\r\n\r\n", 1)
        self.buffer.extend(remainder)
        return header

    @classmethod
    def _validate_upgrade_response(cls, header: bytes, websocket_key: str) -> None:
        lines = header.decode("iso-8859-1").split("\r\n")
        if not lines or not lines[0].startswith(("HTTP/1.1 101 ", "HTTP/1.0 101 ")):
            status = lines[0] if lines else "empty response"
            raise AppServerProtocolError(f"App Server rejected WebSocket upgrade: {status}")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
        if headers.get("upgrade", "").casefold() != "websocket":
            raise AppServerProtocolError("App Server did not confirm a WebSocket upgrade")
        connection_tokens = {
            value.strip().casefold() for value in headers.get("connection", "").split(",")
        }
        if "upgrade" not in connection_tokens:
            raise AppServerProtocolError("App Server returned an invalid WebSocket connection header")
        expected = base64.b64encode(
            hashlib.sha1(
                (websocket_key + cls._GUID).encode("ascii"),
                usedforsecurity=False,
            ).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise AppServerProtocolError("App Server returned an invalid WebSocket accept key")

    def send_json(self, value: dict[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_frame(0x1, payload)

    def receive_json(self, timeout_seconds: float) -> dict[str, Any]:
        if self.socket is None:
            raise AppServerTransportError("App Server WebSocket is not connected")
        self.socket.settimeout(max(0.001, timeout_seconds))
        fragments = bytearray()
        message_opcode: int | None = None
        while True:
            finished, opcode, payload = self._receive_frame()
            if opcode == 0x8:
                raise AppServerTransportError("App Server closed the WebSocket")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1:
                if message_opcode is not None:
                    raise AppServerProtocolError("received a new WebSocket message mid-fragment")
                message_opcode = opcode
                fragments.extend(payload)
            elif opcode == 0x0:
                if message_opcode is None:
                    raise AppServerProtocolError("received an unexpected WebSocket continuation frame")
                fragments.extend(payload)
            else:
                raise AppServerProtocolError(f"unsupported WebSocket opcode: {opcode}")
            if len(fragments) > APP_SERVER_MAX_MESSAGE_BYTES:
                raise AppServerProtocolError("App Server WebSocket message is too large")
            if not finished:
                continue
            try:
                value = json.loads(fragments.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AppServerProtocolError("App Server sent invalid JSON") from exc
            if not isinstance(value, dict):
                raise AppServerProtocolError("App Server JSON-RPC message must be an object")
            return value

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self.socket is None:
            raise AppServerTransportError("App Server WebSocket is not connected")
        if len(payload) > APP_SERVER_MAX_MESSAGE_BYTES:
            raise AppServerProtocolError("outgoing WebSocket message is too large")
        mask = secrets.token_bytes(4)
        first_byte = 0x80 | opcode
        if len(payload) < 126:
            header = bytes((first_byte, 0x80 | len(payload)))
        elif len(payload) < 65536:
            header = bytes((first_byte, 0x80 | 126)) + struct.pack("!H", len(payload))
        else:
            header = bytes((first_byte, 0x80 | 127)) + struct.pack("!Q", len(payload))
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        try:
            self.socket.sendall(header + mask + masked)
        except OSError as exc:
            raise AppServerTransportError(str(exc)) from exc

    def _receive_frame(self) -> tuple[bool, int, bytes]:
        first_byte, second_byte = self._receive_exact(2)
        if first_byte & 0x70:
            raise AppServerProtocolError("unsupported WebSocket extension bits")
        finished = bool(first_byte & 0x80)
        opcode = first_byte & 0x0F
        masked = bool(second_byte & 0x80)
        if masked:
            raise AppServerProtocolError("server WebSocket frames must not be masked")
        length = second_byte & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._receive_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._receive_exact(8))[0]
        if opcode >= 0x8 and (not finished or length > 125):
            raise AppServerProtocolError("invalid WebSocket control frame")
        if length > APP_SERVER_MAX_MESSAGE_BYTES:
            raise AppServerProtocolError("App Server WebSocket frame is too large")
        return finished, opcode, self._receive_exact(length)

    def _receive_exact(self, length: int) -> bytes:
        if self.socket is None:
            raise AppServerTransportError("App Server WebSocket is not connected")
        try:
            while len(self.buffer) < length:
                chunk = self.socket.recv(min(65536, length - len(self.buffer)))
                if not chunk:
                    raise AppServerTransportError("App Server WebSocket disconnected")
                self.buffer.extend(chunk)
        except TimeoutError:
            raise
        except OSError as exc:
            raise AppServerTransportError(str(exc)) from exc
        payload = bytes(self.buffer[:length])
        del self.buffer[:length]
        return payload


class AppServerClient:
    """Small JSON-RPC client used only for origin-thread status and queue APIs."""

    def __init__(self, socket_path: Path, timeout_seconds: float = 15) -> None:
        self.connection = UnixWebSocketConnection(socket_path, timeout_seconds)
        self.timeout_seconds = timeout_seconds
        self.next_request_id = 1
        self.notifications: deque[dict[str, Any]] = deque()

    def __enter__(self) -> "AppServerClient":
        self.connection.connect()
        try:
            self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "monitor-long-task",
                        "title": "monitor-long-task origin-session gate",
                        "version": __version__,
                    },
                    "capabilities": {
                        "experimentalApi": True,
                        "requestAttestation": False,
                        "optOutNotificationMethods": [
                            "item/agentMessage/delta",
                            "item/commandExecution/outputDelta",
                            "item/reasoning/summaryTextDelta",
                            "item/reasoning/textDelta",
                            "turn/plan/updated",
                        ],
                    },
                },
                self.timeout_seconds,
            )
            self.connection.send_json({"method": "initialized", "params": {}})
        except Exception:
            self.connection.close()
            raise
        return self

    def __exit__(self, *_args: object) -> None:
        self.connection.close()

    def request(
        self,
        method: str,
        params: dict[str, Any],
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return self._request(method, params, timeout_seconds or self.timeout_seconds)

    def _request(
        self,
        method: str,
        params: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        request_id = self.next_request_id
        self.next_request_id += 1
        self.connection.send_json({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for App Server {method} response")
            message = self.connection.receive_json(remaining)
            if message.get("id") == request_id and "method" not in message:
                error = message.get("error")
                if isinstance(error, dict):
                    raise AppServerRequestError(method, error)
                result = message.get("result")
                if not isinstance(result, dict):
                    raise AppServerProtocolError(f"{method} returned a non-object result")
                return result
            if "method" in message and "id" not in message:
                self.notifications.append(message)
                continue
            if "method" in message and "id" in message:
                # Approval and user-input requests are also delivered to the
                # interactive client. This observer must neither answer nor
                # cancel them; the originating UI remains authoritative.
                continue

    def next_notification(self, timeout_seconds: float) -> dict[str, Any]:
        if self.notifications:
            return self.notifications.popleft()
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for an App Server notification")
            message = self.connection.receive_json(remaining)
            if "method" in message and "id" not in message:
                return message
            if "method" in message and "id" in message:
                # Leave server requests for an interactive subscriber to answer.
                continue


def app_server_method_unavailable(error: AppServerRequestError) -> bool:
    message = error.message.casefold()
    return (
        error.code == -32601
        or "unknown method" in message
        or "method not found" in message
        or (error.code == -32600 and "unknown variant" in message)
        or "message queue is unavailable" in message
    )


def extract_thread_status(response: dict[str, Any]) -> str:
    thread = response.get("thread")
    status = thread.get("status") if isinstance(thread, dict) else None
    status_type = status.get("type") if isinstance(status, dict) else None
    if status_type not in {"active", "idle", "notLoaded", "systemError"}:
        raise AppServerProtocolError("thread/resume returned an invalid thread status")
    return status_type


def find_queued_origin_message(
    client: AppServerClient,
    thread_id: str,
    client_message_id: str,
) -> dict[str, Any] | None:
    cursor: str | None = None
    for _page in range(2):
        params: dict[str, Any] = {"threadId": thread_id, "limit": 100}
        if cursor is not None:
            params["cursor"] = cursor
        response = client.request("thread/queue/list", params)
        entries = response.get("data")
        if not isinstance(entries, list):
            raise AppServerProtocolError("thread/queue/list returned invalid data")
        for entry in entries:
            if isinstance(entry, dict) and entry.get("clientUserMessageId") == client_message_id:
                return entry
        next_cursor = response.get("nextCursor")
        if next_cursor is None:
            return None
        if not isinstance(next_cursor, str) or not next_cursor:
            raise AppServerProtocolError("thread/queue/list returned an invalid cursor")
        cursor = next_cursor
    raise AppServerProtocolError("thread/queue/list exceeded the documented queue capacity")


def enqueue_origin_message_if_supported(
    client: AppServerClient,
    thread_id: str,
    client_message_id: str,
    message: str,
) -> dict[str, Any] | None:
    try:
        existing = find_queued_origin_message(client, thread_id, client_message_id)
    except AppServerRequestError as exc:
        if app_server_method_unavailable(exc):
            return None
        raise
    if existing is not None:
        return {"queued_submission": existing, "already_present": True}
    try:
        response = client.request(
            "thread/queue/add",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": message}],
                "clientUserMessageId": client_message_id,
            },
        )
    except AppServerRequestError as exc:
        if app_server_method_unavailable(exc):
            return None
        raise
    except (AppServerTransportError, TimeoutError) as exc:
        raise OriginQueueOutcomeUnknown(
            "thread/queue/add response was lost; refusing to retry an uncertain delivery"
        ) from exc
    queued_submission = response.get("queuedSubmission")
    if not isinstance(queued_submission, dict) or not queued_submission.get("id"):
        raise AppServerProtocolError("thread/queue/add returned an invalid queued submission")
    return {"queued_submission": queued_submission, "already_present": False}


def wait_for_origin_delivery_route(
    config: dict[str, Any],
    message: str,
    progress: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """Queue durably when supported, otherwise wait for a real idle status event."""

    socket_path = Path(config["app_server_socket"])
    thread_id = config["thread_id"]
    deadline = time.monotonic() + config["idle_timeout_seconds"]
    connection_attempts = 0
    reconnect_index = 0
    initial_status: str | None = None
    queue_supported: bool | None = None

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OriginSessionIdleTimeout("origin Codex thread did not become idle before the deadline")
        connection_attempts += 1
        progress(
            {
                "status": "waiting_for_idle",
                "connection_attempts": connection_attempts,
                "idle_gate": "connecting",
            }
        )
        try:
            with AppServerClient(socket_path, timeout_seconds=min(15, remaining)) as client:
                response = client.request(
                    "thread/resume",
                    {"threadId": thread_id, "excludeTurns": True},
                    timeout_seconds=min(30, remaining),
                )
                status_type = extract_thread_status(response)
                if initial_status is None:
                    initial_status = status_type
                progress(
                    {
                        "status": "waiting_for_idle",
                        "idle_gate": "subscribed",
                        "last_observed_thread_status": status_type,
                        "last_observed_at": utc_now(),
                    }
                )

                queued = enqueue_origin_message_if_supported(
                    client,
                    thread_id,
                    config["client_user_message_id"],
                    message,
                )
                if queued is not None:
                    queue_supported = True
                    entry = queued["queued_submission"]
                    return {
                        "delivery": "app-server-queue",
                        "connection_attempts": connection_attempts,
                        "initial_thread_status": initial_status,
                        "last_thread_status": status_type,
                        "queued_submission_id": entry["id"],
                        "already_present": queued["already_present"],
                    }
                queue_supported = False

                if status_type in {"idle", "notLoaded"}:
                    return {
                        "delivery": "codex-exec-resume",
                        "connection_attempts": connection_attempts,
                        "initial_thread_status": initial_status,
                        "last_thread_status": status_type,
                        "queue_supported": queue_supported,
                    }
                if status_type == "systemError":
                    raise AppServerProtocolError("origin Codex thread is in systemError")

                progress(
                    {
                        "status": "waiting_for_idle",
                        "idle_gate": "waiting_on_status_event",
                        "last_observed_thread_status": "active",
                    }
                )
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise OriginSessionIdleTimeout(
                            "origin Codex thread did not become idle before the deadline"
                        )
                    notification = client.next_notification(remaining)
                    if notification.get("method") != "thread/status/changed":
                        continue
                    params = notification.get("params")
                    if not isinstance(params, dict) or params.get("threadId") != thread_id:
                        continue
                    status = params.get("status")
                    event_status = status.get("type") if isinstance(status, dict) else None
                    if event_status not in {"active", "idle", "notLoaded", "systemError"}:
                        raise AppServerProtocolError(
                            "thread/status/changed carried an invalid thread status"
                        )
                    progress(
                        {
                            "status": "waiting_for_idle",
                            "idle_gate": "waiting_on_status_event",
                            "last_observed_thread_status": event_status,
                            "last_observed_at": utc_now(),
                        }
                    )
                    if event_status in {"idle", "notLoaded"}:
                        return {
                            "delivery": "codex-exec-resume",
                            "connection_attempts": connection_attempts,
                            "initial_thread_status": initial_status,
                            "last_thread_status": event_status,
                            "queue_supported": queue_supported,
                        }
                    if event_status == "systemError":
                        raise AppServerProtocolError("origin Codex thread entered systemError")
        except OriginSessionIdleTimeout:
            raise
        except (AppServerRequestError, AppServerProtocolError):
            raise
        except (AppServerTransportError, TimeoutError) as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OriginSessionIdleTimeout(
                    "origin Codex thread did not become idle before the deadline"
                ) from exc
            delay = APP_SERVER_RECONNECT_DELAYS[
                min(reconnect_index, len(APP_SERVER_RECONNECT_DELAYS) - 1)
            ]
            reconnect_index += 1
            progress(
                {
                    "status": "waiting_for_idle",
                    "idle_gate": "reconnecting",
                    "last_connection_error": one_line(str(exc))[:500],
                    "next_reconnect_seconds": min(delay, remaining),
                }
            )
            time.sleep(min(delay, remaining))


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


def spawn_codex_resume(
    task_dir: Path,
    request: dict[str, Any],
    message: str,
) -> dict[str, Any]:
    config = request["origin_session_resume"]
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


def dispatch_origin_session(
    task_dir: Path,
    request: dict[str, Any],
    state: dict[str, Any],
    progress: Callable[[dict[str, Any]], None] | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    config = request["origin_session_resume"]
    rendered_message = message if message is not None else render_session_message(request, state)
    message_hash = hashlib.sha256(rendered_message.encode("utf-8")).hexdigest()
    if not config.get("idle_gate_enabled", False):
        result = spawn_codex_resume(task_dir, request, rendered_message)
        result["idle_gate"] = {
            "mode": "unavailable",
            "reason": "App Server Unix socket was unavailable when the monitor task was created",
        }
        return result

    publish = progress or (lambda _update: None)
    try:
        route = wait_for_origin_delivery_route(config, rendered_message, publish)
        if route["delivery"] == "app-server-queue":
            return {
                "status": "queued",
                "method": "app-server-thread-queue",
                "thread_id": config["thread_id"],
                "queued_submission_id": route["queued_submission_id"],
                "client_user_message_id": config["client_user_message_id"],
                "already_present": route["already_present"],
                "queued_at": utc_now(),
                "message_sha256": message_hash,
                "idle_gate": {
                    "mode": "durable-app-server-queue",
                    "connection_attempts": route["connection_attempts"],
                    "initial_thread_status": route["initial_thread_status"],
                    "last_thread_status": route["last_thread_status"],
                },
            }
        result = spawn_codex_resume(task_dir, request, rendered_message)
        result["idle_gate"] = {
            "mode": "app-server-status-events",
            "connection_attempts": route["connection_attempts"],
            "initial_thread_status": route["initial_thread_status"],
            "last_thread_status": route["last_thread_status"],
            "queue_supported": route["queue_supported"],
            "released_at": utc_now(),
        }
        return result
    except Exception as exc:
        return {
            "status": "failed",
            "method": "app-server-idle-gate",
            "thread_id": config["thread_id"],
            "failed_at": utc_now(),
            "error": one_line(str(exc))[:500],
            "message_sha256": message_hash,
            "log": str(task_dir / "session-resume.log"),
        }


def finalize_terminal_state(
    task_dir: Path,
    request: dict[str, Any],
    state: dict[str, Any],
) -> None:
    write_state(task_dir, state, terminal=True)
    if "origin_session_resume" not in request or "origin_session_resume" in state:
        return
    config = request["origin_session_resume"]
    message = render_session_message(request, state)
    resume_state: dict[str, Any] = {
        "status": "waiting_for_idle" if config.get("idle_gate_enabled", False) else "dispatching",
        "method": "app-server-idle-gate" if config.get("idle_gate_enabled", False) else "codex-exec-resume",
        "thread_id": config["thread_id"],
        "client_user_message_id": config.get("client_user_message_id"),
        "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
        "waiting_since": utc_now(),
    }
    state["origin_session_resume"] = resume_state
    write_state(task_dir, state, terminal=True)

    def persist_progress(update: dict[str, Any]) -> None:
        resume_state.update(update)
        state["origin_session_resume"] = resume_state
        write_state(task_dir, state, terminal=True)

    state["origin_session_resume"] = dispatch_origin_session(
        task_dir,
        request,
        state,
        progress=persist_progress,
        message=message,
    )
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


def is_unix_socket(path: Path) -> bool:
    try:
        return stat.S_ISSOCK(path.stat().st_mode)
    except OSError:
        return False


def build_origin_session_resume(args: argparse.Namespace) -> dict[str, Any] | None:
    if not args.resume_origin_session:
        if args.session_message:
            raise ValueError("--session-message requires --resume-origin-session")
        return None
    idle_timeout_seconds = int(getattr(args, "session_idle_timeout_minutes", 1440) * 60)
    if idle_timeout_seconds < 60:
        raise ValueError("origin-session idle timeout must be at least one minute")
    socket_path = Path(
        getattr(args, "app_server_socket", str(DEFAULT_APP_SERVER_SOCKET))
    ).expanduser().resolve()
    return {
        "thread_id": normalize_thread_id(args.origin_thread_id),
        "message_template": validate_session_message_template(args.session_message or ""),
        "codex_binary": resolve_codex_binary(args.codex_binary),
        "client_user_message_id": str(uuid.uuid4()),
        "app_server_socket": str(socket_path),
        "idle_gate_enabled": is_unix_socket(socket_path),
        "idle_timeout_seconds": idle_timeout_seconds,
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
        help="deliver one follow-up to the exact origin thread after it becomes idle",
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
    parser.add_argument(
        "--app-server-socket",
        default=str(DEFAULT_APP_SERVER_SOCKET),
        help="managed Codex App Server Unix socket used for event-driven idle detection",
    )
    parser.add_argument(
        "--session-idle-timeout-minutes",
        type=float,
        default=1440,
        help="maximum wait for the origin Codex thread to become idle; default: 1440",
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
