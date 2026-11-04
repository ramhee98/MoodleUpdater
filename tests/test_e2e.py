"""End-to-end runs of the real CLI against a disposable site with fake services.

Every test runs moodle_updater.py in a subprocess (see helpers.Site.run) and checks
the exit code, the files on disk and the calls the fake tools received.
"""
import os
import stat

import pytest
from helpers import NEW_VERSION, OLD_VERSION, Site, git, snapshot

ALL_STEPS = ["--non-interactive", "--directory-backup", "--db-dump", "--git-clone",
             "--moodle-cli-upgrade", "--enable-maintenance-mode", "--restart-webserver"]


@pytest.fixture
def site(tmp_path):
    return Site(tmp_path)


def _mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def _out(result):
    return result.stdout + result.stderr


def test_happy_path(site):
    result = site.run(*ALL_STEPS)
    assert result.returncode == 0, _out(result)
    assert site.live_version() == NEW_VERSION
    assert site.leftovers() == []
    # Backup and dump exist and are private.
    (backup,) = os.listdir(site.backups)
    assert _mode(os.path.join(site.backups, backup)) == 0o700
    (dump,) = os.listdir(site.dumps)
    assert _mode(os.path.join(site.dumps, dump)) == 0o600
    # config.php was carried over without widening its mode.
    assert _mode(os.path.join(site.live, "config.php")) == 0o640
    # The webserver stayed down through the whole upgrade (H09) and came back afterwards.
    systemctl = [c[1] for c in site.calls("systemctl")]
    assert systemctl == ["stop", "start"]
    (context,) = [c for c in site.calls() if c[0] == "upgrade-context"]
    assert context[1:] == ["stopped", "on"]
    php = [c[1:] for c in site.calls("php")]
    assert php == [["checks.php"], ["maintenance.php", "--enable"], ["upgrade.php", "--non-interactive"],
                   ["maintenance.php", "--disable"], ["checks.php"]]


def _assert_untouched(site):
    """The live site is still the old version with its config.php and plugin, and no
    staging tree, partial backup or partial dump was left behind."""
    assert site.live_version() == OLD_VERSION
    assert os.path.isfile(os.path.join(site.live, "config.php"))
    assert os.path.isfile(os.path.join(site.live, "local", "custom", "version.php"))
    assert site.leftovers() == []


# --- C03: the swap waits for the dump ---------------------------------------------

def test_failed_dump_keeps_old_code_live(site):
    result = site.run(*ALL_STEPS, FAKE_DUMP_RC=2)
    assert result.returncode == 1, _out(result)
    _assert_untouched(site)
    assert os.listdir(site.dumps) == []  # no partial dump either
    assert not [c for c in site.calls("php") if c[1] == "upgrade.php"]
    assert [c[1] for c in site.calls("systemctl")] == ["stop", "start"]
    assert "Database dump failed" in _out(result)


# --- H04: submodule failures fail the clone ---------------------------------------

def test_failed_submodule_keeps_old_code_live(tmp_path):
    def add_broken_submodule(repo):
        sub = os.path.join(str(tmp_path), "subrepo")
        os.makedirs(sub)
        git("init", "-q", "-b", "main", cwd=sub)
        git("commit", "-q", "--allow-empty", "-m", "init", cwd=sub)
        git("-c", "protocol.file.allow=always", "submodule", "add", "-q", sub, "local/sub", cwd=repo)
        # The submodule's repository disappears: updating it must fail.
        import shutil
        shutil.rmtree(sub)

    site = Site(tmp_path / "site", upstream_extra=add_broken_submodule)
    result = site.run("--non-interactive", "--git-clone", "--moodle-cli-upgrade")
    assert result.returncode == 1, _out(result)
    _assert_untouched(site)
    assert "submodules failed: local/sub" in _out(result)
    assert not site.calls("php")


# --- H05: ownership is set before the swap ----------------------------------------

def test_failed_chown_keeps_old_code_live(site):
    result = site.run("--non-interactive", "--git-clone", "--moodle-cli-upgrade", FAKE_CHOWN_FAIL=1)
    assert result.returncode == 1, _out(result)
    _assert_untouched(site)
    assert not site.calls("php")


# --- H06: service failures ----------------------------------------------------------

def test_failed_webserver_stop_changes_nothing(site):
    result = site.run(*ALL_STEPS, FAKE_SYSTEMCTL_FAIL="stop")
    assert result.returncode == 1, _out(result)
    _assert_untouched(site)
    assert not os.path.exists(site.backups) or os.listdir(site.backups) == []
    assert not site.calls("mariadb-dump") and not site.calls("php")
    # The start is still attempted, so a half-stopped server comes back.
    assert [c[1] for c in site.calls("systemctl")] == ["stop", "start"]


def test_failed_webserver_start_fails_the_run(site):
    result = site.run(*ALL_STEPS, FAKE_SYSTEMCTL_FAIL="start")
    assert result.returncode == 1, _out(result)
    assert site.live_version() == NEW_VERSION
    assert "FAILED STEPS: Starting the webserver" in _out(result)


def test_missing_webserver_is_a_failure(site):
    result = site.run(*ALL_STEPS, FAKE_APT_INSTALLED="mariadb-server")
    assert result.returncode == 1, _out(result)
    _assert_untouched(site)


def test_failed_database_restart_changes_nothing(site):
    result = site.run(*ALL_STEPS, "--restart-database", FAKE_SYSTEMCTL_FAIL="restart")
    assert result.returncode == 1, _out(result)
    _assert_untouched(site)
    assert not site.calls("mariadb-dump")


def test_interrupted_stop_still_starts_webserver(site):
    # SIGINT arrives while systemctl stop runs: the start must still happen.
    result = site.run(*ALL_STEPS, FAKE_SIGINT_ON="stop")
    assert [c[1] for c in site.calls("systemctl")][:2] == ["stop", "start"]
    assert result.returncode != 0
    _assert_untouched(site)


# --- H07: maintenance mode failures -------------------------------------------------

def test_failed_maintenance_enable_skips_upgrade(site):
    result = site.run(*ALL_STEPS, FAKE_MAINT_ENABLE_RC=1)
    assert result.returncode == 1, _out(result)
    assert not [c for c in site.calls("php") if c[1] == "upgrade.php"]
    assert "maintenance mode was requested but could not be enabled" in _out(result)


def test_failed_maintenance_disable_fails_the_run(site):
    result = site.run(*ALL_STEPS, FAKE_MAINT_DISABLE_RC=1)
    assert result.returncode == 1, _out(result)
    assert "Maintenance mode is still ENABLED" in _out(result)
    assert "maintenance.php --disable" in _out(result)


# --- H08: system check exit codes ---------------------------------------------------

@pytest.mark.parametrize("code", [2, 3, 255])
def test_failed_pre_check_skips_upgrade(site, code):
    result = site.run("--non-interactive", "--moodle-cli-upgrade", FAKE_CHECKS_RC=code,
                      FAKE_CHECKS_STDERR="PHP Fatal error: boom")
    assert result.returncode == 1, _out(result)
    assert not [c for c in site.calls("php") if c[1] == "upgrade.php"]


def test_check_warning_does_not_block(site):
    result = site.run("--non-interactive", "--moodle-cli-upgrade", FAKE_CHECKS_RC=1)
    assert result.returncode == 0, _out(result)
    assert [c for c in site.calls("php") if c[1] == "upgrade.php"]


def test_force_continue_overrides_failed_pre_check(site):
    result = site.run("--non-interactive", "--moodle-cli-upgrade", "--force-continue", FAKE_CHECKS_RC="2,0")
    assert result.returncode == 0, _out(result)
    assert [c for c in site.calls("php") if c[1] == "upgrade.php"]


def test_failed_post_check_fails_the_run(site):
    result = site.run("--non-interactive", "--moodle-cli-upgrade", FAKE_CHECKS_RC="0,2")
    assert result.returncode == 1, _out(result)


def test_failed_upgrade_follows_recovery_policy(site):
    # Agreed policy: disable maintenance mode, start the webserver, exit 1.
    result = site.run(*ALL_STEPS, FAKE_UPGRADE_RC=1)
    assert result.returncode == 1, _out(result)
    assert site.live_version() == NEW_VERSION
    assert [c[1:] for c in site.calls("php") if c[1] == "maintenance.php"] == [["maintenance.php", "--enable"],
                                                                           ["maintenance.php", "--disable"]]
    assert [c[1] for c in site.calls("systemctl")] == ["stop", "start"]


# --- H10: dry run changes nothing -----------------------------------------------------

def test_dry_run_changes_nothing(tmp_path):
    site = Site(tmp_path)
    # A self-update would pull a new commit into the updater's own checkout.
    updater_upstream = os.path.join(site.root, "updater-upstream")
    git("init", "-q", "-b", "main", site.app)
    git("add", "-A", cwd=site.app)
    git("commit", "-q", "-m", "installed", cwd=site.app)
    git("clone", "-q", site.app, updater_upstream)
    git("remote", "add", "origin", updater_upstream, cwd=site.app)
    git("fetch", "-q", "origin", cwd=site.app)
    git("branch", "-q", "--set-upstream-to=origin/main", cwd=site.app)
    with open(os.path.join(updater_upstream, "pulled.txt"), "w") as f:
        f.write("new code")
    git("add", "-A", cwd=updater_upstream)
    git("commit", "-q", "-m", "update", cwd=updater_upstream)
    site.write_config(auto_update_script="True")

    # The dry run's only output is its log; it exists already on a real install.
    open(os.path.join(site.app, "moodle_updater.log"), "w").close()
    before = snapshot(site.root, exclude={os.path.join("app", "moodle_updater.log"), "fake.log"})
    result = site.run("--dry-run", *ALL_STEPS, "--full-backup", "--restore-plugins", "--restart-database")
    assert result.returncode == 0, _out(result)
    after = snapshot(site.root, exclude={os.path.join("app", "moodle_updater.log"), "fake.log"})
    assert after == before
    assert not os.path.exists(os.path.join(site.app, "pulled.txt"))
    # No fake tool ran at all: no DB client, no php, no systemctl, no chown.
    assert site.calls() == []
    assert "[Dry Run] Skipping self-update" in _out(result)


def test_dry_run_does_not_create_missing_config(site):
    os.remove(os.path.join(site.app, "config.ini"))
    result = site.run("--dry-run", "--non-interactive", "--git-clone")
    assert result.returncode == 1
    assert not os.path.exists(os.path.join(site.app, "config.ini"))
    assert not os.path.exists(os.path.join(site.app, "moodle_updater.log"))


def test_missing_config_is_reported_before_logging_starts(site):
    # L01: the log file is not created before the missing config is noticed.
    os.remove(os.path.join(site.app, "config.ini"))
    result = site.run("--non-interactive", "--git-clone")
    assert result.returncode == 1
    assert os.path.exists(os.path.join(site.app, "config.ini"))  # created from the template
    assert not os.path.exists(os.path.join(site.app, "moodle_updater.log"))
    assert "not found" in _out(result)


# --- C02: configured dump location ----------------------------------------------------

def test_clone_only_refuses_dump_dir_inside_code(site):
    inside = os.path.join(site.live, "dumps")
    os.makedirs(inside)
    with open(os.path.join(inside, "moodle_old.sql"), "w") as f:
        f.write("precious")
    site.write_config(db_dump_path=inside)
    result = site.run("--non-interactive", "--git-clone")
    assert result.returncode == 1, _out(result)
    assert os.path.isfile(os.path.join(inside, "moodle_old.sql"))


def test_full_backup_excludes_old_dumps_without_db_dump(site):
    dumps_in_path = os.path.join(site.www, "dumps")
    os.makedirs(dumps_in_path)
    with open(os.path.join(dumps_in_path, "moodle_old.sql"), "w") as f:
        f.write("old dump")
    site.write_config(db_dump_path=dumps_in_path)
    result = site.run("--non-interactive", "--directory-backup", "--full-backup")
    assert result.returncode == 0, _out(result)
    (backup,) = os.listdir(site.backups)
    assert not os.path.exists(os.path.join(site.backups, backup, "dumps", "moodle_old.sql"))
    assert os.path.isfile(os.path.join(site.backups, backup, "moodle", "config.php"))


# --- C01 / H02: config.php in the new tree ----------------------------------------------

def test_config_php_symlink_in_repository_is_not_followed(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched")

    def add_symlink(repo):
        os.symlink(str(victim), os.path.join(repo, "config.php"))

    site = Site(tmp_path / "site", upstream_extra=add_symlink)
    result = site.run("--non-interactive", "--git-clone")
    assert result.returncode == 0, _out(result)
    assert victim.read_text() == "untouched"
    config = os.path.join(site.live, "config.php")
    assert not os.path.islink(config)
    assert "s3cret" in open(config).read()
    assert _mode(config) == 0o640


@pytest.mark.parametrize("original_mode, expected", [(0o600, 0o600), (0o640, 0o640), (0o666, 0o644)])
def test_config_php_mode_is_kept_without_group_or_world_write(site, original_mode, expected):
    os.chmod(os.path.join(site.live, "config.php"), original_mode)
    old_umask = os.umask(0o022)
    try:
        result = site.run("--non-interactive", "--git-clone")
    finally:
        os.umask(old_umask)
    assert result.returncode == 0, _out(result)
    assert _mode(os.path.join(site.live, "config.php")) == expected


# --- H13: downgrade refused on the staged tree ----------------------------------------

def test_downgrade_is_refused_even_without_remote_version(tmp_path):
    # Upstream's branch tip is OLDER than the live site; the HTTP check can't run
    # against a local path, so only the staged-tree check can catch it.
    site = Site(tmp_path, upstream_versions=(NEW_VERSION, OLD_VERSION))
    assert site.live_version() == NEW_VERSION
    result = site.run("--non-interactive", "--git-clone", "--moodle-cli-upgrade")
    assert result.returncode == 1, _out(result)
    assert site.live_version() == NEW_VERSION
    assert "Refusing to downgrade" in _out(result)
    assert site.leftovers() == []


def test_unknown_installed_version_warns_and_proceeds(site):
    os.remove(os.path.join(site.live, "version.php"))
    result = site.run("--non-interactive", "--git-clone")
    assert result.returncode == 0, _out(result)
    assert "a downgrade cannot be ruled out" in _out(result)
    assert site.live_version() == NEW_VERSION


# --- H12: plugins that are submodules -------------------------------------------------

def test_restore_plugins_fills_unsynced_submodule(tmp_path):
    def add_submodule(repo):
        sub = os.path.join(str(tmp_path), "pluginrepo")
        os.makedirs(sub)
        git("init", "-q", "-b", "main", cwd=sub)
        with open(os.path.join(sub, "version.php"), "w") as f:
            f.write("<?php // plugin from submodule\n")
        git("add", "-A", cwd=sub)
        git("commit", "-q", "-m", "init", cwd=sub)
        git("-c", "protocol.file.allow=always", "submodule", "add", "-q", sub, "local/subplugin", cwd=repo)

    site = Site(tmp_path / "site", upstream_extra=add_submodule)
    # The live site has the plugin checked out (as a submodule would be).
    os.makedirs(os.path.join(site.live, "local", "subplugin"), exist_ok=True)
    with open(os.path.join(site.live, "local", "subplugin", "version.php"), "w") as f:
        f.write("<?php // plugin from submodule\n")
    result = site.run("--non-interactive", "--directory-backup", "--git-clone", "--sync-submodules-off",
                      "--restore-plugins")
    assert result.returncode == 0, _out(result)
    assert os.path.isfile(os.path.join(site.live, "local", "subplugin", "version.php"))
    assert os.path.isfile(os.path.join(site.live, "local", "custom", "version.php"))
