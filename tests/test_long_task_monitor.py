from __future__ import annotations

import importlib.util
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY_ROOT / "skills" / "monitor-long-task"
SCRIPT_PATH = SKILL_ROOT / "scripts" / "long_task_monitor.py"
MODULE_SPEC = importlib.util.spec_from_file_location("long_task_monitor", SCRIPT_PATH)
if MODULE_SPEC is None or MODULE_SPEC.loader is None:
    raise RuntimeError(f"cannot import monitor script: {SCRIPT_PATH}")
monitor = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(monitor)


FAKE_WEBHOOK = (
    "https://open.feishu.cn/open-apis/bot/v2/hook/"
    "example-test-webhook"
)


class FakeResponse:
    status = 200

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return b'{"StatusCode":0,"StatusMessage":"success","code":0,"msg":"success"}'


class FakeOpener:
    def __init__(self) -> None:
        self.request = None
        self.timeout = None

    def open(self, request: object, timeout: int) -> FakeResponse:
        self.request = request
        self.timeout = timeout
        return FakeResponse()


class FakeStdin:
    def __init__(self) -> None:
        self.value = ""
        self.closed = False

    def write(self, value: str) -> int:
        self.value += value
        return len(value)

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.stdin = FakeStdin()


class LongTaskMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.webhook_file = self.root / "secrets" / "webhook"
        monitor.save_webhook_url(self.webhook_file, FAKE_WEBHOOK)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(
        self,
        task_id: str,
        mode: str,
        command: list[str],
        timeout: float = 1,
    ) -> dict[str, object]:
        request: dict[str, object] = {
            "version": 1,
            "task_id": task_id,
            "mode": mode,
            "project": "demo",
            "summary": "测试任务",
            "created_at": monitor.utc_now(),
            "cwd": str(self.root),
            "timeout_seconds": timeout,
            "command": command,
            "webhook_file": str(self.webhook_file),
        }
        if mode == "watch":
            request["interval_seconds"] = 0.01
            request["check_timeout_seconds"] = 0.1
        return request

    def test_message_normalization(self) -> None:
        self.assertEqual(monitor.normalize_project("【demo】："), "demo")
        self.assertEqual(monitor.normalize_summary("完成： 全量测试"), "全量测试")
        request = {"project": "demo", "summary": "全量测试"}
        self.assertEqual(monitor.task_message(request), "【demo】：完成全量测试")

    def test_webhook_file_is_private_and_not_overwritten(self) -> None:
        self.assertEqual(stat.S_IMODE(self.webhook_file.stat().st_mode), 0o600)
        self.assertEqual(monitor.load_webhook_url(self.webhook_file), FAKE_WEBHOOK)
        with self.assertRaises(FileExistsError):
            monitor.save_webhook_url(self.webhook_file, FAKE_WEBHOOK)

        lark_webhook = "https://open.larksuite.com/open-apis/bot/v2/hook/example"
        monitor.save_webhook_url(self.webhook_file, lark_webhook, force=True)
        self.assertEqual(monitor.load_webhook_url(self.webhook_file), lark_webhook)

    def test_send_feishu_validates_success_response(self) -> None:
        opener = FakeOpener()
        with mock.patch.object(monitor.urllib.request, "build_opener", return_value=opener):
            result = monitor.send_feishu(self.webhook_file, "【demo】：完成测试任务")

        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(opener.timeout, 15)
        payload = json.loads(opener.request.data.decode("utf-8"))
        self.assertEqual(payload["content"]["text"], "【demo】：完成测试任务")

    def test_managed_command_notifies_only_after_zero_exit(self) -> None:
        successful_dir = self.root / "successful"
        successful_dir.mkdir()
        successful_request = self.request(
            "successful",
            "launch",
            [sys.executable, "-c", "print('ok')"],
        )
        sent = {
            "status": "sent",
            "attempts": 1,
            "http_status": 200,
            "sent_at": monitor.utc_now(),
        }
        with mock.patch.object(monitor, "send_feishu", return_value=sent) as notifier:
            self.assertEqual(monitor.run_managed_command(successful_dir, successful_request), 0)
        successful_state = json.loads((successful_dir / "summary.json").read_text())
        self.assertEqual(successful_state["status"], "completed")
        notifier.assert_called_once()

        failed_dir = self.root / "failed"
        failed_dir.mkdir()
        failed_request = self.request(
            "failed",
            "launch",
            [sys.executable, "-c", "raise SystemExit(7)"],
        )
        with mock.patch.object(monitor, "send_feishu") as notifier:
            self.assertEqual(monitor.run_managed_command(failed_dir, failed_request), 7)
        failed_state = json.loads((failed_dir / "summary.json").read_text())
        self.assertEqual(failed_state["status"], "failed")
        self.assertEqual(failed_state["exit_code"], 7)
        notifier.assert_not_called()

    def test_notification_failure_preserves_task_success(self) -> None:
        task_dir = self.root / "notification-failed"
        task_dir.mkdir()
        request = self.request("notification-failed", "launch", [sys.executable, "-c", "pass"])
        state = monitor.initial_state(task_dir, request, "running")
        with mock.patch.object(monitor, "send_feishu", side_effect=OSError("offline")):
            monitor.complete_and_notify(task_dir, request, state)
        final_state = json.loads((task_dir / "summary.json").read_text())
        self.assertEqual(final_state["status"], "completed_notification_failed")
        self.assertEqual(final_state["notification"]["status"], "failed")

    def test_watch_success_and_timeout(self) -> None:
        success_dir = self.root / "watch-success"
        success_dir.mkdir()
        success_request = self.request(
            "watch-success",
            "watch",
            [sys.executable, "-c", "raise SystemExit(0)"],
        )
        sent = {
            "status": "sent",
            "attempts": 1,
            "http_status": 200,
            "sent_at": monitor.utc_now(),
        }
        with mock.patch.object(monitor, "send_feishu", return_value=sent):
            self.assertEqual(monitor.watch_completion(success_dir, success_request), 0)
        success_state = json.loads((success_dir / "summary.json").read_text())
        self.assertEqual(success_state["status"], "completed")
        self.assertEqual(success_state["checks"], 1)

        timeout_dir = self.root / "watch-timeout"
        timeout_dir.mkdir()
        timeout_request = self.request(
            "watch-timeout",
            "watch",
            [sys.executable, "-c", "raise SystemExit(1)"],
            timeout=0.06,
        )
        with mock.patch.object(monitor, "send_feishu") as notifier:
            self.assertEqual(monitor.watch_completion(timeout_dir, timeout_request), 0)
        timeout_state = json.loads((timeout_dir / "summary.json").read_text())
        self.assertEqual(timeout_state["status"], "timed_out")
        self.assertGreaterEqual(timeout_state["checks"], 1)
        notifier.assert_not_called()

    def test_configure_cli_reads_webhook_from_stdin(self) -> None:
        codex_home = self.root / "codex-home"
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(codex_home)
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "configure"],
            input=FAKE_WEBHOOK + "\n",
            text=True,
            capture_output=True,
            check=True,
            env=environment,
        )
        payload = json.loads(result.stdout)
        configured = codex_home / "secrets" / "feishu-long-task-webhook"
        self.assertTrue(payload["ok"])
        self.assertEqual(configured.read_text().strip(), FAKE_WEBHOOK)
        self.assertEqual(stat.S_IMODE(configured.stat().st_mode), 0o600)

    def test_origin_session_resume_requires_exact_thread_and_agent_message(self) -> None:
        thread_id = "01a042ae-2b44-7a00-9d98-60cb16f9e5d4"
        arguments = SimpleNamespace(
            resume_origin_session=True,
            origin_thread_id=thread_id,
            session_message="任务进入 {status}，请读取 {summary_file} 后自行继续。",
            codex_binary=sys.executable,
        )
        config = monitor.build_origin_session_resume(arguments)
        self.assertEqual(config["thread_id"], thread_id)
        self.assertEqual(config["codex_binary"], str(Path(sys.executable).resolve()))

        arguments.origin_thread_id = "not-a-thread-id"
        with self.assertRaisesRegex(ValueError, "must be a UUID"):
            monitor.build_origin_session_resume(arguments)

        arguments.origin_thread_id = thread_id
        arguments.session_message = "未知字段 {unknown}"
        with self.assertRaisesRegex(ValueError, "unknown session message field"):
            monitor.build_origin_session_resume(arguments)

    def test_origin_session_config_enables_idle_gate_for_unix_socket(self) -> None:
        thread_id = "01a042ae-2b44-7a00-9d98-60cb16f9e5d4"
        socket_path = self.root / "app-server.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as app_server_socket:
            app_server_socket.bind(str(socket_path))
            arguments = SimpleNamespace(
                resume_origin_session=True,
                origin_thread_id=thread_id,
                session_message="任务进入 {status}，请读取 {summary_file} 后自行继续。",
                codex_binary=sys.executable,
                app_server_socket=str(socket_path),
                session_idle_timeout_minutes=12,
            )
            config = monitor.build_origin_session_resume(arguments)

        self.assertTrue(config["idle_gate_enabled"])
        self.assertEqual(config["app_server_socket"], str(socket_path))
        self.assertEqual(config["idle_timeout_seconds"], 720)
        self.assertEqual(str(monitor.uuid.UUID(config["client_user_message_id"])), config["client_user_message_id"])

    def test_idle_gate_waits_for_matching_status_event_without_polling(self) -> None:
        thread_id = "01a042ae-2b44-7a00-9d98-60cb16f9e5d4"
        requests: list[str] = []

        class FakeClient:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                self.notifications = iter(
                    [
                        {
                            "method": "thread/status/changed",
                            "params": {
                                "threadId": "019fefe7-c884-7971-bbe2-5c08ede61683",
                                "status": {"type": "idle"},
                            },
                        },
                        {
                            "method": "thread/status/changed",
                            "params": {
                                "threadId": thread_id,
                                "status": {"type": "active", "activeFlags": []},
                            },
                        },
                        {
                            "method": "thread/status/changed",
                            "params": {
                                "threadId": thread_id,
                                "status": {"type": "idle"},
                            },
                        },
                    ]
                )

            def __enter__(self) -> "FakeClient":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def request(
                self,
                method: str,
                _params: dict[str, object],
                timeout_seconds: float | None = None,
            ) -> dict[str, object]:
                del timeout_seconds
                requests.append(method)
                if method == "thread/resume":
                    return {"thread": {"status": {"type": "active", "activeFlags": []}}}
                if method == "thread/queue/list":
                    raise monitor.AppServerRequestError(
                        method,
                        {"code": -32600, "message": "unknown variant `thread/queue/list`"},
                    )
                raise AssertionError(f"unexpected request: {method}")

            def next_notification(self, _timeout_seconds: float) -> dict[str, object]:
                return next(self.notifications)

        progress: list[dict[str, object]] = []
        config = {
            "app_server_socket": str(self.root / "app-server.sock"),
            "thread_id": thread_id,
            "client_user_message_id": str(monitor.uuid.uuid4()),
            "idle_timeout_seconds": 60,
        }
        with mock.patch.object(monitor, "AppServerClient", FakeClient):
            route = monitor.wait_for_origin_delivery_route(config, "继续处理", progress.append)

        self.assertEqual(route["delivery"], "codex-exec-resume")
        self.assertEqual(route["initial_thread_status"], "active")
        self.assertEqual(route["last_thread_status"], "idle")
        self.assertEqual(requests, ["thread/resume", "thread/queue/list"])
        observed = [update.get("last_observed_thread_status") for update in progress]
        self.assertEqual(observed, [None, "active", "active", "active", "idle"])

    def test_idle_gate_treats_open_idle_thread_as_ready(self) -> None:
        thread_id = "01a042ae-2b44-7a00-9d98-60cb16f9e5d4"

        class IdleClient:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def __enter__(self) -> "IdleClient":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def request(
                self,
                method: str,
                _params: dict[str, object],
                timeout_seconds: float | None = None,
            ) -> dict[str, object]:
                del timeout_seconds
                if method == "thread/resume":
                    return {"thread": {"status": {"type": "idle"}}}
                if method == "thread/queue/list":
                    raise monitor.AppServerRequestError(
                        method,
                        {"code": -32601, "message": "Method not found"},
                    )
                raise AssertionError(f"unexpected request: {method}")

            def next_notification(self, _timeout_seconds: float) -> dict[str, object]:
                raise AssertionError("an idle thread must not wait for another event")

        config = {
            "app_server_socket": str(self.root / "app-server.sock"),
            "thread_id": thread_id,
            "client_user_message_id": str(monitor.uuid.uuid4()),
            "idle_timeout_seconds": 60,
        }
        with mock.patch.object(monitor, "AppServerClient", IdleClient):
            route = monitor.wait_for_origin_delivery_route(config, "继续处理", lambda _update: None)

        self.assertEqual(route["delivery"], "codex-exec-resume")
        self.assertEqual(route["initial_thread_status"], "idle")
        self.assertEqual(route["last_thread_status"], "idle")

    def test_idle_gate_prefers_durable_app_server_queue(self) -> None:
        thread_id = "01a042ae-2b44-7a00-9d98-60cb16f9e5d4"
        client_message_id = str(monitor.uuid.uuid4())
        requests: list[tuple[str, dict[str, object]]] = []

        class QueueClient:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def __enter__(self) -> "QueueClient":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def request(
                self,
                method: str,
                params: dict[str, object],
                timeout_seconds: float | None = None,
            ) -> dict[str, object]:
                del timeout_seconds
                requests.append((method, params))
                if method == "thread/resume":
                    return {"thread": {"status": {"type": "active", "activeFlags": []}}}
                if method == "thread/queue/list":
                    return {"data": [], "nextCursor": None}
                if method == "thread/queue/add":
                    return {
                        "queuedSubmission": {
                            "id": "queued-1",
                            "input": params["input"],
                            "clientUserMessageId": params["clientUserMessageId"],
                        }
                    }
                raise AssertionError(f"unexpected request: {method}")

            def next_notification(self, _timeout_seconds: float) -> dict[str, object]:
                raise AssertionError("durable queue acceptance must finish the waiter")

        config = {
            "app_server_socket": str(self.root / "app-server.sock"),
            "thread_id": thread_id,
            "client_user_message_id": client_message_id,
            "idle_timeout_seconds": 60,
        }
        with mock.patch.object(monitor, "AppServerClient", QueueClient):
            route = monitor.wait_for_origin_delivery_route(config, "由 Agent 决定的消息", lambda _update: None)

        self.assertEqual(route["delivery"], "app-server-queue")
        self.assertEqual(route["queued_submission_id"], "queued-1")
        self.assertFalse(route["already_present"])
        self.assertEqual([method for method, _params in requests], [
            "thread/resume",
            "thread/queue/list",
            "thread/queue/add",
        ])
        self.assertEqual(requests[-1][1]["clientUserMessageId"], client_message_id)
        self.assertEqual(requests[-1][1]["input"], [{"type": "text", "text": "由 Agent 决定的消息"}])

    def test_queue_add_with_unknown_outcome_is_not_retried(self) -> None:
        class UncertainQueueClient:
            def request(
                self,
                method: str,
                _params: dict[str, object],
                timeout_seconds: float | None = None,
            ) -> dict[str, object]:
                del timeout_seconds
                if method == "thread/queue/list":
                    return {"data": [], "nextCursor": None}
                if method == "thread/queue/add":
                    raise monitor.AppServerTransportError("response lost")
                raise AssertionError(f"unexpected request: {method}")

        with self.assertRaisesRegex(
            monitor.OriginQueueOutcomeUnknown,
            "refusing to retry",
        ):
            monitor.enqueue_origin_message_if_supported(
                UncertainQueueClient(),
                "01a042ae-2b44-7a00-9d98-60cb16f9e5d4",
                str(monitor.uuid.uuid4()),
                "继续处理",
            )

    def test_idle_gate_reconnects_only_after_transport_failure(self) -> None:
        thread_id = "01a042ae-2b44-7a00-9d98-60cb16f9e5d4"

        class ReconnectingClient:
            created = 0

            def __init__(self, *_args: object, **_kwargs: object) -> None:
                self.number = ReconnectingClient.created
                ReconnectingClient.created += 1

            def __enter__(self) -> "ReconnectingClient":
                if self.number == 0:
                    raise monitor.AppServerTransportError("daemon restarted")
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def request(
                self,
                method: str,
                _params: dict[str, object],
                timeout_seconds: float | None = None,
            ) -> dict[str, object]:
                del timeout_seconds
                if method == "thread/resume":
                    return {"thread": {"status": {"type": "idle"}}}
                if method == "thread/queue/list":
                    raise monitor.AppServerRequestError(
                        method,
                        {"code": -32601, "message": "Method not found"},
                    )
                raise AssertionError(f"unexpected request: {method}")

        progress: list[dict[str, object]] = []
        config = {
            "app_server_socket": str(self.root / "app-server.sock"),
            "thread_id": thread_id,
            "client_user_message_id": str(monitor.uuid.uuid4()),
            "idle_timeout_seconds": 60,
        }
        with (
            mock.patch.object(monitor, "AppServerClient", ReconnectingClient),
            mock.patch.object(monitor.time, "sleep") as sleep,
        ):
            route = monitor.wait_for_origin_delivery_route(config, "继续处理", progress.append)

        self.assertEqual(route["connection_attempts"], 2)
        sleep.assert_called_once_with(1)
        self.assertIn("reconnecting", [update.get("idle_gate") for update in progress])

    def test_websocket_upgrade_accept_key_is_validated(self) -> None:
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        valid = (
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
        )
        monitor.UnixWebSocketConnection._validate_upgrade_response(valid, key)
        with self.assertRaisesRegex(monitor.AppServerProtocolError, "invalid WebSocket accept key"):
            monitor.UnixWebSocketConnection._validate_upgrade_response(
                valid.replace(b"s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", b"invalid"),
                key,
            )

    def test_status_observer_does_not_answer_interactive_server_requests(self) -> None:
        messages = iter(
            [
                {
                    "id": 77,
                    "method": "item/tool/requestUserInput",
                    "params": {"threadId": "origin"},
                },
                {
                    "method": "thread/status/changed",
                    "params": {"threadId": "origin", "status": {"type": "idle"}},
                },
            ]
        )

        class FakeConnection:
            def receive_json(self, _timeout_seconds: float) -> dict[str, object]:
                return next(messages)

        client = monitor.AppServerClient(self.root / "unused.sock")
        client.connection = FakeConnection()
        notification = client.next_notification(10)
        self.assertEqual(notification["method"], "thread/status/changed")

    def test_origin_session_resume_dispatches_once_without_polling(self) -> None:
        task_dir = self.root / "origin-session"
        task_dir.mkdir()
        thread_id = "01a042ae-2b44-7a00-9d98-60cb16f9e5d4"
        request = self.request("origin-session", "launch", [sys.executable, "-c", "pass"])
        request["origin_session_resume"] = {
            "thread_id": thread_id,
            "message_template": (
                "任务 {task_id} 已进入 {status}。请读取 {summary_file}，"
                "由你基于真实结果决定下一步。"
            ),
            "codex_binary": sys.executable,
        }
        state = monitor.initial_state(task_dir, request, "completed")
        state["finished_at"] = monitor.utc_now()
        fake_process = FakeProcess()

        with mock.patch.object(monitor.subprocess, "Popen", return_value=fake_process) as popen:
            monitor.finalize_terminal_state(task_dir, request, state)
            monitor.finalize_terminal_state(task_dir, request, state)

        popen.assert_called_once()
        positional, keyword = popen.call_args
        self.assertEqual(
            positional[0],
            [sys.executable, "exec", "resume", thread_id, "-"],
        )
        self.assertTrue(keyword["start_new_session"])
        self.assertEqual(keyword["cwd"], str(self.root))
        self.assertEqual(
            fake_process.stdin.value,
            (
                "任务 origin-session 已进入 completed。"
                f"请读取 {task_dir / 'summary.json'}，由你基于真实结果决定下一步。\n"
            ),
        )
        self.assertTrue(fake_process.stdin.closed)
        final_state = json.loads((task_dir / "summary.json").read_text())
        resume = final_state["origin_session_resume"]
        self.assertEqual(resume["status"], "dispatched")
        self.assertEqual(resume["thread_id"], thread_id)
        self.assertEqual(resume["pid"], 4242)
        self.assertEqual(resume["idle_gate"]["mode"], "unavailable")
        self.assertEqual(stat.S_IMODE((task_dir / "session-resume.log").stat().st_mode), 0o600)

    def test_origin_session_queue_persists_waiting_state_and_does_not_spawn_cli(self) -> None:
        task_dir = self.root / "origin-session-queue"
        task_dir.mkdir()
        thread_id = "01a042ae-2b44-7a00-9d98-60cb16f9e5d4"
        client_message_id = str(monitor.uuid.uuid4())
        request = self.request("origin-session-queue", "launch", [sys.executable, "-c", "pass"])
        request["origin_session_resume"] = {
            "thread_id": thread_id,
            "message_template": "任务进入 {status}，请读取 {summary_file}。",
            "codex_binary": sys.executable,
            "client_user_message_id": client_message_id,
            "app_server_socket": str(self.root / "app-server.sock"),
            "idle_gate_enabled": True,
            "idle_timeout_seconds": 60,
        }
        state = monitor.initial_state(task_dir, request, "failed")
        state["exit_code"] = 3
        waiting_snapshots: list[dict[str, object]] = []

        def fake_route(
            _config: dict[str, object],
            _message: str,
            progress: object,
        ) -> dict[str, object]:
            progress(
                {
                    "status": "waiting_for_idle",
                    "idle_gate": "waiting_on_status_event",
                    "last_observed_thread_status": "active",
                }
            )
            waiting_snapshots.append(json.loads((task_dir / "summary.json").read_text()))
            return {
                "delivery": "app-server-queue",
                "connection_attempts": 1,
                "initial_thread_status": "active",
                "last_thread_status": "active",
                "queued_submission_id": "queued-1",
                "already_present": False,
            }

        with (
            mock.patch.object(monitor, "wait_for_origin_delivery_route", side_effect=fake_route) as waiter,
            mock.patch.object(monitor.subprocess, "Popen") as popen,
        ):
            monitor.finalize_terminal_state(task_dir, request, state)
            monitor.finalize_terminal_state(task_dir, request, state)

        waiter.assert_called_once()
        popen.assert_not_called()
        self.assertEqual(
            waiting_snapshots[0]["origin_session_resume"]["last_observed_thread_status"],
            "active",
        )
        final_state = json.loads((task_dir / "summary.json").read_text())
        resume = final_state["origin_session_resume"]
        self.assertEqual(resume["status"], "queued")
        self.assertEqual(resume["method"], "app-server-thread-queue")
        self.assertEqual(resume["client_user_message_id"], client_message_id)
        self.assertEqual(resume["queued_submission_id"], "queued-1")

    def test_origin_session_resume_passes_agent_message_over_stdin(self) -> None:
        task_dir = self.root / "origin-session-subprocess"
        task_dir.mkdir()
        record_path = self.root / "resume-record.json"
        fake_codex = self.root / "fake-codex"
        fake_codex.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "pathlib.Path(os.environ['FAKE_CODEX_RECORD']).write_text(\n"
            "    json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read()}),\n"
            "    encoding='utf-8',\n"
            ")\n",
            encoding="utf-8",
        )
        fake_codex.chmod(0o700)
        thread_id = "01a042ae-2b44-7a00-9d98-60cb16f9e5d4"
        request = self.request(
            "origin-session-subprocess",
            "launch",
            [sys.executable, "-c", "pass"],
        )
        request["origin_session_resume"] = {
            "thread_id": thread_id,
            "message_template": "由 Agent 决定的消息：{status} / {summary_file}",
            "codex_binary": str(fake_codex),
        }
        state = monitor.initial_state(task_dir, request, "failed")
        state["exit_code"] = 9
        expected_message = monitor.render_session_message(request, state) + "\n"
        real_popen = subprocess.Popen
        processes: list[subprocess.Popen[str]] = []

        def retained_popen(*args: object, **kwargs: object) -> subprocess.Popen[str]:
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        with (
            mock.patch.dict(os.environ, {"FAKE_CODEX_RECORD": str(record_path)}),
            mock.patch.object(monitor.subprocess, "Popen", side_effect=retained_popen),
        ):
            result = monitor.dispatch_origin_session(task_dir, request, state)
            return_code = processes[0].wait(timeout=5)

        self.assertEqual(processes[0].pid, result["pid"])
        self.assertEqual(return_code, 0)
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["argv"], ["exec", "resume", thread_id, "-"])
        self.assertEqual(record["stdin"], expected_message)

    def test_public_skill_metadata_is_portable(self) -> None:
        skill_text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        metadata_text = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertTrue(skill_text.startswith("---\nname: monitor-long-task\n"))
        self.assertIn("description:", skill_text.split("---", 2)[1])
        self.assertNotIn("/" + "root" + "/", skill_text)
        self.assertIn("$monitor-long-task", metadata_text)


if __name__ == "__main__":
    unittest.main()
