import os
import time
import logging
import select
import shutil
import subprocess
import sys
import glob
from modules.application_setup import ApplicationSetup
from modules.system_monitor import SystemMonitor

SEPARATOR = "-------------------------------------------------------------------------"


def _sanitize_db_output(text, password):
    """Strip the literal password and password-related warnings from
    mysql/mysqldump output before it goes to the log."""
    if not text:
        return ""
    sanitized = text
    if password:
        sanitized = sanitized.replace(password, "*****")
    # Drop the well-known mysqldump/mysql line about using -p on the CLI;
    # it's noise, and depending on locale variants it can be quoted oddly.
    cleaned_lines = [
        line for line in sanitized.splitlines()
        if "password on the command line" not in line.lower()
    ]
    return "\n".join(cleaned_lines).strip()

class MoodleBackupManager:
    """Manages directory backups, database dumps, and Git clone operations for Moodle."""

    def __init__(self, path, moodle, folder_backup_path, dry_run=False):
        self.path = path
        self.moodle = moodle
        self.folder_backup_path = folder_backup_path
        self.dry_run = dry_run
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
        # Track plugin restore results
        self.runtime_restore_plugins = None
        self.restored_plugins = []
        self.skipped_plugins = []

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
        """Handle directory backups using rsync."""
        start = time.time()

        backup_type = "full" if full_backup else "partial"
        source_path = os.path.join(self.path, '') if full_backup else os.path.join(self.path, self.moodle, '')
        backup_folder = os.path.join(
            self.folder_backup_path,
            f"{self.moodle}_bak_{backup_type}_{time.strftime('%Y-%m-%d-%H-%M-%S')}"
        )

        exclude_args = [
            '--exclude', 'moodledata/cache',
            '--exclude', 'moodledata/localcache',
            '--exclude', 'moodledata/sessions',
            '--exclude', 'moodledata/temp',
            '--exclude', 'moodledata/trashdir',
        ] if full_backup else []

        logging.info(f"Starting {backup_type} backup from {source_path} to {backup_folder}")

        if self.dry_run:
            logging.info(f"[Dry Run] Would run: rsync {' '.join(exclude_args)} {source_path} {backup_folder}")
        else:
            try:
                subprocess.run(['rsync', '-r', *exclude_args, source_path, backup_folder], check=True)
                size = sum(
                    os.path.getsize(os.path.join(root, file))
                    for root, _, files in os.walk(backup_folder)
                    for file in files
                )
                logging.info(f"Backup completed and saved in {backup_folder} - ({size / (1024 * 1024 * 1024):.2f} GB)")
            except subprocess.CalledProcessError as e:
                logging.error(f"Backup failed: {e.stderr}")

        self.runtime_backup = int(time.time() - start)

    def db_dump(self, dbname, dbuser, dbpass, verbose, db_dump_path):
        """Perform database dump using mysqldump with progress monitoring."""
        start = time.time()

        dump_file = os.path.join(
            db_dump_path,
            f"{dbname}_{time.strftime('%Y-%m-%d-%H-%M-%S')}.sql"
        )
        # Pass the DB password via the MYSQL_PWD environment variable rather
        # than as a -p<pw> CLI argument: CLI args are visible in `ps` output
        # and shell history, env vars are not.
        dump_args = [
            'mysqldump', '-u', dbuser,
            '--single-transaction', '--skip-lock-tables',
            '--max_allowed_packet=100M', '--quick', '--databases', dbname
        ]
        dump_env = {**os.environ, 'MYSQL_PWD': dbpass}

        if verbose:
            dump_args.append('--verbose')

        logging.info(f"Starting database dump for {dbname} to {dump_file}")

        # Initialize SystemMonitor
        monitor = SystemMonitor()

        # Start monitoring during database dump
        monitor.start_monitoring(dump_file, dbname, dbuser, dbpass)

        try:
            if self.dry_run:
                logging.info(f"[Dry Run] Would run: {' '.join(dump_args)} (with MYSQL_PWD set)")
                time.sleep(10)
            else:
                with open(dump_file, "w") as dump:
                    result = subprocess.run(dump_args, stdout=dump, stderr=subprocess.PIPE, text=True, check=True, env=dump_env)
                    sanitized_stderr = _sanitize_db_output(result.stderr, dbpass)
                    if sanitized_stderr:
                        logging.warning(f"mysqldump warning: {sanitized_stderr}")
                    logging.info(f"Database dump saved in {dump_file} - ({os.path.getsize(dump_file) / (1024 * 1024 * 1024):.2f} GB)")
        except (IOError, OSError) as file_error:
            logging.error(f"Failed to open {dump_file} for writing: {file_error}")
            return
        except subprocess.CalledProcessError as e:
            logging.error(f"Database dump failed: {_sanitize_db_output(e.stderr, dbpass)}")
            return
        finally:
            # Stop monitoring
            monitor.stop_monitoring()

        self.runtime_dump = int(time.time() - start)

    def git_clone(self, config_php, repository, branch, sync_submodules, chown_user, chown_group, restore_submodules_from_backup=False, full_backup=False):
        """Clone a git repository."""
        start = time.time()
        clone_path = os.path.join(self.path, self.moodle)

        if os.path.exists(clone_path):
            if self.dry_run:
                logging.info(f"[Dry Run] Would remove existing directory: {clone_path}")
            else:
                try:
                    shutil.rmtree(clone_path)
                except PermissionError:
                    logging.error(f"Permission denied while removing {clone_path}. Try running with elevated privileges.")
                except Exception as e:
                    logging.error(f"Error removing directory {clone_path}: {e}")

        if self.dry_run:
            logging.info(f"[Dry Run] Would clone repository: {repository} to {self.path}")
            logging.info(f"[Dry Run] Would checkout branch: {branch} to {clone_path}")
        else:
            try:
                subprocess.run(['git', 'clone', repository, clone_path], check=True)
            except subprocess.CalledProcessError as e:
                logging.error(f"Git clone failed: {e.stderr}")
            try:
                subprocess.run(['git', '-C', clone_path, 'checkout', branch], check=True)
            except subprocess.CalledProcessError as e:
                logging.error(f"Git checkout failed: {e.stderr}")

        if sync_submodules:
            if self.dry_run:
                logging.info(f"[Dry Run] Would sync and update git submodules in {clone_path}")
            else:
                try:
                    subprocess.run(['git', 'submodule', 'sync'], cwd=clone_path, check=True)
                except subprocess.CalledProcessError as e:
                    logging.error(f"Git submodule sync failed: {e.stderr}")
                
                # Get list of submodules and update each individually
                result = subprocess.run(['git', 'submodule', 'status'], cwd=clone_path, capture_output=True, text=True)
                
                for line in result.stdout.strip().split('\n'):
                    if not line.strip():
                        continue
                    submodule_path = line.split()[1]
                    try:
                        subprocess.run(['git', 'submodule', 'update', '--init', '--recursive', '--remote', '--', submodule_path], 
                                      cwd=clone_path, check=True)
                        logging.info(f"Updated submodule {submodule_path} with remote tracking branch")
                        self.submodules_success += 1
                    except subprocess.CalledProcessError as e:
                        logging.error(f"Git submodule update failed for {submodule_path}: {e.stderr}")
                        self.submodules_failed += 1
                        self.failed_submodules.append(submodule_path)
                
                # Log brief summary
                total = self.submodules_success + self.submodules_failed
                if total > 0:
                    logging.info(f"Submodule sync complete: {self.submodules_success}/{total} succeeded, {self.submodules_failed}/{total} failed")
        elif restore_submodules_from_backup:
            if not full_backup:
                backup_folder = max(glob.glob(os.path.join(self.folder_backup_path, f"{self.moodle}_bak_partial_*")), key=os.path.getmtime)
            else:
                backup_folder = max(glob.glob(os.path.join(self.folder_backup_path, f"{self.moodle}_bak_full_*"), "moodle"), key=os.path.getmtime)

            submodules = subprocess.run(['git', 'config', '--file', os.path.join(backup_folder, '.gitmodules'), '--get-regexp', 'path'], capture_output=True, text=True)
            if submodules.returncode == 0:
                submodule_paths = [line.split()[1] for line in submodules.stdout.strip().split('\n')]
            else:
                logging.error(f"Failed to get submodules from backup: {submodules.stderr.strip()}")
                submodule_paths = []

            if self.dry_run:
                logging.info(f"[Dry Run] Would restore submodules {submodule_paths} from backup in {backup_folder} to {clone_path}")
                for submodule in submodule_paths:
                        logging.info(f"[Dry Run] would restore submodule {submodule} from backup {backup_folder} to {clone_path}")
            else:
                try:
                    for submodule in submodule_paths:
                        logging.info(f"Restoring submodule {submodule} from backup {backup_folder} to {clone_path}")
                        subprocess.run(['cp', '-r', os.path.join(backup_folder, submodule), os.path.join(clone_path, os.path.dirname(submodule))], check=True)
                except subprocess.CalledProcessError as e:
                    logging.error(f"Restoring submodules from backup failed: {e.stderr}")

        if self.dry_run:
            logging.info(f"[Dry Run] Would create config.php in {clone_path}")
            logging.info(f"[Dry Run] Would set ownership of {clone_path} to www-data:www-data.")
        else:
            with open(os.path.join(clone_path, 'config.php'), 'w') as config_file:
                config_file.write(config_php)
            try:
                subprocess.run(['chown', f'{chown_user}:{chown_group}', clone_path, '-R'], check=True)
            except subprocess.CalledProcessError as e:
                logging.error(f"Setting folder ownership failed: {e.stderr}")

        logging.info("Finished git clone process")
        self.runtime_clone = int(time.time() - start)
        logging.info(f"Git clone completed in {self.runtime_clone} seconds.")

    def dir_backup_and_git_clone(self, config_php, full_backup, repo, branch, sync_submodules, chown_user, chown_group, restore_submodules_from_backup=False):
        """Perform directory backup followed by git clone."""
        logging.info("Starting directory backup and git clone process.")
        self.dir_backup(full_backup)
        self.git_clone(config_php, repo, branch, sync_submodules, chown_user, chown_group, restore_submodules_from_backup, full_backup)

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

    def restore_plugins(self, chown_user, chown_group, full_backup=False, selection_mode="auto"):
        """Copy third-party plugins present in the latest directory backup but missing from the new clone.

        selection_mode "auto" restores every missing plugin; "manual" prompts y/n
        for each discovered plugin before copying it.
        """
        start = time.time()
        logging.info("Starting plugin restore from backup.")

        clone_path = os.path.join(self.path, self.moodle)

        # Locate the latest directory backup (mirrors restore-submodules logic).
        if not full_backup:
            pattern = os.path.join(self.folder_backup_path, f"{self.moodle}_bak_partial_*")
        else:
            pattern = os.path.join(self.folder_backup_path, f"{self.moodle}_bak_full_*")
        candidates = glob.glob(pattern)
        if not candidates:
            logging.error(f"No directory backup found matching {pattern}; cannot restore plugins.")
            return
        backup_folder = max(candidates, key=os.path.getmtime)

        # In a full backup the moodle source lives under <backup>/<moodle>/.
        backup_moodle_root = os.path.join(backup_folder, self.moodle) if full_backup else backup_folder

        # Moodle 5.x uses a "public dir" layout where plugins live under public/.
        # Detect which root contains the plugin parents in both the backup and the new clone.
        backup_code_root = self._find_code_root(backup_moodle_root)
        clone_code_root = self._find_code_root(clone_path)
        if backup_code_root != backup_moodle_root or clone_code_root != clone_path:
            logging.info(f"Detected Moodle public-dir layout. Backup code root: {backup_code_root}, clone code root: {clone_code_root}.")

        # Submodule paths are already handled by --restore-submodules; skip them here.
        # .gitmodules sits at the repo root, but paths inside are relative to it,
        # so we also need to strip the public/ prefix when comparing.
        submodule_paths = set()
        gitmodules = os.path.join(backup_moodle_root, '.gitmodules')
        if os.path.isfile(gitmodules):
            result = subprocess.run(
                ['git', 'config', '--file', gitmodules, '--get-regexp', 'path'],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                raw_paths = {line.split()[1] for line in result.stdout.strip().split('\n') if line.strip()}
                code_root_rel = os.path.relpath(backup_code_root, backup_moodle_root)
                prefix = '' if code_root_rel == '.' else code_root_rel + os.sep
                for p in raw_paths:
                    submodule_paths.add(p[len(prefix):] if prefix and p.startswith(prefix) else p)

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
            if rel_path in submodule_paths:
                logging.debug(f"Skipping plugin {rel_path}: handled as a git submodule, not a restorable plugin.")
                continue
            dst = os.path.join(clone_code_root, rel_path)
            if os.path.exists(dst):
                # Already in the new clone: a core plugin, or a third-party one
                # that the repo/submodules already provide. Nothing to restore.
                self.skipped_plugins.append(rel_path)
                logging.debug(f"Skipping plugin {rel_path}: already present in new clone.")
                continue
            candidates.append((rel_path, dirpath, dst))

        # A missing parent plugin is restored with `cp -r`, which already carries
        # its nested subplugins. Drop any candidate whose ancestor is also being
        # restored so we don't copy the same tree twice.
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
            return

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
                return

        if self.dry_run:
            for rel_path, src, dst in plugins_to_restore:
                logging.info(f"[Dry Run] Would copy plugin {rel_path} from {src} to {dst}")
                self.restored_plugins.append(rel_path)
        else:
            for rel_path, src, dst in plugins_to_restore:
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    subprocess.run(['cp', '-r', src, dst], check=True)
                    logging.info(f"Restored plugin {rel_path} from backup.")
                    self.restored_plugins.append(rel_path)
                except subprocess.CalledProcessError as e:
                    logging.error(f"Failed to restore plugin {rel_path}: {e.stderr}")

            if self.restored_plugins:
                try:
                    subprocess.run(['chown', f'{chown_user}:{chown_group}', clone_path, '-R'], check=True)
                except subprocess.CalledProcessError as e:
                    logging.error(f"Setting ownership after plugin restore failed: {e.stderr}")

        self.runtime_restore_plugins = int(time.time() - start)
        logging.info(f"Plugin restore completed in {self.runtime_restore_plugins} seconds. Restored: {len(self.restored_plugins)}, already present: {len(self.skipped_plugins)}.")

    def moodle_cli_upgrade(self, moodle_maintenance_mode_flag, force_continue):
        """Upgrading Moodle instance via admin/cli/upgrade.php with pre/post system checks"""
        start = time.time()
        logging.info("Starting Moodle upgrade via CLI...")
        moodle_upgrade_script = os.path.join(self.path, self.moodle, "admin/cli/upgrade.php")

        if self.dry_run:
            logging.info(f"[Dry Run] Would run: php {moodle_upgrade_script} --non-interactive")
            logging.info(f"[Dry Run] Would run system checks using: php admin/cli/checks.php")
        else:
            # Run pre-upgrade checks
            self.run_moodle_check(before_upgrade=True, force_continue=force_continue)

            try:
                if moodle_maintenance_mode_flag:
                    self.moodle_maintenance_mode(True)

                process = subprocess.Popen(
                    ['php', moodle_upgrade_script, '--non-interactive'],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
                )

                # Track error indicators from output
                error_lines = []
                current_section = ""

                # Process both stdout and stderr as they come in
                while True:
                    ready_to_read, _, _ = select.select([process.stdout, process.stderr], [], [], 0.1)
                    for stream in ready_to_read:
                        line = stream.readline().strip()
                        if not line:
                            continue
                        if stream is process.stdout:
                            # Track section headers (Moodle uses == Section == format)
                            if line.startswith('==') and line.endswith('=='):
                                current_section = line.strip('= ')
                                logging.info(line)
                            # Moodle uses !! prefix for errors/warnings
                            elif line.startswith('!!'):
                                logging.error(f"{line}")
                                error_detail = f"{current_section}: {line}" if current_section else line
                                error_lines.append(error_detail)
                            elif 'error' in line.lower() or 'failed' in line.lower():
                                logging.warning(line)
                                error_lines.append(line)
                            else:
                                logging.info(line)
                        elif stream is process.stderr:
                            logging.warning(line)
                            error_lines.append(line)
                    if process.poll() is not None:
                        break

                # Drain any remaining output after process ends
                for stream in [process.stdout, process.stderr]:
                    for line in stream:
                        line = line.strip()
                        if not line:
                            continue
                        if stream is process.stdout:
                            if line.startswith('==') and line.endswith('=='):
                                current_section = line.strip('= ')
                                logging.info(line)
                            elif line.startswith('!!'):
                                logging.error(f"{line}")
                                error_detail = f"{current_section}: {line}" if current_section else line
                                error_lines.append(error_detail)
                            elif 'error' in line.lower() or 'failed' in line.lower():
                                logging.warning(line)
                                error_lines.append(line)
                            else:
                                logging.info(line)
                        else:
                            logging.warning(line)
                            error_lines.append(line)

                process.wait()

                if moodle_maintenance_mode_flag:
                    self.moodle_maintenance_mode(False)

                if process.returncode != 0:
                    logging.error(f"Moodle upgrade failed with exit code {process.returncode}")
                    self.upgrade_failed = True
                    self.upgrade_error_details.append(f"Exit code: {process.returncode}")
                    # Add captured error lines to details
                    for err_line in error_lines:
                        self.upgrade_error_details.append(err_line)

            except Exception as e:
                logging.error(f"Unexpected error during Moodle upgrade: {e}")
                self.upgrade_failed = True
                self.upgrade_error_details.append(f"Unexpected error: {e}")

            # Run post-upgrade checks
            self.run_moodle_check(before_upgrade=False, force_continue=force_continue)

        logging.info("Finished Moodle upgrade via CLI")
        self.runtime_cliupgrade = int(time.time() - start)

    def moodle_maintenance_mode(self, enable: bool):
        """Enable or disable Moodle maintenance mode."""
        mode = "enable" if enable else "disable"
        command = f"php {os.path.join(self.path, self.moodle, 'admin/cli/maintenance.php')} --{mode}"

        if self.dry_run:
            logging.info(f"[Dry Run] Would run: {command}")
        else:
            try:
                subprocess.run(command, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                logging.info(f"Moodle maintenance mode {mode}d successfully.")
            except subprocess.CalledProcessError as e:
                logging.error(f"Failed to {mode} maintenance mode: {e.stderr}")

    def run_moodle_check(self, before_upgrade=True, force_continue=False):
        """Run Moodle system check before or after upgrades, logging results with appropriate log levels."""
        moodle_checks_script = os.path.join(self.path, self.moodle, "admin/cli/checks.php")
        error = False
        if before_upgrade:
            phase = "before upgrade"
            auto_continue_choice = "n"
        else:
            phase = "after upgrade"
            auto_continue_choice = "y"
        logging.info(f"Running Moodle system check ({phase})...")
        logging.info(SEPARATOR)

        try:
            # Run the Moodle check command and capture output (even if it fails)
            result = subprocess.run(
                ['php', moodle_checks_script],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )

            if "\n" in result.stdout.strip():
                formatted_message = "checks.php returned:\n" + result.stdout.strip()
            else:
                formatted_message = "checks.php returned: " + result.stdout.strip()

            if "CRITICAL" in formatted_message or "ERROR" in formatted_message:
                logging.error(formatted_message)
                error = True
            elif "WARNING" in formatted_message:
                logging.warning(formatted_message)
            elif "OK" in formatted_message:
                logging.info(formatted_message)
            else:
                logging.debug(formatted_message)

            if result.returncode != 0:
                logging.critical(f"Moodle system check ({phase}) failed with exit code {result.returncode}")

        except Exception as e:
            logging.critical(f"Unexpected error while running Moodle system check ({phase}): {str(e)}")
            error = True

        if error:
            timeout = 60
            if not force_continue:
                logging.info(f"Pausing for manual intervention... (script will continue automatically in {timeout}s)")
                if not ApplicationSetup.confirm(f"Errors detected in Moodle check. Do you want to continue?", auto_continue_choice, timeout):
                    logging.critical(f"Execution stopped due to errors in Moodle system check ({phase}).")
                    sys.exit(1)

        logging.info(SEPARATOR)
        logging.info(f"Finished Moodle system check")