import builtins
import subprocess

import pytest

from modules.application_setup import ApplicationSetup
from modules.moodle_backup import MoodleBackupManager


def _closed_stdin(*_args, **_kwargs):
    raise EOFError


@pytest.mark.parametrize("default, expected", [("y", True), ("n", False), ("", None)])
def test_confirm_on_closed_stdin_returns_default(monkeypatch, default, expected):
    monkeypatch.setattr(builtins, "input", _closed_stdin)
    assert ApplicationSetup.confirm("Continue?", default) is expected


def test_confirm_cancel_exits(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda *_: "c")
    with pytest.raises(SystemExit):
        ApplicationSetup.confirm("Continue?", "y")


def _failing_checks(monkeypatch):
    result = subprocess.CompletedProcess(args=[], returncode=2, stdout="CRITICAL: something broke", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: result)
    monkeypatch.setattr(builtins, "input", lambda *_: pytest.fail("must not prompt in non-interactive mode"))


def _manager(tmp_path):
    checks = tmp_path / "moodle" / "admin" / "cli" / "checks.php"
    checks.parent.mkdir(parents=True)
    checks.write_text("<?php")
    return MoodleBackupManager(str(tmp_path), "moodle", str(tmp_path), non_interactive=True)


def test_failed_pre_check_stops_without_prompt(monkeypatch, tmp_path):
    _failing_checks(monkeypatch)
    assert _manager(tmp_path).run_moodle_check(before_upgrade=True) is False


def test_failed_post_check_is_reported_without_prompt(monkeypatch, tmp_path):
    _failing_checks(monkeypatch)
    assert _manager(tmp_path).run_moodle_check(before_upgrade=False) is False


def test_force_continue_skips_stop(monkeypatch, tmp_path):
    _failing_checks(monkeypatch)
    assert _manager(tmp_path).run_moodle_check(before_upgrade=True, force_continue=True) is True


def test_missing_checks_script_is_an_error(tmp_path):
    manager = MoodleBackupManager(str(tmp_path), "moodle", str(tmp_path), non_interactive=True)
    assert manager.run_moodle_check(before_upgrade=True) is False
