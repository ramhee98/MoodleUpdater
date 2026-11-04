import os
import queue
import stat
import threading
import time
import logging
import shutil
import subprocess
from modules.application_setup import ApplicationSetup
from modules.config_manager import SEPARATOR
from modules.db_tools import sanitize_db_output, db_host_args, resolve_db_tool
from modules.git_manager import git_environment, git_stdin
from modules.moodle_version import MoodleVersionChecker
from modules.system_monitor import SystemMonitor

# Default mode for config.php when the original's mode is unknown: owner rw, group r.
DEFAULT_CONFIG_PHP_MODE = 0o640


def is_within(path, parent):
    """True if path is parent itself or lies below it (symlinks resolved)."""
    path, parent = os.path.realpath(path), os.path.realpath(parent)
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def _tree_size(path):
    """Total size in bytes of the files below path. Symlinks count as themselves
    (lstat), so dangling links don't raise; unreadable entries are skipped."""
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _unique_path(path):
    """path, or path with a -1, -2, ... suffix before the extension if it exists."""
    base, ext = os.path.splitext(path)
    candidate, counter = path, 1
    while os.path.lexists(candidate):
        candidate = f"{base}-{counter}{ext}"
        counter += 1
    return candidate


class MoodleBackupManager:
    """Manages directory backups, database dumps, and Git clone operations for Moodle."""

    def __init__(self, path, moodle, folder_backup_path, dry_run=False, non_interactive=False, php_cli_user=None,
                 db_dump_path=None):
        self.path = path
        self.moodle = moodle
        self.folder_backup_path = folder_backup_path
        self.dry_run = dry_run
        self.non_interactive = non_interactive
        # Run Moodle's PHP CLI scripts as this user instead of root (None: current user)
        self.php_cli_user = php_cli_user or None
        # Configured dump location; kept out of full backups whether or not this run dumps
        self.db_dump_path = db_dump_path
        # Folder written by dir_backup() in this run (None until it succeeds)
        self.backup_folder = None
        # Dump file written in this run (None until it succeeds)
        self.dump_file = None
        # Staging directory holding the prepared clone until commit_clone() swaps it in
        self.staged_clone = None
        # Set when a failed swap could not put the previous code back
        self.live_tree_missing = False
        self.runtime_backup = None
        self.runtime_dump = None
        self.runtime_clone = None
        self.runtime_cliupgrade = None
        # Track submodule sync results
        self.submodules_success = 0
        self.submodules_failed = 0
        self.failed_submodules = []
        # Track upgrade failure
        self.upgrade_failed = False
        self.upgrade_error_details = []
        self.maintenance_disable_failed = False
        # Track plugin restore results
        self.runtime_restore_plugins = None
        self.restored_plugins = []
        self.skipped_plugins = []

    @property
    def clone_path(self):
        """The live Moodle code directory."""
        return os.path.join(self.path, self.moodle)

    # Well-known plugin parent directories, used only to detect the Moodle 5.x
    # "public dir" layout (see _find_code_root). Plugin *discovery* no longer
    # relies on this list — it walks the tree for version.php instead — so this
    # only needs a few entries that reliably exist in every Moodle install.
    PLUGIN_PARENTS = [
        'admin/tool',
        'auth',
        'blocks',
        'enrol',
        'filter',
        'mod',
        'repository',
        'theme',
    ]

    # Directories never descended into while discovering plugins: VCS metadata
    # and bundled dependency trees, which can hold stray version.php files that
    # are not Moodle plugins.
    PLUGIN_WALK_SKIP_DIRS = {'.git', 'node_modules', 'vendor'}

    def dir_backup(self, full_backup):
        """Handle directory backups using rsync. Returns True on success."""
        start = time.time()

        backup_type = "full" if full_backup else "partial"
        timestamp = time.strftime('%Y-%m-%d-%H-%M-%S')
        source_path = os.path.join(self.path, '') if full_backup else os.path.join(self.path, self.moodle, '')
        name = f"{self.moodle}_bak_{backup_type}_{timestamp}"
        backup_folder = os.path.join(self.folder_backup_path, name)

        exclude_args = [
            '--exclude', 'moodledata/cache',
            '--exclude', 'moodledata/localcache',
            '--exclude', 'moodledata/sessions',
            '--exclude', 'moodledata/temp',
            '--exclude', 'moodledata/trashdir',
            *self._own_output_excludes(source_path),
        ] if full_backup else []

        logging.info(f"Starting {backup_type} backup from {source_path} to {backup_folder}")

        if self.dry_run:
            logging.info(f"[Dry Run] Would run: rsync -a {' '.join(exclude_args)} {source_path} {backup_folder}")
            self.backup_folder = backup_folder
            self.runtime_backup = 0
            return True

        # The backup holds config.php (DB password) and user files. rsync -a gives
        # the destination root the source root's mode (typically 0755), so copy into
        # a private 0700 container that rsync never touches, and move the finished
        # backup out of it only after it has been made private itself.
        container = os.path.join(self.folder_backup_path, f"{self.moodle}_bak_inprogress_{timestamp}")
        staged = os.path.join(container, name)
        success = False
        try:
            os.mkdir(container, 0o700)
            os.chmod(container, 0o700)
            # -a keeps symlinks, permissions, ownership and timestamps so the
            # backup can be restored as-is (-r alone silently skips symlinks).
            subprocess.run(['rsync', '-a', *exclude_args, source_path, staged], check=True)
            os.chmod(staged, 0o700)
            os.rename(staged, backup_folder)
            os.rmdir(container)
            success = True
            size = _tree_size(backup_folder)
            logging.info(f"Backup completed and saved in {backup_folder} - ({size / (1024 * 1024 * 1024):.2f} GB)")
            self.backup_folder = backup_folder
        except subprocess.CalledProcessError as e:
            logging.error(f"Backup failed: rsync exited with code {e.returncode}.")
        except OSError as e:
            logging.error(f"Backup failed: {e}")
        finally:
            # A partial backup is useless and holds secrets: never leave one behind.
            if not success and os.path.isdir(container) and not os.path.islink(container):
                logging.warning(f"Removing incomplete backup {container}")
                shutil.rmtree(container, ignore_errors=True)

        self.runtime_backup = int(time.time() - start)
        return success

    def _own_output_excludes(self, source_path):
        """rsync excludes for this tool's own backups, dumps and lock file when they
        live inside the backup source, so a full backup never copies earlier
        backups or the dump that is being written in parallel."""
        excludes = []
        outputs = [(self.folder_backup_path, [f"{self.moodle}_bak_*", f".moodle_updater_{self.moodle}.lock"])]
        if self.db_dump_path:
            outputs.append((self.db_dump_path, ["*.sql"]))
        for location, patterns in outputs:
            if not is_within(location, source_path):
                continue
            rel = os.path.relpath(os.path.realpath(location), os.path.realpath(source_path))
            prefix = "/" if rel == "." else f"/{rel}/"
            for pattern in patterns:
                # A leading "/" anchors the pattern at the root of the transfer.
                excludes += ['--exclude', prefix + pattern]
        return excludes

    def db_dump(self, dbname, dbuser, dbpass, verbose, db_dump_path, dbhost=None):
        """Perform database dump using mysqldump with progress monitoring. Returns True on success."""
        start = time.time()

        timestamp = time.strftime('%Y-%m-%d-%H-%M-%S')
        dump_file = os.path.join(db_dump_path, f"{dbname}_{timestamp}.sql")
        # Written under a hidden temporary name and renamed once complete, so a
        # failed dump can neither look like a backup nor clobber an existing one.
        partial_file = os.path.join(db_dump_path, f".{dbname}_{timestamp}.partial.sql")
        # Pass the DB password via the MYSQL_PWD environment variable rather
        # than as a -p<pw> CLI argument: CLI args are visible in `ps` output
        # and shell history, env vars are not.
        dump_args = [
            resolve_db_tool('mariadb-dump', 'mysqldump'), '-u', dbuser, *db_host_args(dbhost),
            '--single-transaction', '--skip-lock-tables',
            '--max_allowed_packet=100M', '--quick', '--databases', dbname
        ]
        dump_env = {**os.environ, 'MYSQL_PWD': dbpass}

        if verbose:
            dump_args.append('--verbose')

        logging.info(f"Starting database dump for {dbname} to {dump_file}")

        if self.dry_run:
            # No monitoring either: it would connect to the database.
            logging.info(f"[Dry Run] Would run: {' '.join(dump_args)} (with MYSQL_PWD set)")
            self.runtime_dump = 0
            return True

        # Initialize SystemMonitor
        monitor = SystemMonitor()

        # Start monitoring during database dump
        monitor.start_monitoring(partial_file, dbname, dbuser, dbpass, dbhost)

        created = completed = False
        try:
            os.makedirs(db_dump_path, exist_ok=True)
            # The dump contains every user record and password hash: 0600. O_EXCL and
            # O_NOFOLLOW refuse to reuse or follow anything already at that name.
            fd = os.open(partial_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created = True
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as dump:
                result = subprocess.run(dump_args, stdout=dump, stderr=subprocess.PIPE, text=True, check=True, env=dump_env)
            sanitized_stderr = sanitize_db_output(result.stderr, dbpass)
            if sanitized_stderr:
                logging.warning(f"mysqldump warning: {sanitized_stderr}")
            dump_file = _unique_path(dump_file)
            os.rename(partial_file, dump_file)
            completed = True
            self.dump_file = dump_file
            logging.info(f"Database dump saved in {dump_file} - ({os.path.getsize(dump_file) / (1024 * 1024 * 1024):.2f} GB)")
        except OSError as error:
            logging.error(f"Database dump to {dump_file} failed: {error}")
            return False
        except subprocess.CalledProcessError as e:
            logging.error(f"Database dump failed: {sanitize_db_output(e.stderr, dbpass)}")
            return False
        finally:
            # Stop monitoring
            monitor.stop_monitoring()
            # Remove the incomplete dump we created (also on Ctrl-C), never a file
            # that was already there.
            if created and not completed and os.path.lexists(partial_file):
                logging.warning(f"Removing incomplete dump file {partial_file}")
                os.remove(partial_file)

        self.runtime_dump = int(time.time() - start)
        return True

    def _git_run(self, args, **kwargs):
        """Run git without ever prompting in non-interactive mode."""
        return subprocess.run(['git', *args], env=git_environment(self.non_interactive),
                              stdin=git_stdin(self.non_interactive), **kwargs)

    def prepare_clone(self, config_php, repository, branch, sync_submodules, restore_submodules_from_backup=False,
                      full_backup=False, config_php_mode=DEFAULT_CONFIG_PHP_MODE):
        """Build the new code tree in a private staging directory next to the live
        code: clone, checkout, submodules, config.php and a downgrade check. Nothing
        live is touched; commit_clone() swaps the tree in later. Returns True on
        success, with the tree in self.staged_clone. config_php None skips config.php."""
        start = time.time()
        timestamp = time.strftime('%Y-%m-%d-%H-%M-%S')
        # Sibling of the live code, so the final swap is a rename on the same filesystem.
        staging_path = os.path.join(self.path, f".{self.moodle}_new_{timestamp}")
        self.staged_clone = None

        if self.dry_run:
            logging.info(f"[Dry Run] Would clone repository: {repository} to {staging_path}")
            logging.info(f"[Dry Run] Would checkout branch: {branch} in {staging_path}")
            if sync_submodules:
                logging.info(f"[Dry Run] Would sync and update git submodules in {staging_path}")
            elif restore_submodules_from_backup:
                backup_root = self._backup_moodle_root(full_backup)
                for submodule in (self._read_submodule_paths(backup_root) or []) if backup_root else []:
                    logging.info(f"[Dry Run] Would restore submodule {submodule} from backup {backup_root} to {staging_path}")
            if config_php is not None:
                logging.info(f"[Dry Run] Would create config.php (mode {config_php_mode:o}) in {staging_path}")
            logging.info("[Dry Run] Would refuse the clone if it is older than the installed version")
            self.staged_clone = staging_path
            self.runtime_clone = 0
            return True

        try:
            # Private until the swap: the staged tree soon contains config.php.
            os.mkdir(staging_path, 0o700)
            os.chmod(staging_path, 0o700)
        except OSError as e:
            logging.error(f"Could not create staging directory {staging_path}: {e}")
            return False

        try:
            ok = self._build_staging(staging_path, config_php, repository, branch, sync_submodules,
                                     restore_submodules_from_backup, full_backup, config_php_mode)
        except BaseException:
            self._discard(staging_path)
            raise
        if not ok:
            logging.error("The existing Moodle code was left untouched.")
            self._discard(staging_path)
            return False

        self.staged_clone = staging_path
        self.runtime_clone = int(time.time() - start)
        logging.info(f"New code prepared in {staging_path} in {self.runtime_clone} seconds.")
        return True

    def _build_staging(self, staging_path, config_php, repository, branch, sync_submodules,
                       restore_submodules_from_backup, full_backup, config_php_mode):
        """Fill the staging directory. Returns False (after logging why) on any failure."""
        try:
            # Cloning into the existing, empty, private directory keeps its 0700 mode.
            self._git_run(['clone', repository, staging_path], check=True)
        except subprocess.CalledProcessError as e:
            logging.error(f"Git clone failed (exit code {e.returncode}).")
            return False
        try:
            self._git_run(['-C', staging_path, 'checkout', branch], check=True)
        except subprocess.CalledProcessError as e:
            logging.error(f"Git checkout of {branch} failed (exit code {e.returncode}).")
            return False

        if sync_submodules:
            if not self._sync_submodules(staging_path):
                return False
        elif restore_submodules_from_backup:
            if not self._restore_submodules(staging_path, full_backup):
                return False

        if config_php is None:
            logging.warning(f"No config.php provided; {self.clone_path} will not contain a config.php.")
        else:
            try:
                self._write_config_php(staging_path, config_php, config_php_mode)
            except OSError as e:
                logging.error(f"Writing config.php failed: {e}.")
                return False

        return self._check_not_downgrade(staging_path)

    def _sync_submodules(self, staging_path):
        """Sync and update every submodule. Any failure fails the clone: a missing
        submodule usually means a missing plugin, which must not go live."""
        try:
            self._git_run(['submodule', 'sync', '--recursive'], cwd=staging_path, check=True)
        except subprocess.CalledProcessError as e:
            logging.error(f"Git submodule sync failed (exit code {e.returncode}).")
            return False

        # Get list of submodules and update each individually
        result = self._git_run(['submodule', 'status'], cwd=staging_path, capture_output=True, text=True)
        if result.returncode != 0:
            logging.error(f"Listing git submodules failed (exit code {result.returncode}): {result.stderr.strip()}")
            return False

        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            submodule_path = line.split()[1]
            try:
                self._git_run(['submodule', 'update', '--init', '--recursive', '--remote', '--', submodule_path],
                              cwd=staging_path, check=True)
                logging.info(f"Updated submodule {submodule_path} with remote tracking branch")
                self.submodules_success += 1
            except subprocess.CalledProcessError as e:
                logging.error(f"Git submodule update failed for {submodule_path} (exit code {e.returncode}).")
                self.submodules_failed += 1
                self.failed_submodules.append(submodule_path)

        # Log brief summary
        total = self.submodules_success + self.submodules_failed
        if total > 0:
            logging.info(f"Submodule sync complete: {self.submodules_success}/{total} succeeded, {self.submodules_failed}/{total} failed")
        if self.failed_submodules:
            logging.error(f"Not using the new clone because these submodules failed: {', '.join(self.failed_submodules)}")
            return False
        return True

    def _restore_submodules(self, staging_path, full_backup):
        """Copy the submodules listed in this run's backup into the staged tree."""
        backup_folder = self._backup_moodle_root(full_backup)
        if backup_folder is None:
            return False
        submodule_paths = self._read_submodule_paths(backup_folder)
        if submodule_paths is None:
            return False

        failed = []
        for submodule in submodule_paths:
            logging.info(f"Restoring submodule {submodule} from backup {backup_folder} to {staging_path}")
            try:
                self._copy_into(os.path.join(backup_folder, submodule), staging_path, submodule)
            except (OSError, shutil.Error) as e:
                logging.error(f"Restoring submodule {submodule} from backup failed: {e}")
                failed.append(submodule)
        if failed:
            logging.error(f"Restoring submodules from backup failed for: {', '.join(failed)}.")
            return False
        return True

    @staticmethod
    def _copy_into(src, root, rel_path):
        """Copy the directory src to <root>/<rel_path>, merging into an existing
        (e.g. empty submodule) directory. The target must resolve inside root: the
        cloned repository could contain symlinks pointing elsewhere."""
        dst = os.path.join(root, rel_path)
        if not os.path.isdir(src):
            raise OSError(f"{src} does not exist in the backup")
        if not is_within(dst, root):
            raise OSError(f"{dst} resolves outside {root} (symlink in the new tree?)")
        shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)

    @staticmethod
    def _write_config_php(directory, content, mode):
        """Create <directory>/config.php with the given mode. Whatever the cloned
        repository put at that name is removed first, so a symlink is never followed
        (it could point to any file root can write)."""
        path = os.path.join(directory, 'config.php')
        if os.path.lexists(path):
            if os.path.isdir(path) and not os.path.islink(path):
                raise OSError(f"{path} is a directory in the new tree")
            logging.warning(f"The cloned tree contains its own {path}; replacing it.")
            os.unlink(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w') as config_file:
            config_file.write(content)

    def _check_not_downgrade(self, staging_path):
        """Refuse a clone whose version.php is older than the installed one. This
        reads the checked-out tree, so it holds whatever the remote HTTP check said.
        Unknown versions only produce a warning."""
        live_release, live_build = MoodleVersionChecker(self.clone_path, None, None).get_local_version()
        new_release, new_build = MoodleVersionChecker(staging_path, None, None).get_local_version()
        try:
            live_version, new_version = float(live_build), float(new_build)
        except ValueError:
            logging.warning(f"Cannot compare versions (installed: {live_build}, new clone: {new_build}); "
                            "a downgrade cannot be ruled out. Proceeding.")
            return True
        if new_version < live_version:
            logging.critical(f"The new clone ({new_release} - Version: {new_build}) is older than the installed "
                             f"Moodle ({live_release} - Version: {live_build}). Refusing to downgrade.")
            return False
        logging.info(f"New clone version {new_release} ({new_build}), installed {live_release} ({live_build}).")
        return True

    def commit_clone(self, chown_user, chown_group):
        """Set ownership on the staged tree and swap it in for the live code. On
        failure the previous code stays (or is put back). Returns True on success."""
        staging_path = self.staged_clone
        if staging_path is None:
            logging.error("No prepared clone to put in place.")
            return False
        clone_path = self.clone_path

        if self.dry_run:
            logging.info(f"[Dry Run] Would set ownership of {staging_path} to {chown_user}:{chown_group}.")
            logging.info(f"[Dry Run] Would replace {clone_path} with {staging_path}")
            self.staged_clone = None
            return True

        # Ownership is set before the swap, so a failure (e.g. an unknown user)
        # leaves the previous code live.
        try:
            subprocess.run(['chown', '-R', f'{chown_user}:{chown_group}', staging_path], check=True)
        except subprocess.CalledProcessError as e:
            logging.error(f"Setting folder ownership failed (exit code {e.returncode}). The existing Moodle code was left untouched.")
            self.discard_clone()
            return False

        old_path = os.path.join(self.path, f".{self.moodle}_old_{time.strftime('%Y-%m-%d-%H-%M-%S')}")
        ok = self._swap_in(staging_path, clone_path, old_path)
        self.staged_clone = None
        if ok:
            logging.info(f"New Moodle code is live in {clone_path}")
        return ok

    def discard_clone(self):
        """Remove a prepared clone that will not be used."""
        if self.staged_clone and not self.dry_run:
            logging.info(f"Discarding the prepared clone {self.staged_clone}")
            self._discard(self.staged_clone)
        self.staged_clone = None

    def git_clone(self, config_php, repository, branch, sync_submodules, chown_user, chown_group,
                  restore_submodules_from_backup=False, full_backup=False, config_php_mode=DEFAULT_CONFIG_PHP_MODE):
        """Prepare a clone and swap it in right away. Returns True on success."""
        if not self.prepare_clone(config_php, repository, branch, sync_submodules,
                                  restore_submodules_from_backup, full_backup, config_php_mode):
            return False
        return self.commit_clone(chown_user, chown_group)

    def _backup_moodle_root(self, full_backup):
        """Return the Moodle code directory inside the backup made in this run.

        Restores must use this run's backup, never an older one found on disk.
        In dry-run mode nothing was copied, so the live install stands in for it."""
        if not self.backup_folder:
            logging.error("No directory backup was made in this run; cannot restore from backup.")
            return None
        # A full backup copies the parent folder, so the code lives in <backup>/<moodle>.
        root = os.path.join(self.backup_folder, self.moodle) if full_backup else self.backup_folder
        if self.dry_run and not os.path.isdir(root):
            live = self.clone_path
            logging.info(f"[Dry Run] Backup {root} does not exist; reading the live install {live} instead.")
            return live
        return root

    @staticmethod
    def _read_submodule_paths(moodle_root):
        """Return the submodule paths listed in <moodle_root>/.gitmodules ([] if
        there is none), or None if the file exists but cannot be read."""
        gitmodules = os.path.join(moodle_root, '.gitmodules')
        if not os.path.isfile(gitmodules):
            return []
        result = subprocess.run(['git', 'config', '--file', gitmodules, '--get-regexp', r'\.path$'],
                                capture_output=True, text=True)
        if result.returncode == 1:  # file exists but defines no submodules
            return []
        if result.returncode != 0:
            logging.error(f"Failed to read submodules from {gitmodules}: {result.stderr.strip()}")
            return None
        return [line.split(maxsplit=1)[1] for line in result.stdout.splitlines() if line.strip()]

    @staticmethod
    def _discard(path):
        """Remove a half-built staging directory, ignoring errors."""
        shutil.rmtree(path, ignore_errors=True)

    def _swap_in(self, staging_path, clone_path, old_path):
        """Replace clone_path with staging_path. The old tree is moved aside first
        and put back if the swap fails, then removed. Returns True on success."""
        # The staged tree was private while it was built; give it the live tree's mode.
        try:
            live_mode = stat.S_IMODE(os.stat(clone_path).st_mode) if os.path.isdir(clone_path) else 0o755
            os.chmod(staging_path, live_mode)
        except OSError as e:
            logging.error(f"Could not prepare {staging_path} for the swap: {e}. The existing Moodle code was left untouched.")
            self._discard(staging_path)
            return False

        moved_old = False
        if os.path.lexists(clone_path):
            try:
                os.rename(clone_path, old_path)
                moved_old = True
            except OSError as e:
                logging.error(f"Could not move the existing {clone_path} aside: {e}. "
                              "The existing Moodle code was left untouched. (Is it a mount point?)")
                self._discard(staging_path)
                return False
        try:
            os.rename(staging_path, clone_path)
        except OSError as e:
            logging.error(f"Could not move the new clone into {clone_path}: {e}. Restoring the previous code.")
            if moved_old:
                try:
                    os.rename(old_path, clone_path)
                except OSError as restore_error:
                    self.live_tree_missing = True
                    logging.critical(
                        f"Could not restore the previous code either: {restore_error}. {clone_path} is MISSING. "
                        f"The previous code is intact at {old_path}; restore it manually with: mv '{old_path}' '{clone_path}'")
            # The staged tree holds a copy of config.php; never leave it lying around.
            self._discard(staging_path)
            return False
        if moved_old:
            try:
                shutil.rmtree(old_path)
            except OSError as e:
                logging.warning(f"Could not remove the previous code at {old_path}: {e}")
        return True

    def dir_backup_and_prepare_clone(self, config_php, full_backup, repo, branch, sync_submodules,
                                     restore_submodules_from_backup=False, config_php_mode=DEFAULT_CONFIG_PHP_MODE):
        """Perform directory backup followed by preparing the clone. The clone will
        replace the backed-up code, so it is only prepared when the backup succeeded."""
        logging.info("Starting directory backup and git clone process.")
        if not self.dir_backup(full_backup):
            logging.error("Skipping git clone because the directory backup failed. The existing Moodle code was left untouched.")
            return False
        return self.prepare_clone(config_php, repo, branch, sync_submodules, restore_submodules_from_backup,
                                  full_backup, config_php_mode)

    def _find_code_root(self, moodle_root):
        """Return the dir holding Moodle's code tree.

        Moodle 5.x splits the codebase: source lives under <root>/public/, with
        composer.json and friends at <root>/. Earlier versions keep everything at
        <root>/. Detect by checking where the plugin parents actually exist.
        """
        candidates = [moodle_root, os.path.join(moodle_root, 'public')]
        for candidate in candidates:
            if not os.path.isdir(candidate):
                continue
            for parent in self.PLUGIN_PARENTS:
                if os.path.isdir(os.path.join(candidate, parent)):
                    return candidate
        return moodle_root

    def restore_plugins(self, chown_user, chown_group, full_backup=False, selection_mode="auto", target_root=None):
        """Copy third-party plugins present in this run's directory backup but missing from the new code.

        target_root is the tree to restore into (normally the staged clone, before
        it goes live); None means the live code. selection_mode "auto" restores
        every missing plugin; "manual" prompts y/n for each discovered plugin before
        copying it. Returns True unless a plugin could not be restored.
        """
        start = time.time()
        logging.info("Starting plugin restore from backup.")

        target = target_root or self.clone_path
        if self.dry_run and not os.path.isdir(target):
            logging.info(f"[Dry Run] {target} does not exist; comparing against the live install {self.clone_path} instead.")
            target = self.clone_path

        # Use the backup made in this run (in a full backup the code lives under <backup>/<moodle>/).
        backup_moodle_root = self._backup_moodle_root(full_backup)
        if backup_moodle_root is None:
            return False

        # Moodle 5.x uses a "public dir" layout where plugins live under public/.
        # Detect which root contains the plugin parents in both the backup and the new clone.
        backup_code_root = self._find_code_root(backup_moodle_root)
        clone_code_root = self._find_code_root(target)
        if backup_code_root != backup_moodle_root or clone_code_root != target:
            logging.info(f"Detected Moodle public-dir layout. Backup code root: {backup_code_root}, clone code root: {clone_code_root}.")

        # Discover plugin candidates structurally: any directory under the code
        # root containing a version.php is a Moodle plugin, regardless of where it
        # lives. This covers every plugin type (including ones added in newer
        # Moodle releases) without maintaining a hardcoded parent-directory list.
        # Subplugins (e.g. mod/quiz/report/*) are nested inside another plugin's
        # tree, so we keep descending and collect every version.php dir.
        candidates = []
        for dirpath, dirnames, filenames in os.walk(backup_code_root):
            # Don't descend into VCS metadata or bundled dependency trees — they
            # never hold Moodle plugins and can contain stray version.php files.
            dirnames[:] = [d for d in dirnames if d not in self.PLUGIN_WALK_SKIP_DIRS]
            if 'version.php' not in filenames:
                continue
            rel_path = os.path.relpath(dirpath, backup_code_root)
            dst = os.path.join(clone_code_root, rel_path)
            if os.path.isfile(os.path.join(dst, 'version.php')):
                # Already in the new clone: a core plugin, or a third-party one
                # that the repo/submodules already provide. Nothing to restore.
                self.skipped_plugins.append(rel_path)
                logging.debug(f"Skipping plugin {rel_path}: already present in new clone.")
                continue
            if os.path.isdir(dst) and os.listdir(dst):
                logging.warning(f"Skipping plugin {rel_path}: {dst} exists without version.php; not merging into it.")
                self.skipped_plugins.append(rel_path)
                continue
            # Missing, or an empty directory such as an uninitialized submodule
            # (submodules are restored too when nothing else supplied them).
            candidates.append((rel_path, dirpath, dst))

        # A missing parent plugin is copied together with its nested subplugins.
        # Drop any candidate whose ancestor is also being restored so we don't
        # copy the same tree twice.
        candidates.sort(key=lambda c: c[0])
        plugins_to_restore = []
        restored_roots = []
        for rel_path, src, dst in candidates:
            if any(rel_path == root or rel_path.startswith(root + os.sep) for root in restored_roots):
                logging.debug(f"Skipping plugin {rel_path}: nested inside {next(r for r in restored_roots if rel_path.startswith(r + os.sep))}, restored with its parent.")
                continue
            restored_roots.append(rel_path)
            plugins_to_restore.append((rel_path, src, dst))

        if not plugins_to_restore:
            logging.info("No missing third-party plugins detected in backup.")
            self.runtime_restore_plugins = int(time.time() - start)
            return True

        # In manual mode let the user pick which discovered plugins to restore.
        if selection_mode == "manual":
            logging.info(f"Found {len(plugins_to_restore)} missing third-party plugin(s) in backup.")
            selected = []
            for rel_path, src, dst in plugins_to_restore:
                if ApplicationSetup.confirm(f"Restore plugin {rel_path}?", "y"):
                    selected.append((rel_path, src, dst))
                else:
                    self.skipped_plugins.append(rel_path)
                    logging.info(f"Skipping plugin {rel_path}: deselected by user.")
            plugins_to_restore = selected
            if not plugins_to_restore:
                logging.info("No plugins selected for restore.")
                self.runtime_restore_plugins = int(time.time() - start)
                return True

        success = True
        if self.dry_run:
            for rel_path, src, dst in plugins_to_restore:
                logging.info(f"[Dry Run] Would copy plugin {rel_path} from {src} to {dst}")
                self.restored_plugins.append(rel_path)
        else:
            for rel_path, src, _dst in plugins_to_restore:
                try:
                    self._copy_into(src, clone_code_root, rel_path)
                    logging.info(f"Restored plugin {rel_path} from backup.")
                    self.restored_plugins.append(rel_path)
                except (OSError, shutil.Error) as e:
                    logging.error(f"Failed to restore plugin {rel_path}: {e}")
                    success = False

            # A staged tree gets its ownership in commit_clone(); only fix up live code here.
            if self.restored_plugins and target_root is None:
                try:
                    subprocess.run(['chown', '-R', f'{chown_user}:{chown_group}', target], check=True)
                except subprocess.CalledProcessError as e:
                    logging.error(f"Setting ownership after plugin restore failed (exit code {e.returncode}).")
                    success = False

        self.runtime_restore_plugins = int(time.time() - start)
        logging.info(f"Plugin restore completed in {self.runtime_restore_plugins} seconds. Restored: {len(self.restored_plugins)}, already present: {len(self.skipped_plugins)}.")
        return success

    def _php_command(self, script, *args):
        """Command line for a Moodle CLI script (relative to the Moodle dir). When
        php_cli_user is set and we are root, it runs as that user via runuser, so
        core and plugin upgrade code does not execute with root privileges."""
        command = ['php', os.path.join(self.path, self.moodle, script), *args]
        if self.php_cli_user and os.geteuid() == 0:
            command = ['runuser', '-u', self.php_cli_user, '--', *command]
        return command

    def moodle_cli_upgrade(self, moodle_maintenance_mode_flag, force_continue):
        """Upgrading Moodle instance via admin/cli/upgrade.php with pre/post system checks.

        Returns True only if the pre-check allowed the upgrade, maintenance mode
        could be enabled (when requested) and disabled again, the upgrade succeeded
        and the post-upgrade check reported no errors."""
        start = time.time()
        logging.info("Starting Moodle upgrade via CLI...")
        upgrade_command = self._php_command("admin/cli/upgrade.php", "--non-interactive")

        if self.dry_run:
            logging.info(f"[Dry Run] Would run system checks using: {' '.join(self._php_command('admin/cli/checks.php'))}")
            if moodle_maintenance_mode_flag:
                self.moodle_maintenance_mode(True)
            logging.info(f"[Dry Run] Would run: {' '.join(upgrade_command)}")
            if moodle_maintenance_mode_flag:
                self.moodle_maintenance_mode(False)
            self.runtime_cliupgrade = 0
            return True

        try:
            # Run pre-upgrade checks
            if not self.run_moodle_check(before_upgrade=True, force_continue=force_continue):
                logging.critical("Moodle CLI upgrade not started because the pre-upgrade system check failed.")
                self.upgrade_failed = True
                self.upgrade_error_details.append("Pre-upgrade system check failed; upgrade not started")
                return False

            self._run_upgrade(upgrade_command, moodle_maintenance_mode_flag)

            # Run post-upgrade checks (also after a failed upgrade, for diagnosis)
            post_check_ok = self.run_moodle_check(before_upgrade=False, force_continue=force_continue)
        finally:
            logging.info("Finished Moodle upgrade via CLI")
            self.runtime_cliupgrade = int(time.time() - start)
        return not self.upgrade_failed and not self.maintenance_disable_failed and post_check_ok

    def _run_upgrade(self, upgrade_command, maintenance_requested):
        """Run upgrade.php inside maintenance mode (if requested), recording failures."""
        maintenance_enabled = False
        try:
            if maintenance_requested:
                if not self.moodle_maintenance_mode(True):
                    logging.critical("Not starting the upgrade: maintenance mode was requested but could not be enabled.")
                    self.upgrade_failed = True
                    self.upgrade_error_details.append("Maintenance mode could not be enabled; upgrade not started")
                    return
                maintenance_enabled = True

            returncode, error_lines = self._run_upgrade_process(upgrade_command)
            if returncode != 0:
                logging.error(f"Moodle upgrade failed with exit code {returncode}")
                self.upgrade_failed = True
                self.upgrade_error_details.append(f"Exit code: {returncode}")
                # Add captured error lines to details
                self.upgrade_error_details.extend(error_lines)

        except Exception as e:
            logging.error(f"Unexpected error during Moodle upgrade: {e}")
            self.upgrade_failed = True
            self.upgrade_error_details.append(f"Unexpected error: {e}")
        finally:
            # Never leave the site in maintenance mode because of an exception
            # or Ctrl-C between enabling and disabling it.
            if maintenance_enabled and not self.moodle_maintenance_mode(False):
                self.maintenance_disable_failed = True
                disable = ' '.join(self._php_command('admin/cli/maintenance.php', '--disable'))
                logging.critical(f"Maintenance mode is still ENABLED. Disable it manually with: {disable}")
                self.upgrade_error_details.append("Maintenance mode could not be disabled")

    def _run_upgrade_process(self, command):
        """Run upgrade.php, logging its output as it arrives. Returns (exit code,
        error lines). Each pipe is read by its own thread: reading both from one
        thread can deadlock when the child fills one pipe while we wait for a line
        on the other. On interruption the child is terminated and reaped."""
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        lines = queue.Queue()

        def reader(stream, from_stderr):
            try:
                for line in stream:
                    lines.put((line, from_stderr))
            finally:
                lines.put(None)

        readers = [threading.Thread(target=reader, args=(process.stdout, False), daemon=True),
                   threading.Thread(target=reader, args=(process.stderr, True), daemon=True)]
        for thread in readers:
            thread.start()

        # Current "== Section ==" header and the error lines seen so far
        state = {'section': '', 'errors': []}
        try:
            finished = 0
            while finished < len(readers):
                item = lines.get()
                if item is None:
                    finished += 1
                    continue
                self._process_upgrade_line(item[0], item[1], state)
            process.wait()
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
        finally:
            for thread in readers:
                thread.join(timeout=1)
        return process.returncode, state['errors']

    @staticmethod
    def _process_upgrade_line(line, from_stderr, state):
        """Log one line of upgrade.php output at a fitting level and collect the
        lines that indicate errors in state['errors']."""
        line = line.strip()
        if not line:
            return
        if from_stderr:
            logging.warning(line)
            state['errors'].append(line)
        # Track section headers (Moodle uses == Section == format)
        elif line.startswith('==') and line.endswith('=='):
            state['section'] = line.strip('= ')
            logging.info(line)
        # Moodle uses !! prefix for errors/warnings
        elif line.startswith('!!'):
            logging.error(line)
            state['errors'].append(f"{state['section']}: {line}" if state['section'] else line)
        elif 'error' in line.lower() or 'failed' in line.lower():
            logging.warning(line)
            state['errors'].append(line)
        else:
            logging.info(line)

    def moodle_maintenance_mode(self, enable: bool):
        """Enable or disable Moodle maintenance mode. Returns True on success."""
        mode = "enable" if enable else "disable"
        # Argument list, no shell: config paths with spaces or metacharacters stay inert.
        command = self._php_command("admin/cli/maintenance.php", f"--{mode}")

        if self.dry_run:
            logging.info(f"[Dry Run] Would run: {' '.join(command)}")
            return True
        try:
            subprocess.run(command, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True)
            logging.info(f"Moodle maintenance mode {mode}d successfully.")
            return True
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to {mode} maintenance mode (exit code {e.returncode}): {(e.stderr or e.stdout or '').strip()}")
        except OSError as e:
            logging.error(f"Failed to {mode} maintenance mode: {e}")
        return False

    # checks.php exit codes (NRPE convention, see admin/cli/checks.php)
    CHECK_STATUS = {0: "OK", 1: "WARNING", 2: "CRITICAL", 3: "UNKNOWN"}

    def _check_has_errors(self, phase):
        """Run checks.php and return True if it reported an error or could not run.
        Exit code 1 (warnings) is not an error; 2 (error/critical), 3 (unknown) and
        anything else (PHP failure) are."""
        script = os.path.join(self.path, self.moodle, "admin/cli/checks.php")
        if not os.path.isfile(script):
            logging.critical(f"Moodle system check ({phase}) cannot run: {script} not found.")
            return True
        try:
            result = subprocess.run(self._php_command("admin/cli/checks.php"), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except Exception as e:
            logging.critical(f"Unexpected error while running Moodle system check ({phase}): {str(e)}")
            return True

        output = result.stdout.strip()
        formatted_message = "checks.php returned:" + ("\n" if "\n" in output else " ") + output
        status = self.CHECK_STATUS.get(result.returncode)
        if result.returncode == 0:
            logging.info(formatted_message)
        elif result.returncode == 1:
            logging.warning(formatted_message)
        else:
            logging.error(formatted_message)
        if result.stderr.strip():
            logging.warning(f"checks.php stderr: {result.stderr.strip()}")
        if result.returncode not in (0, 1):
            logging.critical(f"Moodle system check ({phase}) failed with exit code {result.returncode}"
                             f" ({status or 'checks.php did not run correctly'})")
            return True
        return False

    def run_moodle_check(self, before_upgrade=True, force_continue=False):
        """Run Moodle system check before or after upgrades.

        Before the upgrade, returns whether to go ahead: True if the check passed,
        or failed and --force-continue or the user said to continue anyway.
        After the upgrade, returns whether the check passed (errors make the run fail)."""
        phase = "before upgrade" if before_upgrade else "after upgrade"
        logging.info(f"Running Moodle system check ({phase})...")
        logging.info(SEPARATOR)
        error = self._check_has_errors(phase)
        logging.info(SEPARATOR)
        logging.info("Finished Moodle system check")

        if not error:
            return True
        if not before_upgrade:
            logging.error("The Moodle system check after the upgrade reported errors; the run will be reported as failed.")
            return False
        if force_continue:
            logging.warning("Errors detected in the Moodle system check; continuing because of --force-continue.")
            return True
        if self.non_interactive:
            logging.warning("Errors detected in the Moodle system check; non-interactive mode, not continuing "
                            "(use --force-continue to override).")
            return False
        timeout = 60
        logging.info(f"Pausing for manual intervention... (the upgrade is skipped automatically in {timeout}s)")
        return bool(ApplicationSetup.confirm("Errors detected in Moodle check. Do you want to continue?", "n", timeout))
