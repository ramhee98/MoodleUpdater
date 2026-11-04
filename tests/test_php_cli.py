import os
import subprocess

import pytest

from modules.moodle_backup import MoodleBackupManager


def _manager(**kwargs):
    return MoodleBackupManager("/var/www/my site", "moodle", "/tmp", **kwargs)


def test_php_command_runs_as_current_user_by_default():
    assert _manager()._php_command("admin/cli/checks.php") == ["php", "/var/www/my site/moodle/admin/cli/checks.php"]


def test_php_command_uses_runuser_when_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    cmd = _manager(php_cli_user="www-data")._php_command("admin/cli/upgrade.php", "--non-interactive")
    assert cmd[:4] == ["runuser", "-u", "www-data", "--"]
    assert cmd[4:] == ["php", "/var/www/my site/moodle/admin/cli/upgrade.php", "--non-interactive"]


def test_php_command_ignores_user_when_not_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert _manager(php_cli_user="www-data")._php_command("x.php")[0] == "php"


@pytest.mark.parametrize("enable, flag", [(True, "--enable"), (False, "--disable")])
def test_maintenance_mode_uses_argument_list(monkeypatch, enable, flag):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _manager().moodle_maintenance_mode(enable)
    assert seen["cmd"] == ["php", "/var/www/my site/moodle/admin/cli/maintenance.php", flag]
    assert not seen["kwargs"].get("shell")
