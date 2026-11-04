import os
import time

import pytest
from conftest import GROUP, USER

from modules.moodle_backup import MoodleBackupManager


def _plugin(root, rel):
    path = root / rel
    path.mkdir(parents=True, exist_ok=True)
    (path / "version.php").write_text("<?php\n")


@pytest.fixture
def layout(tmp_path):
    """<tmp>/www/moodle (new clone) and <tmp>/backups/moodle_bak_partial_X (this run's backup)."""
    clone = tmp_path / "www" / "moodle"
    _plugin(clone, "mod/forum")
    backup = tmp_path / "backups" / "moodle_bak_partial_2026-01-01-00-00-00"
    _plugin(backup, "mod/forum")
    _plugin(backup, "local/custom")
    _plugin(backup, "local/custom/sub/plugin")  # nested, comes with its parent
    _plugin(backup, "blocks/third")
    return tmp_path, backup


def _manager(tmp_path, **kwargs):
    return MoodleBackupManager(str(tmp_path / "www"), "moodle", str(tmp_path / "backups"), **kwargs)


def test_restore_plugins_without_backup_this_run_fails(layout):
    tmp_path, _ = layout
    assert _manager(tmp_path).restore_plugins(USER, GROUP) is False


def test_restore_plugins_copies_missing_plugins(layout):
    tmp_path, backup = layout
    manager = _manager(tmp_path)
    manager.backup_folder = str(backup)
    assert manager.restore_plugins(USER, GROUP) is True
    assert sorted(manager.restored_plugins) == ["blocks/third", "local/custom"]
    assert (tmp_path / "www" / "moodle" / "local" / "custom" / "sub" / "plugin" / "version.php").exists()


def test_restore_plugins_ignores_newer_unrelated_backups(layout):
    tmp_path, backup = layout
    stale = tmp_path / "backups" / "moodle_bak_partial_2099-01-01-00-00-00"
    _plugin(stale, "local/stale")
    future = time.time() + 3600
    os.utime(stale, (future, future))
    manager = _manager(tmp_path)
    manager.backup_folder = str(backup)
    manager.restore_plugins(USER, GROUP)
    assert "local/stale" not in manager.restored_plugins


def test_restore_plugins_full_backup_reads_moodle_subfolder(tmp_path):
    _plugin(tmp_path / "www" / "moodle", "mod/forum")
    backup = tmp_path / "backups" / "moodle_bak_full_X"
    _plugin(backup / "moodle", "mod/forum")
    _plugin(backup / "moodle", "local/fromfull")
    manager = _manager(tmp_path)
    manager.backup_folder = str(backup)
    assert manager.restore_plugins(USER, GROUP, full_backup=True) is True
    assert manager.restored_plugins == ["local/fromfull"]


@pytest.mark.parametrize("full_backup", [False, True])
def test_restore_submodules_from_this_runs_backup(tmp_path, upstream, full_backup):
    site = tmp_path / "www"
    (site / "moodle").mkdir(parents=True)
    backup = tmp_path / "backups" / "moodle_bak_X"
    code = backup / "moodle" if full_backup else backup
    (code / "local" / "sub").mkdir(parents=True)
    (code / "local" / "sub" / "lib.php").write_text("<?php // from backup")
    (code / ".gitmodules").write_text('[submodule "local/sub"]\n\tpath = local/sub\n\turl = https://example.org/sub.git\n')

    manager = _manager(tmp_path)
    manager.backup_folder = str(backup)
    ok = manager.git_clone("<?php", upstream, "MOODLE_TEST", False, USER, GROUP,
                           restore_submodules_from_backup=True, full_backup=full_backup)
    assert ok is True
    assert (site / "moodle" / "local" / "sub" / "lib.php").read_text() == "<?php // from backup"


def test_restore_submodules_without_backup_keeps_existing_code(tmp_path, upstream):
    site = tmp_path / "www"
    (site / "moodle").mkdir(parents=True)
    (site / "moodle" / "keep.php").write_text("old")
    ok = _manager(tmp_path).git_clone("<?php", upstream, "MOODLE_TEST", False, USER, GROUP,
                                      restore_submodules_from_backup=True)
    assert ok is False
    assert (site / "moodle" / "keep.php").exists()
