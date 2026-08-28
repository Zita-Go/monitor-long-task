from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPOSITORY_ROOT / "scripts" / "install.sh"


class InstallScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.codex_home = Path(self.temporary.name) / "codex-home"
        self.environment = os.environ.copy()
        self.environment["CODEX_HOME"] = str(self.codex_home)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_installer(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(INSTALLER), *arguments],
            cwd=REPOSITORY_ROOT,
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_install_update_and_recoverable_uninstall(self) -> None:
        destination = self.codex_home / "skills" / "monitor-long-task"
        secret = self.codex_home / "secrets" / "feishu-long-task-webhook"
        secret.parent.mkdir(parents=True)
        secret.write_text("private-test-secret\n", encoding="utf-8")

        installed = self.run_installer()
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertTrue((destination / "SKILL.md").is_file())
        monitor = destination / "scripts" / "long_task_monitor.py"
        self.assertEqual(stat.S_IMODE(monitor.stat().st_mode), 0o755)
        version = subprocess.run(
            ["python3", str(monitor), "--version"],
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("0.2.1", version.stdout)

        duplicate = self.run_installer()
        self.assertEqual(duplicate.returncode, 2)
        self.assertIn("Refusing to overwrite", duplicate.stderr)

        marker = destination / "local-marker"
        marker.write_text("old copy\n", encoding="utf-8")
        updated = self.run_installer("--update")
        self.assertEqual(updated.returncode, 0, updated.stderr)
        self.assertFalse((destination / "local-marker").exists())
        backups = list((self.codex_home / "skills" / ".backups").glob("monitor-long-task-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "local-marker").read_text(), "old copy\n")
        self.assertEqual(secret.read_text(), "private-test-secret\n")

        uninstalled = self.run_installer("--uninstall")
        self.assertEqual(uninstalled.returncode, 0, uninstalled.stderr)
        self.assertFalse(destination.exists())
        disabled = list((self.codex_home / "skills" / ".disabled").glob("monitor-long-task-*"))
        self.assertEqual(len(disabled), 1)
        self.assertTrue((disabled[0] / "SKILL.md").is_file())
        self.assertEqual(secret.read_text(), "private-test-secret\n")

    def test_update_and_uninstall_require_an_existing_installation(self) -> None:
        update = self.run_installer("--update")
        self.assertEqual(update.returncode, 3)
        self.assertIn("not installed", update.stderr)

        uninstall = self.run_installer("--uninstall")
        self.assertEqual(uninstall.returncode, 4)
        self.assertIn("not installed", uninstall.stderr)

    def test_help_has_no_side_effects(self) -> None:
        result = self.run_installer("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--update", result.stdout)
        self.assertFalse(self.codex_home.exists())


if __name__ == "__main__":
    unittest.main()
