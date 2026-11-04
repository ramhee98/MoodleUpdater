"""Regression tests for findings of the independent review (REVIEW.md) that are
best exercised below the CLI: real rsync, git, subprocesses and fake DB clients."""
import hashlib
import http.server
import os
import shutil
import stat
import subprocess
import sys
import threading
import time

import pytest
from helpers import make_fakebin

import moodle_updater
from modules import moodle_backup
from modules.git_manager import git_environment, git_stdin
from modules.moodle_backup import MoodleBackupManager
from modules.steps import run_steps_concurrently
from modules.system_monitor import SystemMonitor


def _mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    """Fake DB clients etc. first on PATH, logging to <tmp>/fake.log."""
    fakebin = make_fakebin(str(tmp_path / "fakebin"))
    (tmp_path / "state").mkdir()
    monkeypatch.setenv("PATH", fakebin + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "fake.log"))
    monkeypatch.setenv("FAKE_STATE", str(tmp_path / "state"))
    return fakebin


# --- H01: backup privacy during and after rsync -----------------------------------------

RSYNC_SPY = """#!{python}
import os, subprocess, sys
dest = sys.argv[-1]
code = subprocess.call([{real!r}] + sys.argv[1:])
with open({log!r}, "a") as f:
    parent = os.path.dirname(dest.rstrip("/"))
    f.write("%o %o\\n" % (os.stat(parent).st_mode & 0o777, os.stat(dest).st_mode & 0o777))
sys.exit(int(os.environ.get("SPY_RSYNC_RC", code)))
"""


@pytest.fixture
def rsync_spy(tmp_path, monkeypatch):
    """Real rsync, wrapped to record the modes of the destination and its parent
    right after copying (i.e. while the backup is still being written)."""
    spy_dir = tmp_path / "spybin"
    spy_dir.mkdir()
    log = tmp_path / "rsync.log"
    (spy_dir / "rsync").write_text(RSYNC_SPY.format(python=sys.executable, real=shutil.which("rsync"), log=str(log)))
    (spy_dir / "rsync").chmod(0o755)
    monkeypatch.setenv("PATH", str(spy_dir) + os.pathsep + os.environ["PATH"])
    return log


def _source(tmp_path):
    src = tmp_path / "www" / "moodle"
    src.mkdir(parents=True)
    (src / "config.php").write_text("<?php $CFG->dbpass = 'x';")
    src.chmod(0o755)
    backups = tmp_path / "backups"
    backups.mkdir()
    return MoodleBackupManager(str(tmp_path / "www"), "moodle", str(backups)), backups


def test_backup_is_private_while_copying(tmp_path, rsync_spy):
    manager, backups = _source(tmp_path)
    assert manager.dir_backup(full_backup=False) is True
    parent_mode, dest_mode = rsync_spy.read_text().split()
    # rsync gave the destination the source's 0755, but nobody else could reach it.
    assert (parent_mode, dest_mode) == ("700", "755")
    assert _mode(manager.backup_folder) == 0o700
    assert os.listdir(backups) == [os.path.basename(manager.backup_folder)]


def test_failed_backup_leaves_nothing_behind(tmp_path, rsync_spy, monkeypatch):
    monkeypatch.setenv("SPY_RSYNC_RC", "23")
    manager, backups = _source(tmp_path)
    assert manager.dir_backup(full_backup=False) is False
    assert rsync_spy.read_text().split()[0] == "700"
    assert os.listdir(backups) == []
    assert manager.backup_folder is None


# --- H03: predictable paths ---------------------------------------------------------------

FIXED_TS = "2026-01-01-00-00-00"


@pytest.fixture
def fixed_time(monkeypatch):
    monkeypatch.setattr(moodle_backup.time, "strftime", lambda fmt, *a: FIXED_TS)


def test_dump_does_not_follow_planted_symlink(tmp_path, fakes, fixed_time):
    victim = tmp_path / "victim"
    victim.write_text("precious")
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    os.symlink(victim, dumps / f".moodle_{FIXED_TS}.partial.sql")
    manager = MoodleBackupManager(str(tmp_path), "moodle", str(tmp_path))
    assert manager.db_dump("moodle", "u", "pw", False, str(dumps)) is False
    assert victim.read_text() == "precious"
    assert os.path.islink(dumps / f".moodle_{FIXED_TS}.partial.sql")  # not ours: left alone


@pytest.mark.parametrize("dump_rc", ["0", "2"])
def test_dump_never_clobbers_existing_dump(tmp_path, fakes, fixed_time, monkeypatch, dump_rc):
    monkeypatch.setenv("FAKE_DUMP_RC", dump_rc)
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    existing = dumps / f"moodle_{FIXED_TS}.sql"
    existing.write_text("valid earlier dump")
    manager = MoodleBackupManager(str(tmp_path), "moodle", str(tmp_path))
    ok = manager.db_dump("moodle", "u", "pw", False, str(dumps))
    assert existing.read_text() == "valid earlier dump"
    if dump_rc == "0":
        assert ok and manager.dump_file == str(dumps / f"moodle_{FIXED_TS}-1.sql")
        assert _mode(manager.dump_file) == 0o600
        assert set(os.listdir(dumps)) == {existing.name, f"moodle_{FIXED_TS}-1.sql"}
    else:
        assert not ok and os.listdir(dumps) == [existing.name]


def _lock_path(lock_dir, instance):
    return os.path.join(lock_dir, hashlib.sha256(os.path.realpath(instance).encode()).hexdigest()[:16] + ".lock")


def test_lock_does_not_follow_planted_symlink(tmp_path):
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(mode=0o700)
    victim = tmp_path / "victim"
    victim.write_text("precious")
    instance = str(tmp_path / "www" / "moodle")
    os.symlink(victim, _lock_path(str(lock_dir), instance))
    with pytest.raises(SystemExit):
        moodle_updater.acquire_instance_lock(instance, str(lock_dir))
    assert victim.read_text() == "precious"


def test_lock_refuses_shared_writable_directory(tmp_path):
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    lock_dir.chmod(0o777)
    with pytest.raises(SystemExit):
        moodle_updater.acquire_instance_lock(str(tmp_path / "moodle"), str(lock_dir))


# --- M03: one lock per canonical instance -------------------------------------------------

def test_lock_is_per_canonical_instance(tmp_path):
    lock_dir = str(tmp_path / "locks")
    (tmp_path / "www" / "moodle").mkdir(parents=True)
    os.symlink(tmp_path / "www", tmp_path / "alias")
    held = moodle_updater.acquire_instance_lock(str(tmp_path / "www" / "moodle"), lock_dir)
    try:
        # Same site through another path (e.g. another config file): refused.
        with pytest.raises(SystemExit):
            moodle_updater.acquire_instance_lock(str(tmp_path / "alias" / "moodle"), lock_dir)
        # Another site with the same folder name: independent.
        moodle_updater.acquire_instance_lock(str(tmp_path / "other" / "moodle"), lock_dir).close()
    finally:
        held.close()


# --- H11: upgrade output cannot deadlock ----------------------------------------------------

def test_upgrade_output_with_partial_line_and_full_stderr_pipe(tmp_path):
    child = tmp_path / "child.py"
    child.write_text(
        "import sys\n"
        "sys.stdout.write('partial line without newline'); sys.stdout.flush()\n"
        "for i in range(20000):\n"
        "    sys.stderr.write('warning %d: ' % i + 'x' * 40 + '\\n')\n"  # ~1 MB, far above a pipe buffer
        "sys.stderr.flush()\n"
        "sys.stdout.write(' ... done\\n')\n"
    )
    manager = MoodleBackupManager(str(tmp_path), "moodle", str(tmp_path))
    outcome = {}
    thread = threading.Thread(target=lambda: outcome.update(
        result=manager._run_upgrade_process([sys.executable, str(child)])), daemon=True)
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive(), "upgrade output handling deadlocked"
    returncode, errors = outcome["result"]
    assert returncode == 0
    assert len(errors) == 20000


# --- H14: git never prompts in non-interactive mode -------------------------------------------

class _AuthRequired(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="repo"')
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def auth_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _AuthRequired)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/moodle.git"
    server.shutdown()


def test_non_interactive_clone_does_not_ask_for_credentials(tmp_path, auth_server, capfd, monkeypatch):
    monkeypatch.delenv("GIT_ASKPASS", raising=False)
    monkeypatch.delenv("SSH_ASKPASS", raising=False)
    (tmp_path / "www").mkdir()
    manager = MoodleBackupManager(str(tmp_path / "www"), "moodle", str(tmp_path), non_interactive=True)
    start = time.time()
    assert manager.prepare_clone("<?php", auth_server, "main", False) is False
    assert time.time() - start < 30
    assert "terminal prompts disabled" in capfd.readouterr().err
    assert os.listdir(tmp_path / "www") == []  # staging removed


def test_git_environment(monkeypatch):
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    env = git_environment(True)
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]
    assert git_stdin(True) == subprocess.DEVNULL
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /root/deploy_key")
    assert git_environment(True)["GIT_SSH_COMMAND"] == "ssh -i /root/deploy_key"
    assert git_environment(False) == dict(os.environ)  # interactive runs may prompt as before
    assert git_stdin(False) is None


# --- H15 / M02: DB size client ----------------------------------------------------------------

def test_stalled_size_query_is_bounded(fakes, monkeypatch):
    monkeypatch.setenv("FAKE_MYSQL_SLEEP", "30")
    monkeypatch.setattr(SystemMonitor, "SIZE_QUERY_TIMEOUT", 1)
    start = time.time()
    assert SystemMonitor().get_database_size_mb("moodle", "u", "pw") is None
    assert time.time() - start < 10


def test_monitor_stop_is_bounded_even_with_stalled_client(tmp_path, fakes, monkeypatch):
    monkeypatch.setenv("FAKE_MYSQL_SLEEP", "30")
    monkeypatch.setattr(SystemMonitor, "STOP_TIMEOUT", 1)
    monitor = SystemMonitor()
    monitor.start_monitoring(str(tmp_path / "d.sql"), "moodle", "u", "pw")
    time.sleep(0.2)
    start = time.time()
    monitor.stop_monitoring()
    assert time.time() - start < 5


def test_size_query_error_is_sanitized(fakes, monkeypatch, caplog):
    monkeypatch.setenv("FAKE_MYSQL_FAIL", "1")
    with caplog.at_level("WARNING"):
        assert SystemMonitor().get_database_size_mb("moodle", "u", "s3cretpw") is None
    assert "Could not determine database size" in caplog.text
    assert "s3cretpw" not in caplog.text


# --- H16: a failed rollback ------------------------------------------------------------------

def test_failed_swap_and_failed_rollback(tmp_path, monkeypatch, caplog):
    www = tmp_path / "www"
    live = www / "moodle"
    live.mkdir(parents=True)
    (live / "index.php").write_text("old code")
    staging = www / ".moodle_new_x"
    staging.mkdir()
    (staging / "config.php").write_text("<?php $CFG->dbpass = 'secret';")
    old = www / ".moodle_old_x"

    real_rename = os.rename

    def flaky_rename(src, dst):
        if str(dst) == str(live):  # both "new -> live" and "old -> live" fail
            raise OSError("injected failure")
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", flaky_rename)
    manager = MoodleBackupManager(str(www), "moodle", str(tmp_path))
    assert manager._swap_in(str(staging), str(live), str(old)) is False
    assert manager.live_tree_missing
    assert (old / "index.php").read_text() == "old code"   # recoverable
    assert not staging.exists()                            # staged secrets removed
    assert f"mv '{old}' '{live}'" in caplog.text           # exact recovery step


# --- M04: abnormal worker termination ------------------------------------------------------------

def test_worker_system_exit_is_recorded_as_failure():
    def exits():
        sys.exit(1)

    assert run_steps_concurrently([("exits", exits), ("ok", lambda: True)]) == {"exits": False, "ok": True}


# --- M05: CI tool check -----------------------------------------------------------------------------

def test_ci_tool_check_survives_pipefail():
    workflow = open(os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "ci.yml")).read()
    (command,) = [line.split("run:", 1)[1].strip() for line in workflow.splitlines() if "rsync --version" in line]
    assert subprocess.run(["bash", "-e", "-o", "pipefail", "-c", command], capture_output=True).returncode == 0
