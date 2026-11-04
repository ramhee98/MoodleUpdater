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
    return MoodleBackupManager(str(tmp_path), "moodle", str(tmp_path), non_interactive=True)


def test_failed_pre_check_stops_without_prompt(monkeypatch, tmp_path):
    _failing_checks(monkeypatch)
    with pytest.raises(SystemExit):
        _manager(tmp_path).run_moodle_check(before_upgrade=True)


def test_failed_post_check_continues_without_prompt(monkeypatch, tmp_path):
    _failing_checks(monkeypatch)
    _manager(tmp_path).run_moodle_check(before_upgrade=False)


def test_force_continue_skips_stop(monkeypatch, tmp_path):
    _failing_checks(monkeypatch)
    _manager(tmp_path).run_moodle_check(before_upgrade=True, force_continue=True)
