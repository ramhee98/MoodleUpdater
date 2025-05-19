import os
import time
import logging
import select
import shutil
import subprocess
import sys
from modules.application_setup import ApplicationSetup
from modules.system_monitor import SystemMonitor

SEPARATOR = "-------------------------------------------------------------------------"

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
        dump_args = [
            'mysqldump', '-u', dbuser, f'-p{dbpass}',
            '--single-transaction', '--skip-lock-tables',
            '--max_allowed_packet=100M', '--quick', '--databases', dbname
        ]

        if verbose:
            dump_args.append('--verbose')

        logging.info(f"Starting database dump for {dbname} to {dump_file}")

        # Initialize SystemMonitor
        monitor = SystemMonitor()

        # Start monitoring during database dump
        monitor.start_monitoring(dump_file)

        try:
            if self.dry_run:
                sanitized_args = [arg if not arg.startswith('-p') else '-p *****' for arg in dump_args]
                logging.info(f"[Dry Run] Would run: {' '.join(sanitized_args)}")
                time.sleep(10)
            else:
                with open(dump_file, "w") as dump:
                    result = subprocess.run(dump_args, stdout=dump, stderr=subprocess.PIPE, text=True, check=True)
                    if result.stderr:
                        logging.warning(f"mysqldump warning: {result.stderr.strip()}")
                    logging.info(f"Database dump saved in {dump_file} - ({os.path.getsize(dump_file) / (1024 * 1024 * 1024):.2f} GB)")
        except (IOError, OSError) as file_error:
            logging.error(f"Failed to open {dump_file} for writing: {file_error}")
            return
        except subprocess.CalledProcessError as e:
            logging.error(f"Database dump failed: {e.stderr.strip()}")
            return
        finally:
            # Stop monitoring
            monitor.stop_monitoring()

        self.runtime_dump = int(time.time() - start)

    def git_clone(self, config_php, repository, branch, sync_submodules, chown_user, chown_group):
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
                try:
                    subprocess.run(['git', 'submodule', 'update', '--init', '--recursive', '--remote'], cwd=clone_path, check=True)
                except subprocess.CalledProcessError as e:
                    logging.error(f"Git submodule update failed: {e.stderr}")

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

    def dir_backup_and_git_clone(self, config_php, full_backup, repo, branch, sync_submodules, chown_user, chown_group):
        """Perform directory backup followed by git clone."""
        logging.info("Starting directory backup and git clone process.")
        self.dir_backup(full_backup)
        self.git_clone(config_php, repo, branch, sync_submodules, chown_user, chown_group)

    def moodle_cli_upgrade(self, moodle_maintenance_mode_flag):
        """Upgrading Moodle instance via admin/cli/upgrade.php with pre/post system checks"""
        start = time.time()
        logging.info("Starting Moodle upgrade via CLI...")
        moodle_upgrade_script = os.path.join(self.path, self.moodle, "admin/cli/upgrade.php")

        if self.dry_run:
            logging.info(f"[Dry Run] Would run: php {moodle_upgrade_script} --non-interactive")
            logging.info(f"[Dry Run] Would run system checks using: php admin/cli/checks.php")
        else:
            # Run pre-upgrade checks
            self.run_moodle_check(before_upgrade=True)

            try:
                if moodle_maintenance_mode_flag:
                    self.moodle_maintenance_mode(True)

                process = subprocess.Popen(
                    ['php', moodle_upgrade_script, '--non-interactive'],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
                )

                # Process both stdout and stderr as they come in
                while True:
                    ready_to_read, _, _ = select.select([process.stdout, process.stderr], [], [])
                    for stream in ready_to_read:
                        line = stream.readline().strip()
                        if not line:
                            continue
                        if stream is process.stdout:
                            logging.info(line)
                        elif stream is process.stderr:
                            logging.warning(line)
                    if process.poll() is not None:
                        break

                process.wait()

                if moodle_maintenance_mode_flag:
                    self.moodle_maintenance_mode(False)

                if process.returncode != 0:
                    logging.error(f"Moodle upgrade failed with exit code {process.returncode}")

            except Exception as e:
                logging.error(f"Unexpected error during Moodle upgrade: {e}")

            # Run post-upgrade checks
            self.run_moodle_check(before_upgrade=False)

        logging.info("Finished Moodle upgrade via CLI")
        self.runtime_cliupgrade = int(time.time() - start)
        logging.info(f"Moodle CLI Upgrade completed in {self.runtime_cliupgrade} seconds.")

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

    def run_moodle_check(self, before_upgrade=True):
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
            logging.info(f"Pausing for manual intervention... (script will continue automatically in {timeout}s)")
            if not ApplicationSetup.confirm(f"Errors detected in Moodle check. Do you want to continue?", auto_continue_choice, timeout):
                logging.critical(f"Execution stopped due to errors in Moodle system check ({phase}).")
                sys.exit(1)

        logging.info(SEPARATOR)
        logging.info(f"Finished Moodle system check")