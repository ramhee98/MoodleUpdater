import os
import subprocess
import sys

import pytest

# Make the top-level `modules` package and `moodle_updater` importable from tests.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# chown to our own numeric ids works without root and without named groups.
USER = str(os.getuid())
GROUP = str(os.getgid())


def git(*args, cwd=None):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def upstream(tmp_path):
    """A local repository with one commit on branch MOODLE_TEST."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    git("init", "-q", "-b", "MOODLE_TEST", cwd=repo)
    (repo / "version.php").write_text("<?php $version = 2;\n")
    (repo / "mod" / "forum").mkdir(parents=True)
    (repo / "mod" / "forum" / "version.php").write_text("<?php\n")
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    return str(repo)
