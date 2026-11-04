import logging

import moodle_updater
from modules.moodle_backup import MoodleBackupManager


def _manager(tmp_path, **runtimes):
    manager = MoodleBackupManager(str(tmp_path), "moodle", str(tmp_path))
    for name, value in runtimes.items():
        setattr(manager, name, value)
    return manager


def test_time_saved_only_counts_parallel_steps(tmp_path, caplog):
    manager = _manager(tmp_path, runtime_backup=30, runtime_clone=20, runtime_dump=60, runtime_cliupgrade=100)
    with caplog.at_level(logging.INFO):
        moodle_updater.log_run_summary(manager, runtime=200, lanes_runtime=60)
    assert "Time saved with multithreading: 50 seconds" in caplog.text


def test_no_time_saved_line_without_parallel_phase(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        moodle_updater.log_run_summary(_manager(tmp_path, runtime_dump=10), runtime=10)
    assert "Time saved" not in caplog.text
