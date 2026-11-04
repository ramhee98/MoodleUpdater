import pytest

from modules.moodle_version import MoodleVersionChecker


@pytest.mark.parametrize("repo, expected", [
    ("https://github.com/moodle/moodle", "https://github.com/moodle/moodle"),
    ("https://github.com/moodle/moodle.git", "https://github.com/moodle/moodle"),
    ("https://github.com/moodle/moodle/", "https://github.com/moodle/moodle"),
    ("https://code.gitlab.example/x/moodle.git", "https://code.gitlab.example/x/moodle"),
    ("https://git.example.org/my.github.io/moodle", "https://git.example.org/my.github.io/moodle"),
])
def test_raw_base_url_only_strips_suffix(repo, expected):
    assert MoodleVersionChecker("/x", repo, "main")._raw_base_url() == expected


@pytest.mark.parametrize("subdir", ["", "public"])
def test_local_version_both_layouts(tmp_path, subdir):
    root = tmp_path / subdir if subdir else tmp_path
    root.mkdir(exist_ok=True)
    (root / "version.php").write_text("<?php\n$version  = 2025041400.05;\n$release  = '5.0.1+ (Build: 20250620)';\n")
    assert MoodleVersionChecker(str(tmp_path), None, None).get_local_version() == ("5.0.1+ (Build: 20250620)", "2025041400.05")


def test_local_version_missing(tmp_path):
    assert MoodleVersionChecker(str(tmp_path), None, None).get_local_version() == ("Unknown", "Unknown")
