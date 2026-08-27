from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
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

    def test_public_skill_metadata_is_portable(self) -> None:
        skill_text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        metadata_text = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertTrue(skill_text.startswith("---\nname: monitor-long-task\n"))
        self.assertIn("description:", skill_text.split("---", 2)[1])
        self.assertNotIn("/" + "root" + "/", skill_text)
        self.assertIn("$monitor-long-task", metadata_text)


if __name__ == "__main__":
    unittest.main()
