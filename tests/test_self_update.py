import logging
import os

import pytest
from conftest import git

from modules.git_manager import GitManager


class Restarted(BaseException):
    """Raised instead of os.execv; BaseException so self_update's blanket except can't swallow it."""


@pytest.fixture
def checkout(tmp_path, upstream, monkeypatch):
    """A clone of `upstream` acting as the MoodleUpdater install."""
    path = tmp_path / "updater"
    git("clone", "-q", upstream, str(path))
    monkeypatch.setattr(os, "execv", lambda *a: (_ for _ in ()).throw(Restarted()))
    cfg = tmp_path / "config.ini"
    cfg.write_text("[settings]\n")
    return str(path), str(cfg)


def _update(checkout):
    path, cfg = checkout
    GitManager.self_update(path, cfg, cfg)


def test_no_restart_when_up_to_date(checkout):
    _update(checkout)  # must not raise Restarted


def test_restart_after_new_commit(checkout, upstream):
    with open(os.path.join(upstream, "new.txt"), "w") as f:
        f.write("x")
    git("add", ".", cwd=upstream)
    git("commit", "-q", "-m", "new", cwd=upstream)
    with pytest.raises(Restarted):
        _update(checkout)


def test_up_to_date_detection_does_not_depend_on_locale(checkout, monkeypatch):
    # Under a non-English locale git prints e.g. "Bereits aktuell."; only commits matter.
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    _update(checkout)


def test_local_changes_skip_update(checkout, upstream):
    path, _ = checkout
    with open(os.path.join(path, "version.php"), "a") as f:
        f.write("// local edit\n")
    with open(os.path.join(upstream, "new.txt"), "w") as f:
        f.write("x")
    git("add", ".", cwd=upstream)
    git("commit", "-q", "-m", "new", cwd=upstream)
    _update(checkout)  # skipped, no restart


def test_failing_git_does_not_crash(tmp_path, caplog):
    broken = tmp_path / "broken"
    (broken / ".git").mkdir(parents=True)  # looks like a repo, git disagrees
    caplog.set_level(logging.INFO)
    GitManager.self_update(str(broken), str(tmp_path / "c.ini"), str(tmp_path / "c.ini"))
    assert "Error during self-update" not in caplog.text
    assert "Continuing with the current version." in caplog.text
