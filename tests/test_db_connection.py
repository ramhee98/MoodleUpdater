import subprocess

import pytest

import moodle_updater
from modules import moodle_backup


def test_password_is_passed_via_env_not_argv(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["env"] = cmd, kwargs["env"]
        return subprocess.CompletedProcess(cmd, 1, "", "ERROR 1045: Access denied (using password: s3cret)")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, error = moodle_backup.check_db_connection("moodle", "user", "s3cret")
    assert ok is False
    assert not any("s3cret" in part for part in seen["cmd"])
    assert seen["env"]["MYSQL_PWD"] == "s3cret"
    assert "s3cret" not in error


def _results(monkeypatch, outcomes):
    """Make check_db_connection return the given outcomes in order, recording passwords."""
    passwords = []

    def fake_check(dbname, dbuser, dbpass, dbhost=None):
        passwords.append(dbpass)
        return outcomes.pop(0)

    monkeypatch.setattr(moodle_updater, "check_db_connection", fake_check)
    return passwords


def test_retry_uses_newly_entered_password(monkeypatch):
    passwords = _results(monkeypatch, [(False, "denied"), (True, "")])
    monkeypatch.setattr(moodle_updater, "prompt_db_password", lambda prompt: "right")
    assert moodle_updater.ensure_db_connection("db", "u", "wrong", non_interactive=False) == "right"
    assert passwords == ["wrong", "right"]


def test_non_interactive_failure_exits_without_prompt(monkeypatch):
    _results(monkeypatch, [(False, "denied")])
    monkeypatch.setattr(moodle_updater, "prompt_db_password", lambda prompt: pytest.fail("must not prompt"))
    with pytest.raises(SystemExit):
        moodle_updater.ensure_db_connection("db", "u", "wrong", non_interactive=True)


def test_gives_up_after_max_attempts(monkeypatch):
    passwords = _results(monkeypatch, [(False, "denied")] * 3)
    monkeypatch.setattr(moodle_updater, "prompt_db_password", lambda prompt: "still-wrong")
    with pytest.raises(SystemExit):
        moodle_updater.ensure_db_connection("db", "u", "wrong", non_interactive=False)
    assert len(passwords) == 3


def test_db_host_is_passed_to_client(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert moodle_backup.check_db_connection("moodle", "user", "pw", "db.example.org")[0] is True
    assert seen["cmd"][-3:] == ["-h", "db.example.org", "moodle"]
    moodle_backup.check_db_connection("moodle", "user", "pw", "")
    assert "-h" not in seen["cmd"]
