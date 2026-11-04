import logging
import sys

from modules.moodle_backup import MoodleBackupManager


def _state():
    return {'section': '', 'errors': []}


def test_classifies_lines(caplog):
    state = _state()
    with caplog.at_level(logging.INFO):
        for line, from_stderr in [
            ("== Upgrading plugins ==\n", False),
            ("mod_forum: done\n", False),
            ("!! Something broke !!\n", False),
            ("Cache purge failed\n", False),
            ("PHP Warning: x\n", True),
            ("   \n", False),
        ]:
            MoodleBackupManager._process_upgrade_line(line, from_stderr, state)
    assert state['section'] == "Upgrading plugins"
    assert state['errors'] == ["Upgrading plugins: !! Something broke !!", "Cache purge failed", "PHP Warning: x"]
    levels = {r.getMessage(): r.levelname for r in caplog.records}
    assert levels["!! Something broke !!"] == "ERROR"
    assert levels["mod_forum: done"] == "INFO"
    assert levels["PHP Warning: x"] == "WARNING"


def test_upgrade_failure_collects_output_from_real_process(tmp_path, monkeypatch):
    script = tmp_path / "fake_upgrade.py"
    script.write_text(
        "import sys\n"
        "print('== Upgrading core ==')\n"
        "print('!! Database error !!')\n"
        "print('boom', file=sys.stderr)\n"
        "sys.exit(3)\n"
    )
    manager = MoodleBackupManager(str(tmp_path), "moodle", str(tmp_path))
    monkeypatch.setattr(manager, "_php_command", lambda *a: [sys.executable, str(script)])
    monkeypatch.setattr(manager, "run_moodle_check", lambda **kwargs: None)
    assert manager.moodle_cli_upgrade(False, force_continue=False) is False
    assert manager.upgrade_error_details[0] == "Exit code: 3"
    assert "Upgrading core: !! Database error !!" in manager.upgrade_error_details
    assert "boom" in manager.upgrade_error_details


def test_upgrade_success(tmp_path, monkeypatch):
    script = tmp_path / "ok.py"
    script.write_text("print('== Done ==')\n")
    manager = MoodleBackupManager(str(tmp_path), "moodle", str(tmp_path))
    monkeypatch.setattr(manager, "_php_command", lambda *a: [sys.executable, str(script)])
    monkeypatch.setattr(manager, "run_moodle_check", lambda **kwargs: None)
    assert manager.moodle_cli_upgrade(False, force_continue=False) is True
