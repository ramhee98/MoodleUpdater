import subprocess

from modules.moodle_backup import MoodleBackupManager


def test_maintenance_mode_disabled_when_upgrade_raises(tmp_path, monkeypatch):
    manager = MoodleBackupManager(str(tmp_path), "moodle", str(tmp_path))
    toggles = []
    monkeypatch.setattr(manager, "moodle_maintenance_mode", lambda enable: toggles.append(enable) or True)
    monkeypatch.setattr(manager, "run_moodle_check", lambda **kwargs: True)

    def broken_popen(*args, **kwargs):
        raise OSError("php not found")

    monkeypatch.setattr(subprocess, "Popen", broken_popen)

    assert manager.moodle_cli_upgrade(True, force_continue=False) is False
    assert toggles == [True, False]
    assert manager.upgrade_failed


def test_maintenance_mode_untouched_when_not_requested(tmp_path, monkeypatch):
    manager = MoodleBackupManager(str(tmp_path), "moodle", str(tmp_path))
    toggles = []
    monkeypatch.setattr(manager, "moodle_maintenance_mode", lambda enable: toggles.append(enable) or True)
    monkeypatch.setattr(manager, "run_moodle_check", lambda **kwargs: True)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))

    manager.moodle_cli_upgrade(False, force_continue=False)
    assert toggles == []
