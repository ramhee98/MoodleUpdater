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
