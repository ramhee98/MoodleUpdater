import logging

from modules.moodle_backup import MoodleBackupManager
from modules.steps import run_step, run_steps_concurrently


def test_run_step_success_and_failure():
    assert run_step("ok", lambda: True) is True
    assert run_step("bad", lambda: False) is False
    assert run_step("none", lambda: None) is False


def test_run_step_logs_exceptions(caplog):
    def boom():
        raise ValueError("kaput")

    with caplog.at_level(logging.ERROR):
        assert run_step("Boom", boom) is False
    assert "Boom failed with an unexpected error" in caplog.text
    assert "kaput" in caplog.text


def test_thread_exceptions_reach_the_caller(caplog):
    def boom():
        raise RuntimeError("thread crash")

    with caplog.at_level(logging.ERROR):
        results = run_steps_concurrently([("good", lambda: True), ("crashing", boom)])
    assert results == {"good": True, "crashing": False}
    assert "thread crash" in caplog.text


def test_clone_skipped_when_backup_fails(tmp_path, monkeypatch):
    manager = MoodleBackupManager(str(tmp_path), "moodle", str(tmp_path))
    monkeypatch.setattr(manager, "dir_backup", lambda full_backup: False)
    monkeypatch.setattr(manager, "git_clone", lambda *a, **k: (_ for _ in ()).throw(AssertionError("clone must not run")))
    assert manager.dir_backup_and_git_clone("cfg", False, "repo", "branch", True, "u", "g") is False


def test_git_clone_aborts_when_clone_fails(tmp_path, monkeypatch):
    import subprocess

    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        if cmd[:2] == ["git", "clone"]:
            raise subprocess.CalledProcessError(128, cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manager = MoodleBackupManager(str(tmp_path), "moodle", str(tmp_path))
    assert manager.git_clone("<?php", "repo", "branch", False, "u", "g") is False
    assert not any(cmd[0] == "chown" for cmd in calls)
    assert not (tmp_path / "moodle" / "config.php").exists()
