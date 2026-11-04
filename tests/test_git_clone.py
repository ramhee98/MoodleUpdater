import os

import pytest
from conftest import GROUP, USER

from modules.moodle_backup import MoodleBackupManager

@pytest.fixture
def site(tmp_path):
    """An existing Moodle install at <site>/moodle with an old file and config.php."""
    root = tmp_path / "www"
    (root / "moodle").mkdir(parents=True)
    (root / "moodle" / "old_file.php").write_text("old")
    (root / "moodle" / "config.php").write_text("<?php // old config")
    return root


def _manager(site):
    return MoodleBackupManager(str(site), "moodle", str(site))


def _leftovers(site):
    return [name for name in os.listdir(site) if name.startswith(".moodle_")]


def test_clone_replaces_code_and_writes_config(upstream, site):
    ok = _manager(site).git_clone("<?php // new config", upstream, "MOODLE_TEST", False, USER, GROUP)
    assert ok is True
    moodle = site / "moodle"
    assert (moodle / "version.php").exists()
    assert not (moodle / "old_file.php").exists()
    assert (moodle / "config.php").read_text() == "<?php // new config"
    assert _leftovers(site) == []


@pytest.mark.parametrize("repo_override, branch", [("/nonexistent/repo", "MOODLE_TEST"), (None, "NO_SUCH_BRANCH")])
def test_failed_clone_or_checkout_keeps_existing_code(upstream, site, repo_override, branch):
    ok = _manager(site).git_clone("<?php // new", repo_override or upstream, branch, False, USER, GROUP)
    assert ok is False
    assert (site / "moodle" / "old_file.php").read_text() == "old"
    assert (site / "moodle" / "config.php").read_text() == "<?php // old config"
    assert _leftovers(site) == []


def test_config_none_skips_config_php(upstream, site):
    assert _manager(site).git_clone(None, upstream, "MOODLE_TEST", False, USER, GROUP) is True
    assert not (site / "moodle" / "config.php").exists()


def test_clone_into_empty_site(upstream, tmp_path):
    root = tmp_path / "fresh"
    root.mkdir()
    assert _manager(root).git_clone("<?php", upstream, "MOODLE_TEST", False, USER, GROUP) is True
    assert (root / "moodle" / "version.php").exists()


def test_dry_run_changes_nothing(upstream, site):
    manager = MoodleBackupManager(str(site), "moodle", str(site), dry_run=True)
    assert manager.git_clone("<?php", upstream, "MOODLE_TEST", True, USER, GROUP) is True
    assert (site / "moodle" / "old_file.php").exists()
    assert _leftovers(site) == []
