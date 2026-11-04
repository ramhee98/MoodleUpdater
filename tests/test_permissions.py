import os
import stat
import subprocess

import moodle_updater
from modules.moodle_backup import MoodleBackupManager


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_dump_file_is_private(tmp_path, monkeypatch):
    old_umask = os.umask(0o022)
    try:
        def fake_run(cmd, stdout=None, **kwargs):
            stdout.write("-- dump\n")
            return subprocess.CompletedProcess(cmd, 0, None, "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr("modules.moodle_backup.SystemMonitor.start_monitoring", lambda *a: None)
        monkeypatch.setattr("modules.moodle_backup.SystemMonitor.stop_monitoring", lambda *a: None)
        manager = MoodleBackupManager(str(tmp_path), "moodle", str(tmp_path))
        assert manager.db_dump("db", "u", "p", False, str(tmp_path)) is True
    finally:
        os.umask(old_umask)
    dumps = list(tmp_path.glob("db_*.sql"))
    assert len(dumps) == 1
    assert _mode(dumps[0]) == 0o600


def test_backup_folder_is_private(tmp_path):
    source = tmp_path / "www" / "moodle"
    source.mkdir(parents=True)
    (source / "config.php").write_text("<?php $CFG->dbpass = 'x';")
    os.chmod(source, 0o755)
    backups = tmp_path / "backups"
    backups.mkdir()
    manager = MoodleBackupManager(str(tmp_path / "www"), "moodle", str(backups))
    assert manager.dir_backup(full_backup=False) is True
    assert _mode(manager.backup_folder) == 0o700
    assert (backups / os.path.basename(manager.backup_folder) / "config.php").exists()


def test_lock_file_is_private_and_exclusive(tmp_path):
    lock_dir = tmp_path / "locks"
    instance = tmp_path / "www" / "moodle"
    lock = moodle_updater.acquire_instance_lock(str(instance), str(lock_dir))
    (lock_path,) = lock_dir.iterdir()
    assert _mode(lock_dir) == 0o700
    assert _mode(lock_path) == 0o600
    assert lock_path.read_text().split() == [str(os.getpid()), str(instance)]

    # A second run in another process must be refused and must not wipe the PID.
    code = (
        "import sys; sys.path.insert(0, %r); import moodle_updater; "
        "moodle_updater.acquire_instance_lock(%r, %r)"
    ) % (os.path.dirname(os.path.dirname(os.path.abspath(__file__))), str(instance), str(lock_dir))
    result = subprocess.run([os.sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 1
    assert f"PID {os.getpid()}" in result.stderr
    assert lock_path.read_text().split()[0] == str(os.getpid())
    lock.close()


def test_lock_can_be_reacquired_after_release(tmp_path):
    moodle_updater.acquire_instance_lock(str(tmp_path / "moodle"), str(tmp_path / "locks")).close()
    moodle_updater.acquire_instance_lock(str(tmp_path / "moodle"), str(tmp_path / "locks")).close()


def test_backup_keeps_symlinks_and_modes(tmp_path):
    source = tmp_path / "www" / "moodle"
    source.mkdir(parents=True)
    script = source / "run.sh"
    script.write_text("#!/bin/sh\n")
    os.chmod(script, 0o750)
    os.symlink("run.sh", source / "link.sh")
    os.symlink("does-not-exist", source / "dangling")
    backups = tmp_path / "backups"
    backups.mkdir()

    manager = MoodleBackupManager(str(tmp_path / "www"), "moodle", str(backups))
    assert manager.dir_backup(full_backup=False) is True
    copy = backups / os.path.basename(manager.backup_folder)
    assert os.readlink(copy / "link.sh") == "run.sh"
    assert os.path.islink(copy / "dangling")
    assert _mode(copy / "run.sh") == 0o750


def test_failed_dump_leaves_no_partial_file(tmp_path, monkeypatch):
    def failing_run(cmd, stdout=None, **kwargs):
        stdout.write("-- partial dump\n")
        raise subprocess.CalledProcessError(2, cmd, None, "Lost connection")

    monkeypatch.setattr(subprocess, "run", failing_run)
    monkeypatch.setattr("modules.moodle_backup.SystemMonitor.start_monitoring", lambda *a: None)
    monkeypatch.setattr("modules.moodle_backup.SystemMonitor.stop_monitoring", lambda *a: None)
    dump_dir = tmp_path / "new" / "dumps"  # does not exist yet
    manager = MoodleBackupManager(str(tmp_path), "moodle", str(tmp_path))
    assert manager.db_dump("db", "u", "p", False, str(dump_dir)) is False
    assert dump_dir.is_dir()
    assert list(dump_dir.iterdir()) == []
