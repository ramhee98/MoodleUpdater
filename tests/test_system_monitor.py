import shutil
import subprocess
import threading
import time

import pytest

from modules.system_monitor import SystemMonitor


def test_size_query_uses_installed_client(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/mariadb" if name == "mariadb" else None)
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "moodle\t1234.50\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert SystemMonitor().get_database_size_mb("moodle", "u", "p") == 1234.5
    assert seen["cmd"][0] == "mariadb"


@pytest.mark.parametrize("outcome", [
    FileNotFoundError("no client"),
    subprocess.CompletedProcess([], 1, "", "Access denied"),
    subprocess.CompletedProcess([], 0, "", ""),
])
def test_size_query_failure_returns_none(monkeypatch, outcome):
    def fake_run(cmd, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert SystemMonitor().get_database_size_mb("moodle", "u", "p") is None


def test_invalid_db_name_is_rejected():
    assert SystemMonitor().get_database_size_mb("moodle'; DROP", "u", "p") is None


@pytest.mark.parametrize("db_size", [None, 0.0])
def test_progress_without_size_estimate_does_not_crash(tmp_path, monkeypatch, caplog, db_size):
    monitor = SystemMonitor()
    monkeypatch.setattr(monitor, "get_database_size_mb", lambda *a: db_size)
    dump = tmp_path / "dump.sql"
    dump.write_text("x" * 10)
    thread = threading.Thread(target=monitor.monitor_dump_progress,
                              args=(str(dump), "db", "u", "p"),
                              kwargs={"check_interval": 0.01, "log_interval": 0})
    with caplog.at_level("INFO"):
        thread.start()
        time.sleep(0.05)
        dump.write_text("x" * 2000)
        time.sleep(0.05)
        monitor.stop_event.set()
        thread.join(timeout=2)
    assert not thread.is_alive()
    assert "Estimated size: unknown" in caplog.text
    assert "Database dump progress:" in caplog.text


class RecordingEvent(threading.Event):
    """Stops after the first wait() and records the requested timeout."""

    def __init__(self):
        super().__init__()
        self.timeouts = []

    def wait(self, timeout=None):
        self.timeouts.append(timeout)
        self.set()
        return True


@pytest.mark.parametrize("stats, expected_wait", [
    ((8000, 7800, 100, 0, 100, 200), 0.5),    # available critically low
    ((8000, 7500, 400, 0, 100, 450), 1),      # available low
    ((8000, 1000, 200, 0, 6800, 6000), 2),    # free low, available fine
    ((8000, 1000, 3000, 0, 4000, 6000), 5),   # normal
])
def test_memory_poll_interval_uses_shortest_applicable(monkeypatch, stats, expected_wait):
    monitor = SystemMonitor()
    monitor.stop_event = RecordingEvent()
    monkeypatch.setattr(SystemMonitor, "read_memory_stats", staticmethod(lambda: stats))
    monitor.monitor_memory_usage()
    assert monitor.stop_event.timeouts == [expected_wait]


def test_stop_is_prompt_and_thread_errors_are_logged(monkeypatch, tmp_path, caplog):
    monitor = SystemMonitor()
    monkeypatch.setattr(monitor, "get_database_size_mb", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(SystemMonitor, "read_memory_stats", staticmethod(lambda: (8000, 1000, 3000, 0, 4000, 6000)))
    with caplog.at_level("ERROR"):
        monitor.start_monitoring(str(tmp_path / "d.sql"), "db", "u", "p")
        start = time.time()
        monitor.stop_monitoring()
    assert time.time() - start < 2
    assert "Dump progress monitoring stopped because of an unexpected error" in caplog.text
