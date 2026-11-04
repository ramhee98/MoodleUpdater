import os

from modules.moodle_backup import MoodleBackupManager, is_within


def test_is_within(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "ab").mkdir()
    assert is_within(tmp_path / "a" / "b", tmp_path / "a")
    assert is_within(tmp_path / "a", tmp_path / "a")
    assert is_within(str(tmp_path / "a") + "/", tmp_path / "a")
    assert not is_within(tmp_path / "ab", tmp_path / "a")
    assert not is_within(tmp_path, tmp_path / "a")


def test_full_backup_skips_own_backups_and_dumps(tmp_path):
    # README layout: path, folder_backup_path and db_dump_path all under /var/www/moodle
    root = tmp_path / "www"
    (root / "moodle").mkdir(parents=True)
    (root / "moodle" / "index.php").write_text("<?php")
    (root / "moodledata").mkdir()
    (root / "moodle_bak_partial_old").mkdir()
    (root / "moodle_bak_partial_old" / "big.bin").write_text("old backup")
    (root / ".moodle_updater_moodle.lock").write_text("1")
    (root / "db_dump").mkdir()
    (root / "db_dump" / "moodle_old.sql").write_text("old dump")
    (root / "db_dump" / "notes.txt").write_text("kept")

    manager = MoodleBackupManager(str(root), "moodle", str(root) + "/", db_dump_path=str(root / "db_dump"))
    assert manager.dir_backup(full_backup=True) is True
    backup = manager.backup_folder
    assert os.path.exists(os.path.join(backup, "moodle", "index.php"))
    assert os.path.exists(os.path.join(backup, "db_dump", "notes.txt"))
    assert not os.path.exists(os.path.join(backup, "moodle_bak_partial_old"))
    assert not os.path.exists(os.path.join(backup, "db_dump", "moodle_old.sql"))
    assert not os.path.exists(os.path.join(backup, ".moodle_updater_moodle.lock"))
    # The new backup itself lives in the source but must not contain a copy of itself.
    assert not any(name.startswith("moodle_bak_") for name in os.listdir(backup))


def test_no_excludes_when_outputs_live_elsewhere(tmp_path):
    manager = MoodleBackupManager(str(tmp_path / "www"), "moodle", str(tmp_path / "backups"),
                                  db_dump_path=str(tmp_path / "dumps"))
    assert manager._own_output_excludes(str(tmp_path / "www") + "/") == []
