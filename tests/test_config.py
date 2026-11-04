import configparser

import pytest

from modules.config_manager import ConfigManager


def _config(text):
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(text)
    return config


@pytest.mark.parametrize("value, expected", [
    ("True", True), ("true", True), ("yes", True), ("1", True), ("on", True),
    ("False", False), ("false", False), ("no", False), ("0", False),
])
def test_get_bool_accepts_common_spellings(value, expected):
    assert ConfigManager.get_bool(_config(f"[settings]\ndry_run = {value}\n"), "settings", "dry_run") is expected


def test_get_bool_fallback_when_missing():
    config = _config("[settings]\n")
    assert ConfigManager.get_bool(config, "settings", "dry_run") is False
    assert ConfigManager.get_bool(config, "database", "read_db_from_config", fallback=True) is True


def test_get_bool_rejects_garbage():
    with pytest.raises(SystemExit):
        ConfigManager.get_bool(_config("[settings]\ndry_run = ture\n"), "settings", "dry_run")


def _read(tmp_path, body):
    path = tmp_path / "config.php"
    path.write_text("<?php\nunset($CFG);\nglobal $CFG;\n$CFG = new stdClass();\n" + body)
    return ConfigManager.read_moodle_config(str(path))


def test_read_moodle_config_single_quotes(tmp_path):
    cfg = _read(tmp_path, "$CFG->dbhost = 'localhost';\n$CFG->dbname = 'moodle';\n"
                          "$CFG->dbuser = 'moodleuser';\n$CFG->dbpass = 'p@ss';\n")
    assert cfg == {"dbhost": "localhost", "dbname": "moodle", "dbuser": "moodleuser", "dbpass": "p@ss"}


def test_read_moodle_config_double_quotes_empty_and_escapes(tmp_path):
    cfg = _read(tmp_path, '$CFG->dbname    = "moodle";\n'
                          "$CFG->dbuser = 'it\\'s';\n"
                          '$CFG->dbpass = "a\\"b\\\\c\\$d";\n'
                          "$CFG->dbhost = '';\n")
    assert cfg["dbname"] == "moodle"
    assert cfg["dbuser"] == "it's"
    assert cfg["dbpass"] == 'a"b\\c$d'
    assert cfg["dbhost"] == ""


def test_read_moodle_config_ignores_commented_lines(tmp_path):
    cfg = _read(tmp_path, "// $CFG->dbpass = 'old';\n$CFG->dbpass = 'new';\n")
    assert cfg["dbpass"] == "new"
    assert cfg["dbname"] is None


def test_read_moodle_config_missing_file(tmp_path):
    assert ConfigManager.read_moodle_config(str(tmp_path / "nope.php")) == {}


def test_config_differences_compare_keys_only(tmp_path, caplog):
    config = tmp_path / "config.ini"
    template = tmp_path / "config_template.ini"
    config.write_text("[settings]\nrepo = https://example.org/mine\nold_key = 1\n")
    template.write_text("[settings]\nrepo = https://github.com/BLC-FHGR/moodle\nphp_cli_user =\n")
    ConfigManager.check_config_differences(str(config), str(template))
    assert "missing from config.ini (see config_template.ini): php_cli_user" in caplog.text
    assert "does not know: old_key" in caplog.text
    assert "repo" not in caplog.text


# --- M01: comments, repeated assignments, escapes, dynamic values ---

def test_block_comments_are_ignored(tmp_path):
    cfg = _read(tmp_path, "/*\n$CFG->dbname = 'commented';\n*/\n$CFG->dbname = 'real';\n"
                          "$CFG->dbuser = 'u'; # $CFG->dbuser = 'x';\n")
    assert cfg["dbname"] == "real"
    assert cfg["dbuser"] == "u"


def test_last_assignment_wins_like_php(tmp_path):
    cfg = _read(tmp_path, "$CFG->dbname = 'first';\n$CFG->dbname = 'second';\n")
    assert cfg["dbname"] == "second"


def test_comment_markers_inside_strings_are_kept(tmp_path):
    cfg = _read(tmp_path, "$CFG->dbpass = 'a//b#c/*d*/';\n")
    assert cfg["dbpass"] == "a//b#c/*d*/"


def test_double_quoted_escapes(tmp_path):
    cfg = _read(tmp_path, '$CFG->dbpass = "a\\nb\\t\\x41\\101\\u{42}\\q";\n')
    assert cfg["dbpass"] == "a\nb\tAAB\\q"


@pytest.mark.parametrize("value", ['getenv("DBPASS")', '"pre$suffix"', "DB_PASS"])
def test_dynamic_values_are_refused(tmp_path, value):
    assert _read(tmp_path, f"$CFG->dbpass = {value};\n")["dbpass"] is None
