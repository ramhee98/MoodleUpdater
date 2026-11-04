import subprocess
import sys
import types

import pytest

from modules.service_manager import ServiceManager


class FakePackage:
    def __init__(self, installed):
        self.is_installed = installed


def _manager(monkeypatch, installed, dry_run=False):
    manager = ServiceManager(dry_run=dry_run)
    manager._cache = {name: FakePackage(True) for name in installed}
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: calls.append(cmd))
    return manager, calls


def test_apt_is_not_loaded_until_needed(monkeypatch):
    monkeypatch.setitem(sys.modules, "apt", types.SimpleNamespace(Cache=lambda: pytest.fail("loaded too early")))
    ServiceManager()


def test_database_restart_only_touches_mysql_family(monkeypatch):
    manager, calls = _manager(monkeypatch, ["mariadb-server", "redis-server", "postgresql", "mongodb"])
    manager.restart_database("restart")
    assert calls == [["systemctl", "restart", "mariadb"]]


def test_unknown_packages_do_not_raise(monkeypatch):
    manager, calls = _manager(monkeypatch, [])
    manager.restart_webserver("stop")
    manager.restart_database("restart")
    assert calls == []


def test_prefers_apache_then_nginx(monkeypatch):
    manager, calls = _manager(monkeypatch, ["nginx"])
    manager.restart_webserver("stop")
    assert calls == [["systemctl", "stop", "nginx"]]


def test_success_message_uses_past_tense(monkeypatch, caplog):
    manager, _ = _manager(monkeypatch, ["apache2"])
    with caplog.at_level("INFO"):
        manager.restart_webserver("stop")
    assert "apache2 service stopped successfully." in caplog.text
