"""Stand-in for python3-apt in tests: FAKE_APT_INSTALLED lists the installed packages."""
import os


class _Package:
    def __init__(self, installed):
        self.is_installed = installed


class Cache(dict):
    def __init__(self):
        super().__init__()
        for name in filter(None, os.environ.get("FAKE_APT_INSTALLED", "").split(",")):
            self[name] = _Package(True)
