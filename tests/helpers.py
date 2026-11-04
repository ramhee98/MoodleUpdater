"""Disposable Moodle site, fake system tools and a runner for the real CLI.

Nothing here touches a real Moodle, database or service: php, systemctl, chown and
the MariaDB clients are replaced by small scripts on PATH that log every call to
FAKE_LOG and keep their state (webserver running, maintenance mode) in FAKE_STATE.
git and rsync are the real tools, working on temporary directories.
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FAKES_DIR = os.path.join(os.path.dirname(__file__), "fakes")

OLD_VERSION = "2024010100.00"
NEW_VERSION = "2025010100.00"

_FAKE_PREAMBLE = textwrap.dedent('''\
    import json, os, sys, time
    def log(*parts):
        with open(os.environ["FAKE_LOG"], "a") as f:
            f.write(json.dumps(list(parts)) + "\\n")
    def state(name, value=None):
        path = os.path.join(os.environ["FAKE_STATE"], name)
        if value is None:
            return open(path).read() if os.path.exists(path) else ""
        with open(path, "w") as f:
            f.write(value)
    def rc(var, default="0"):
        return int(os.environ.get(var, default))
''')

FAKE_TOOLS = {
    # Moodle's PHP CLI scripts, dispatched on the script name.
    "php": '''
script = os.path.basename(sys.argv[1]) if len(sys.argv) > 1 else ""
args = sys.argv[2:]
log("php", script, *args)
if script == "checks.php":
    count = int(state("checks_count") or 0)
    state("checks_count", str(count + 1))
    codes = os.environ.get("FAKE_CHECKS_RC", "0").split(",")
    code = int(codes[min(count, len(codes) - 1)])
    print({0: "OK: All 'status' checks OK", 1: "WARNING: Something (ref)",
           2: "CRITICAL: Something (ref)", 3: "UNKNOWN: Something (ref)"}.get(code, ""))
    if os.environ.get("FAKE_CHECKS_STDERR"):
        print(os.environ["FAKE_CHECKS_STDERR"], file=sys.stderr)
    sys.exit(code)
if script == "maintenance.php":
    enable = args[0] == "--enable"
    code = rc("FAKE_MAINT_ENABLE_RC" if enable else "FAKE_MAINT_DISABLE_RC")
    if code == 0:
        state("maintenance", "on" if enable else "off")
    sys.exit(code)
if script == "upgrade.php":
    log("upgrade-context", state("webserver") or "unknown", state("maintenance") or "off")
    print("== Upgrading core ==")
    code = rc("FAKE_UPGRADE_RC")
    if code:
        print("!! Upgrade failed !!")
    sys.exit(code)
sys.exit(1)
''',
    "systemctl": '''
action, service = sys.argv[1], sys.argv[2]
log("systemctl", action, service)
if action == os.environ.get("FAKE_SIGINT_ON"):
    # Simulate Ctrl-C arriving while this command runs.
    import signal
    os.kill(os.getppid(), signal.SIGINT)
    time.sleep(0.5)
if action in os.environ.get("FAKE_SYSTEMCTL_FAIL", "").split(","):
    sys.exit(1)
state("webserver", "stopped" if action == "stop" else "running")
''',
    "chown": '''
log("chown", *sys.argv[1:])
sys.exit(1 if os.environ.get("FAKE_CHOWN_FAIL") else 0)
''',
    "mariadb-dump": '''
log("mariadb-dump", *sys.argv[1:])
print("-- partial dump of " + sys.argv[-1])
code = rc("FAKE_DUMP_RC")
if code:
    print("mariadb-dump: Lost connection", file=sys.stderr)
    sys.exit(code)
print("-- dump complete")
''',
    "mariadb-show": '''
log("mariadb-show", *sys.argv[1:])
sys.exit(rc("FAKE_SHOW_RC"))
''',
    "mariadb": '''
log("mariadb", *sys.argv[1:])
time.sleep(float(os.environ.get("FAKE_MYSQL_SLEEP", "0")))
if os.environ.get("FAKE_MYSQL_FAIL"):
    print("ERROR 1045: Access denied (password: " + os.environ.get("MYSQL_PWD", "") + ")", file=sys.stderr)
    sys.exit(1)
print("db\\t100.00")
''',
}


def make_fakebin(directory):
    """Write the fake tools into directory and return it."""
    os.makedirs(directory, exist_ok=True)
    for name, body in FAKE_TOOLS.items():
        path = os.path.join(directory, name)
        with open(path, "w") as f:
            f.write(f"#!{sys.executable}\n{_FAKE_PREAMBLE}{body}")
        os.chmod(path, 0o755)
    return directory


def git(*args, cwd=None):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
                   cwd=cwd, check=True, capture_output=True)


def _version_php(version):
    return f"<?php\n$version  = {version};\n$release  = '{version[:4]} test';\n"


def make_upstream(path, versions=(OLD_VERSION, NEW_VERSION), extra=None):
    """A Moodle-like repository on branch MOODLE_TEST with one commit per version.
    Returns the hash of each commit. extra(repo_path) can add files to the last commit."""
    os.makedirs(path)
    git("init", "-q", "-b", "MOODLE_TEST", cwd=path)
    for name in ("checks.php", "upgrade.php", "maintenance.php"):
        _write(os.path.join(path, "admin", "cli", name), "<?php // handled by the fake php\n")
    _write(os.path.join(path, "mod", "forum", "version.php"), "<?php\n")
    commits = []
    for index, version in enumerate(versions):
        _write(os.path.join(path, "version.php"), _version_php(version))
        if extra and index == len(versions) - 1:
            extra(path)
        git("add", "-A", cwd=path)
        git("commit", "-q", "-m", f"version {version}", cwd=path)
        commits.append(subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True,
                                      text=True, check=True).stdout.strip())
    return commits


def _write(path, content, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    if mode is not None:
        os.chmod(path, mode)


CONFIG_PHP = "<?php\n$CFG->dbname = 'moodle';\n$CFG->dbuser = 'moodleuser';\n$CFG->dbpass = 's3cret';\n"


class Site:
    """A disposable installation: <root>/app (copy of the updater), <root>/www/moodle
    (live Moodle at OLD_VERSION with config.php and a third-party plugin),
    <root>/upstream (repository whose MOODLE_TEST branch is at NEW_VERSION)."""

    def __init__(self, root, upstream_extra=None, upstream_versions=(OLD_VERSION, NEW_VERSION)):
        self.root = str(root)
        self.app = os.path.join(self.root, "app")
        self.www = os.path.join(self.root, "www")
        self.live = os.path.join(self.www, "moodle")
        self.upstream = os.path.join(self.root, "upstream")
        self.backups = os.path.join(self.root, "backups")
        self.dumps = os.path.join(self.root, "dumps")
        self.state = os.path.join(self.root, "state")
        self.fake_log = os.path.join(self.root, "fake.log")
        self.lock_dir = os.path.join(self.root, "lock")
        self.fakebin = make_fakebin(os.path.join(self.root, "fakebin"))
        os.makedirs(self.state)
        open(self.fake_log, "w").close()

        # The updater itself, as it would be installed.
        os.makedirs(self.app)
        shutil.copy(os.path.join(REPO_ROOT, "moodle_updater.py"), self.app)
        shutil.copy(os.path.join(REPO_ROOT, "config_template.ini"), self.app)
        shutil.copytree(os.path.join(REPO_ROOT, "modules"), os.path.join(self.app, "modules"),
                        ignore=shutil.ignore_patterns("__pycache__"))

        commits = make_upstream(self.upstream, upstream_versions, upstream_extra)
        # Live site: the old version, plus things only a real install has.
        os.makedirs(self.www)
        git("clone", "-q", self.upstream, self.live)
        git("checkout", "-q", commits[0], cwd=self.live)
        _write(os.path.join(self.live, "config.php"), CONFIG_PHP, 0o640)
        _write(os.path.join(self.live, "local", "custom", "version.php"), "<?php // third-party\n")
        self.write_config()

    def write_config(self, **overrides):
        settings = {
            "dry_run": "False", "auto_update_script": "False", "repo": self.upstream,
            "branch": "MOODLE_TEST", "path": self.www, "moodle": "moodle",
            "chown_user": str(os.getuid()), "chown_group": str(os.getgid()),
            "folder_backup_path": self.backups, "db_dump_path": self.dumps,
        }
        settings.update(overrides)
        lines = ["[settings]"] + [f"{k} = {v}" for k, v in settings.items()]
        lines += ["[database]", "read_db_from_config = True",
                  "[logging]", "log_to_console = True", "log_to_file = True",
                  f"log_file_path = {os.path.join(self.app, 'moodle_updater.log')}", "log_level = INFO"]
        _write(os.path.join(self.app, "config.ini"), "\n".join(lines) + "\n")

    def run(self, *args, timeout=120, input=None, **fake_env):
        """Run the real CLI (root check patched out) and return the CompletedProcess.
        input answers the interactive prompts; without it stdin is closed."""
        env = {k: v for k, v in os.environ.items() if not k.startswith(("FAKE_", "GIT_"))}
        env.update({
            "PATH": self.fakebin + os.pathsep + os.environ["PATH"],
            "PYTHONPATH": FAKES_DIR,
            "PYTHONDONTWRITEBYTECODE": "1",
            "FAKE_LOG": self.fake_log, "FAKE_STATE": self.state,
            "FAKE_APT_INSTALLED": "apache2,mariadb-server",
            "MOODLE_UPDATER_LOCK_DIR": self.lock_dir,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        })
        env.update({k: str(v) for k, v in fake_env.items()})
        wrapper = ("import sys, runpy\n"
                   "import modules.application_setup as setup\n"
                   "setup.running_as_root = lambda: True\n"
                   "sys.argv = ['moodle_updater.py'] + sys.argv[1:]\n"
                   "runpy.run_path('moodle_updater.py', run_name='__main__')\n")
        return subprocess.run([sys.executable, "-c", wrapper, *args], cwd=self.app, env=env,
                              **({"input": input} if input is not None else {"stdin": subprocess.DEVNULL}),
                              capture_output=True, text=True, timeout=timeout)

    def calls(self, tool=None):
        with open(self.fake_log) as f:
            entries = [json.loads(line) for line in f if line.strip()]
        return [e for e in entries if tool is None or e[0] == tool]

    def live_version(self):
        with open(os.path.join(self.live, "version.php")) as f:
            content = f.read()
        return NEW_VERSION if NEW_VERSION in content else OLD_VERSION if OLD_VERSION in content else content

    def leftovers(self):
        """Staging/old trees and in-progress backups that should never survive a run."""
        found = [n for n in os.listdir(self.www) if n.startswith(".moodle_")]
        if os.path.isdir(self.backups):
            found += [n for n in os.listdir(self.backups) if "inprogress" in n]
        if os.path.isdir(self.dumps):
            found += [n for n in os.listdir(self.dumps) if n.endswith(".partial.sql")]
        return found


def snapshot(root, exclude=()):
    """Path -> (type, mode, size, mtime, content hash) for everything below root."""
    result = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            if rel in exclude:
                continue
            st = os.lstat(path)
            digest = None
            if stat.S_ISREG(st.st_mode):
                with open(path, "rb") as f:
                    digest = hashlib.sha256(f.read()).hexdigest()
            result[rel] = (stat.S_IFMT(st.st_mode), stat.S_IMODE(st.st_mode), st.st_size, st.st_mtime_ns, digest)
    return result
