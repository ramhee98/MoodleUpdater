# Standard Library
import configparser
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time

# Third-Party Libraries
import apt
import requests
from logging.handlers import RotatingFileHandler

# Constants
SEPARATOR = "-------------------------------------------------------------------------"

# Runtime globals
dry_run = False

class ConfigManager:
    """Manages configuration loading and logging setup."""
    def __init__(self, config_path):
        self.config_path = config_path
        self.config = self.load_config()

    def load_config(self):
        """Load configuration from a file."""
        config = configparser.ConfigParser(interpolation=None)
        config.read(self.config_path)
        #logging.info(f"Loaded configuration from {self.config_path}")
        return config

    def configure_logging(self):
        """Configures logging based on the settings in the configuration file."""
        log_to_console = self.config.getboolean("logging", "log_to_console", fallback=True)
        log_to_file = self.config.getboolean("logging", "log_to_file", fallback=True)
        log_file_path = self.config.get("logging", "log_file_path", fallback="moodle_updater.log")
        log_level = self.config.get("logging", "log_level", fallback="INFO").upper()
        numeric_level = getattr(logging, log_level, logging.INFO)

        handlers = []
        if log_to_console:
            handlers.append(logging.StreamHandler())
        if log_to_file:
            handlers.append(RotatingFileHandler(log_file_path, maxBytes=5 * 1024 * 1024, backupCount=3))

        logging.basicConfig(level=numeric_level, format="%(asctime)s - %(levelname)s - %(message)s", handlers=handlers)
        logging.info(f"Logging configured. Level: {log_level}")
        if log_to_console:
            logging.info("Logging to console enabled.")
        if log_to_file:
            logging.info(f"Logging to file enabled. File path: {log_file_path}")

    @staticmethod
    def read_moodle_config(config_path):
        """Reads the Moodle config.php file and extracts $CFG->dbname, $CFG->dbuser, and $CFG->dbpass."""
        cfg_values = {}

        try:
            with open(config_path, 'r') as file:
                content = file.read()

            # Regular expressions to match $CFG->dbname, $CFG->dbuser, and $CFG->dbpass
            patterns = {
                'dbname': r"\$CFG->dbname\s*=\s*'([^']+)'",
                'dbuser': r"\$CFG->dbuser\s*=\s*'([^']+)'",
                'dbpass': r"\$CFG->dbpass\s*=\s*'([^']+)'",
            }

            for key, pattern in patterns.items():
                match = re.search(pattern, content)
                if match:
                    cfg_values[key] = match.group(1)
                else:
                    cfg_values[key] = None

        except FileNotFoundError:
            logging.error(f"File {config_path} not found.")
        except Exception as e:
            logging.error(f"An error occurred while reading the Moodle config: {e}")

        return cfg_values

class ApplicationSetup:
    """Handles configuration loading, logging setup, and initial checks."""
    
    def __init__(self, config_path, config_template_path):
        if os.geteuid() != 0:
            sys.exit(f"This script must be run as root. Use 'sudo python3 {__file__}'")

        self.pwd = os.path.dirname(os.path.abspath(__file__))
        self.config_path = config_path
        self.config_template_path = config_template_path
        self.config_manager = ConfigManager(self.config_path)
        
        # Load configuration
        self.config = self.config_manager.config
        
        # Configure logging
        self.config_manager.configure_logging()
        
        # Perform initial setup tasks
        self.handle_auto_update()  # 🔹 This may modify config.ini

        # Ensure config is reloaded if changed
        self.config_manager = ConfigManager(self.config_path)  
        self.config = self.config_manager.config  

        # Ensure config file exists
        self.ensure_config_exists()

        # Load essential settings
        self.load_core_settings()
    
    def handle_auto_update(self):
        """Checks if auto-update is enabled and runs it if necessary."""
        auto_update = self.config.get('settings', 'auto_update_script', fallback=False)
        if auto_update == "True":
            self_update(self.pwd, self.config_path, self.config_template_path)
        else:
            logging.info(SEPARATOR)
            if confirm("Pull MoodleUpdater from GitHub?", "n"):
                self_update(self.pwd, self.config_path, self.config_template_path)

    def ensure_config_exists(self):
        """Ensures the config file exists, otherwise creates one from the template."""
        if not os.path.exists(self.config_path):
            logging.error(f"Configuration file '{self.config_path}' not found.")
            if os.path.exists(self.config_template_path):
                shutil.copy(self.config_template_path, self.config_path)
                logging.info("Configuration file has been created.")
                logging.info("Please edit config.ini to your needs.")
            else:
                logging.error("Missing both config.ini and config_template.ini. Please create config.ini manually.")
            sys.exit(1)

    def load_core_settings(self):
        """Loads core settings like paths, dry-run mode, and Moodle version."""
        self.dry_run = self.config.get('settings', 'dry_run', fallback="False") == "True"
        self.moodle = self.config.get('settings', 'moodle', fallback='moodle')
        self.path = self.config.get('settings', 'path', fallback=self.pwd)
        self.full_path = os.path.join(self.path, self.moodle)
        self.configphppath = os.path.join(self.full_path, 'config.php')

        # Detect Moodle version
        checker = MoodleVersionChecker(self.full_path, None, None)
        local_release, _ = checker.get_local_version()
        logging.info(f"Moodle version detected: {local_release}")
        logging.info(SEPARATOR)

        # Log if dry-run mode is enabled
        if self.dry_run:
            logging.warning("[Dry Run] is enabled!")
            logging.info(SEPARATOR)

class MoodleVersionChecker:
    """Handles retrieval of Moodle version information from local and remote sources."""

    def __init__(self, moodle_path, repo_url, branch):
        self.moodle_path = moodle_path
        self.repo_url = repo_url
        self.branch = branch

    def get_local_version(self):
        """Retrieve Moodle version information from the local installation."""
        version_file = os.path.join(self.moodle_path, "version.php")

        if not os.path.exists(version_file):
            logging.warning("Moodle version file not found.")
            return "Unknown", "Unknown"

        try:
            with open(version_file, "r") as f:
                content = f.read()

            # Human-friendly version (e.g., "4.1+ (Build: 20240115)")
            release_match = re.search(r"\$release\s*=\s*'([^']+)'", content)
            # Numeric version (e.g., "2024042205.00")
            build_match = re.search(r"\$version\s*=\s*([\d\.]+);", content)

            return release_match.group(1) if release_match else "Unknown", \
                   build_match.group(1) if build_match else "Unknown"

        except Exception as e:
            logging.error(f"Error reading Moodle version: {e}")
            return "Unknown", "Unknown"

    def get_remote_version(self):
        """Retrieve Moodle version information from the remote Git repository."""
        version_url = f"{self.repo_url.replace('.git', '')}/raw/{self.branch}/version.php"

        try:
            response = requests.get(version_url, timeout=10)
            if response.status_code == 200:
                # Human-friendly version (e.g., "4.1+ (Build: 20240115)")
                release_match = re.search(r"\$release\s*=\s*'([^']+)'", response.text)
                # Numeric version (e.g., "2024042205.00")
                build_match = re.search(r"\$version\s*=\s*([\d\.]+);", response.text)

                return release_match.group(1) if release_match else "Unknown", \
                       build_match.group(1) if build_match else "Unknown"
            else:
                logging.warning(f"Failed to retrieve remote version (HTTP {response.status_code})")
                return "Unknown", "Unknown"

        except requests.RequestException as e:
            logging.error(f"Error fetching remote version: {e}")
            return "Unknown", "Unknown"

def confirm(question, default=''):
    """Prompt the user for confirmation with optional default response and cancel functionality."""
    valid_responses = {'y': True, 'n': False, 'c': None}
    option = "Yes(y)/No(n)/Cancel(c)" + (f" Default={default}" if default else "")

    while True:
        user_input = input(f"{question} {option}: ").strip().lower()

        if user_input in valid_responses:
            if user_input == 'c':  # Cancel case
                logging.warning("User canceled the operation.")
                exit(1)
            return valid_responses[user_input]
        elif default and user_input == '':
            return valid_responses.get(default.lower(), False)

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
            if dry_run:
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

    def git_clone(self, config_php, repository, branch, sync_submodules):
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
            if dry_run:
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
                subprocess.run(['chown', 'www-data:www-data', clone_path, '-R'], check=True)
            except subprocess.CalledProcessError as e:
                logging.error(f"Setting folder ownership failed: {e.stderr}")

        logging.info("Finished git clone process")
        self.runtime_clone = int(time.time() - start)
        logging.info(f"Git clone completed in {self.runtime_clone} seconds.")

    def dir_backup_and_git_clone(self, config_php, full_backup, repo, branch, sync_submodules):
        """Perform directory backup followed by git clone."""
        logging.info("Starting directory backup and git clone process.")
        self.dir_backup(full_backup)
        self.git_clone(config_php, repo, branch, sync_submodules)

class SystemMonitor:
    """Monitors system resource usage and database dump progress."""

    def __init__(self):
        self.stop_event = threading.Event()

    def monitor_dump_progress(self, dump_file, check_interval=5, log_interval=60, stagnation_threshold=60):
        """
        Monitors the size of the dump file and logs its progress periodically.
        
        :param dump_file: The path to the dump file.
        :param stop_event: A threading event to signal the thread to stop.
        :param check_interval: Time in seconds between file size checks.
        :param log_interval: Minimum time in seconds between logs.
        :param stagnation_threshold: Time in seconds before logging a stagnation warning.
        """
        logging.info(f"Monitoring database dump progress: {dump_file}")

        last_size = 0
        stagnation_time = 0
        last_log_time = 0

        while not self.stop_event.is_set():
            if os.path.exists(dump_file):
                current_size = os.path.getsize(dump_file)
                now = time.time()

                if current_size == last_size:
                    stagnation_time += check_interval
                    if stagnation_time >= stagnation_threshold and now - last_log_time >= log_interval:
                        logging.warning(f"Database dump file size hasn't changed for {stagnation_time} seconds. Possible stall?")
                        last_log_time = now
                else:
                    stagnation_time = 0
                    if now - last_log_time >= log_interval:
                        size_mb = current_size / (1024 * 1024)
                        if size_mb >= 1024:
                            logging.info(f"Database dump progress: {size_mb / 1024:.2f} GB")
                        else:
                            logging.info(f"Database dump progress: {size_mb:.2f} MB")
                        last_log_time = now

                last_size = current_size

            time.sleep(check_interval)

        logging.info("Database dump monitoring stopped.")

    def monitor_memory_usage(self):
        """Monitors memory usage and logs more frequently as free memory decreases."""
        # Track previous memory states to detect recovery
        previous_critical = False
        previous_warning = False
        previous_low_free_critical = False
        previous_low_free_warning = False

        while not self.stop_event.is_set():
            # Get memory statistics
            mem_line = next(line for line in os.popen('free -t -m').readlines() if line.startswith("Mem"))
            total_memory, used_memory, free_memory, shared_memory, buff_cached_memory, available_memory = map(int, mem_line.split()[1:7])

            # === CRITICAL MEMORY STATE ===
            if available_memory < 250:
                logging.critical(
                    "CRITICAL MEMORY WARNING: Available memory critically low (%d MB)! System may soon become unstable.",
                    available_memory
                )
                previous_critical = True  # Track that we are in a critical state
                sleep_time = 0.5

            # RECOVERY: Exiting Critical State
            elif previous_critical and available_memory >= 250:
                logging.error("RECOVERY: Available memory recovered to %d MB from a critical state.", available_memory)
                previous_critical = False  # Reset state

            # === WARNING MEMORY STATE ===
            if available_memory < 500:
                if not previous_warning and not previous_critical:  # Only log if not already in a worse state
                    logging.warning(
                        "LOW MEMORY WARNING: Available memory below 500 MB (%d MB). Performance may degrade.",
                        available_memory
                    )
                previous_warning = True
                sleep_time = 1

            # RECOVERY: Exiting Warning State
            elif previous_warning and available_memory >= 500:
                logging.info("RECOVERY: Available memory recovered to %d MB, above warning threshold.", available_memory)
                previous_warning = False

            # === CRITICAL: FREE MEMORY EXTREMELY LOW (but available is OK) ===
            if free_memory < 125 and available_memory > 500:
                logging.critical("LOW FREE MEMORY: Free memory is %d MB, but available memory is sufficient (%d MB).", free_memory, available_memory)
                previous_low_free_critical = True
                sleep_time = 0.5

            # RECOVERY: Exiting Critical State
            elif previous_low_free_critical and free_memory >= 125:
                logging.warning("RECOVERY: Free memory increased to %d MB from a critical state.", free_memory)
                previous_low_free_critical = False

            # === FREE MEMORY LOW (but available is OK) ===
            if free_memory < 250 and available_memory > 500:
                if not previous_low_free_warning:
                    logging.warning("LOW FREE MEMORY: Free memory is %d MB, but available memory is sufficient (%d MB).", free_memory, available_memory)
                previous_low_free_warning = True
                sleep_time = 2

            # RECOVERY: Free Memory Restored
            elif previous_low_free_warning and free_memory >= 250:
                logging.info("RECOVERY: Free memory increased to %d MB.", free_memory)
                previous_low_free_warning = False

            # === NORMAL STATE ===
            else:
                sleep_time = 5  # Normal operation

            # Debug log for general memory monitoring
            logging.debug(
                "Memory Stats | Total: %d MB | Used: %d MB | Free: %d MB | Shared: %d MB | Buffers/Cached: %d MB | Available: %d MB",
                total_memory, used_memory, free_memory, shared_memory, buff_cached_memory, available_memory
            )

            time.sleep(sleep_time)

        logging.info("Memory monitoring stopped.")

    def start_monitoring(self, dump_file):
        """Starts monitoring memory and optionally dump progress in separate threads."""
        logging.info("Starting system monitoring...")
        
        self.memory_thread = threading.Thread(target=self.monitor_memory_usage)
        self.memory_thread.start()

        self.dump_thread = threading.Thread(target=self.monitor_dump_progress, args=(dump_file,))
        self.dump_thread.start()

    def stop_monitoring(self):
        """Stops all monitoring threads."""
        logging.info("Stopping system monitoring...")
        self.stop_event.set()

        self.memory_thread.join()
        if hasattr(self, "dump_thread"):
            self.dump_thread.join()

def get_commit_details(commit_hash, pwd):
    """Retrieve commit details (time, author, summary) for a given commit hash."""
    try:
        result = subprocess.run(
            ['git', '-C', pwd, 'show', '-s', '--format=%ci|%an|%s', commit_hash],
            capture_output=True, text=True, check=True
        )
        output = result.stdout.strip().split('|')

        if len(output) == 3:
            return output  # Returns (time, author, summary)
        else:
            logging.warning(f"Unexpected output format from Git for commit {commit_hash}: {output}")
            return "Unknown", "Unknown", "Unknown"
    
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to retrieve commit details for {commit_hash}: {e.stderr}")
    except FileNotFoundError:
        logging.error("Git command not found. Ensure Git is installed and accessible.")
    except Exception as e:
        logging.error(f"Unexpected error retrieving commit details: {e}")

    return "Unknown", "Unknown", "Unknown"

class ServiceManager:
    """Handles web and database service restarts."""

    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.cache = apt.Cache()  # Load package cache once and reuse

    def restart_webserver(self, action):
        """start / stop the apache or nginx webserver, depending on which one is installed"""
        webserver = None

        if self.cache["apache2"].is_installed:
            webserver = "apache2"
        elif self.cache["nginx"].is_installed:
            webserver = "nginx"

        if not webserver:
            logging.warning("No supported web server found (Apache/Nginx).")
            return

        logging.info(f"Attempting to {action} the {webserver} service.")

        if self.dry_run:
            logging.info(f"[Dry Run] Would run: systemctl {action} {webserver}")
        else:
            self._run_systemctl(action, webserver)

    def restart_database(self, action):
        """Start / stop the installed database service, based on availability."""
        database_services = {
            "mysql-server": "mysql",
            "mariadb-server": "mariadb",
            "postgresql": "postgresql",
            "mssql-server": "mssql-server",
            "mongodb": "mongodb",
            "redis-server": "redis",
        }

        # Identify installed database services
        installed_db_services = [service for service in database_services if service in self.cache and self.cache[service].is_installed]

        if not installed_db_services:
            logging.warning("No supported database server found.")
            return

        for db_service in installed_db_services:
            service_name = database_services[db_service]
            logging.info(f"Attempting to {action} the {service_name} service.")

            if self.dry_run:
                logging.info(f"[Dry Run] Would run: systemctl {action} {service_name}")
            else:
                self._run_systemctl(action, service_name)

    def _run_systemctl(self, action, service_name):
        """Runs the systemctl command for service management."""
        try:
            subprocess.run(['systemctl', action, service_name], check=True)
            logging.info(f"{service_name} service {action}ed successfully.")
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to {action} the {service_name} service: {e.stderr}")

def self_update(pwd, CONFIG_PATH, CONFIG_TEMPLATE_PATH):
    """Check if running inside a Git repo, ensure no local changes, and pull the latest changes."""
    logging.info(SEPARATOR)
    logging.info("Starting self-update process...")
    try:
        git_dir = os.path.join(pwd, '.git')
        if not os.path.exists(git_dir):
            logging.warning("Not a Git repository. Skipping self-update.")
            return

        # Get current branch
        try:
            branch_result = subprocess.run(
                ['git', '-C', pwd, 'rev-parse', '--abbrev-ref', 'HEAD'],
                capture_output=True, text=True, check=True
            )
        except subprocess.CalledProcessError as e:
            logging.error(f"Retrieving current branch failed: {e.stderr}")
        current_branch = branch_result.stdout.strip()
        logging.info(f"Current branch: {current_branch}")

        # Get current commit details
        try:
            current_commit_result = subprocess.run(
                ['git', '-C', pwd, 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True
            )
        except subprocess.CalledProcessError as e:
            logging.error(f"Retrieving commit details failed: {e.stderr}")
        current_commit = current_commit_result.stdout.strip() if current_commit_result.returncode == 0 else "Unknown"
        current_commit_time, current_commit_author, current_commit_summary = get_commit_details(current_commit, pwd)

        logging.info(f"Current commit: {current_commit}")
        logging.info(f"Commit time: {current_commit_time}")
        logging.info(f"Author: {current_commit_author}")
        logging.info(f"Summary: {current_commit_summary}")

        # Check for uncommitted changes
        try:
            status_result = subprocess.run(
                ['git', '-C', pwd, 'status', '--porcelain'], capture_output=True, text=True, check=True
            )
        except subprocess.CalledProcessError as e:
            logging.error(f"Checking for uncommited changes failed: {e.stderr}")
        if status_result.stdout.strip():
            logging.warning("Local changes detected. Skipping self-update to avoid conflicts.")
            check_config_differences(CONFIG_PATH, CONFIG_TEMPLATE_PATH)
            return

        # Pull the latest changes from the remote repository to update the script.
        logging.info("Checking for updates...")
        try:
            pull_result = subprocess.run(
                ['git', '-C', pwd, 'pull', '--rebase'], capture_output=True, text=True, check=True
            )
        except subprocess.CalledProcessError as e:
            logging.error(f"Git pull failed: {e.stderr}")


        if "Already up to date." in pull_result.stdout:
            logging.info("The script is already up to date.")
            check_config_differences(CONFIG_PATH, CONFIG_TEMPLATE_PATH)
        else:
            # Get updated commit details
            try:
                updated_commit_result = subprocess.run(
                    ['git', '-C', pwd, 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True
                )
            except subprocess.CalledProcessError as e:
                logging.error(f"Retrieving updated commit failed: {e.stderr}")

            updated_commit = updated_commit_result.stdout.strip() if updated_commit_result.returncode == 0 else "Unknown"
            updated_commit_time, updated_commit_author, updated_commit_summary = get_commit_details(updated_commit, pwd)

            logging.info(f"Updated from commit {current_commit} to commit {updated_commit} on branch {current_branch}")
            logging.info(f"Old commit details:")
            logging.info(f"  Time: {current_commit_time}")
            logging.info(f"  Author: {current_commit_author}")
            logging.info(f"  Summary: {current_commit_summary}")
            logging.info(f"New commit details:")
            logging.info(f"  Time: {updated_commit_time}")
            logging.info(f"  Author: {updated_commit_author}")
            logging.info(f"  Summary: {updated_commit_summary}")

            check_config_differences(CONFIG_PATH, CONFIG_TEMPLATE_PATH)

            # Restart the script with the updated version
            logging.info("Restarting the script...")
            logging.info(SEPARATOR)
            os.execv(sys.executable, [sys.executable] + sys.argv)

    except Exception as e:
        logging.error(f"Error during self-update: {e}")
        logging.info("Continuing with the current version.")

def check_config_differences(config_path, template_path):
    """Check for differences between config.ini and config_template.ini."""
    logging.info("Checking configuration differences.")
    try:
        config = configparser.ConfigParser(interpolation=None)
        template = configparser.ConfigParser(interpolation=None)
        config.read(config_path)
        template.read(template_path)

        all_sections = set(config.sections()).union(set(template.sections()))
        for section in all_sections:
            config_items = set(config.items(section)) if config.has_section(section) else set()
            template_items = set(template.items(section)) if template.has_section(section) else set()
            added = template_items - config_items
            removed = config_items - template_items

            if added or removed:
                logging.warning(f"Differences in section {section}: Added={added}, Removed={removed}")

    except Exception as e:
        logging.error(f"Error while checking configuration differences: {e}")

def main():
    global dry_run

    # Initialize Application Setup
    setup = ApplicationSetup(
        config_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.ini'),
        config_template_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config_template.ini')
    )

    config = setup.config
    pwd = setup.pwd
    dry_run = setup.dry_run
    moodle = setup.moodle
    path = setup.path
    full_path = setup.full_path
    configphppath = setup.configphppath
    multithreading = False

    # Get user confirmation for operations
    dir_backup = confirm("Start directory backup process?", "y")
    db_dump = confirm("Start DB dump process?", "y")
    git_clone = confirm("Start git clone process?", "y")

    logging.info(SEPARATOR)
    logging.info(f"dirbackup: {dir_backup}")
    logging.info(f"dbdump: {db_dump}")
    logging.info(f"gitclone: {git_clone}")
    logging.info(SEPARATOR)

    # Abort if no tasks were selected
    if not dir_backup and not db_dump and not git_clone:
        logging.warning("No tasks selected. Script aborted.")
        sys.exit(1)

    folder_backup_path = config.get('settings', 'folder_backup_path', fallback='pwd')
    if folder_backup_path in ["pwd", ""]:
        folder_backup_path = pwd
    if not folder_backup_path.endswith("/"):
        folder_backup_path = os.path.join(folder_backup_path, '')

    backup_manager = MoodleBackupManager(
        path=path,
        moodle=moodle,
        folder_backup_path=folder_backup_path,
        dry_run=dry_run
    )

    restart_webserver_flag = confirm("Restart webserver automatically?", "y")
    restart_database_flag = False
    verbose = confirm("Do you want to enable verbose mode?", default='n')

    if dir_backup or git_clone:
        logging.info("Preparing Moodle directory path.")
        if not confirm(f"Is this the correct Moodle directory? {path}", "y"):
            path = input("Please enter a path: ").rstrip("/")
            full_path = os.path.join(path, moodle)

    # Directory backup process
    if dir_backup:
        full_backup = confirm("Backup entire folder (containing moodle, moodledata, and data)?", "n")

    # Database dump process
    if db_dump:
        db_dump_path = config.get('settings', 'db_dump_path', fallback='pwd')
        if db_dump_path in ["pwd", ""]:
            db_dump_path = pwd
        read_db_from_config = config.get('database', 'read_db_from_config', fallback="True") == "True"
        dbpass = ""

        if not read_db_from_config:
            dbname = config.get('database', 'db_name', fallback='moodle')
            dbuser = config.get('database', 'db_user', fallback='root')
            while not dbpass.strip():
                dbpass = input("Please enter DB password: ").strip()
        else:
            cfg = ConfigManager.read_moodle_config(configphppath)
            dbname = cfg.get('dbname')
            dbuser = cfg.get('dbuser')
            dbpass = cfg.get('dbpass')
        
        restart_database_flag = confirm("Restart database before dump?", "n")

        if dry_run:
            logging.info(f"[Dry Run] Would run: mysqlshow to check if DB: {dbname} is accessible with user: {dbuser}")
            result = "returncode=0"
        else:
            try:
                subprocess.run(
                    ['mysqlshow', '-u', dbuser, f'-p{dbpass}', dbname],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, check=True
                )
                logging.info("Connection to DB established.")
            except subprocess.CalledProcessError as e:
                logging.error(f"Connection to DB failed: {e.stderr}")
                while not dbpass.strip():
                    dbpass = input("Please enter DB password again: ").strip()
                    if dbpass:
                        break
                try:
                    subprocess.run(
                        ['mysqlshow', '-u', dbuser, f'-p{dbpass}', dbname],
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, check=True
                    )
                    logging.info("Connection to DB established.")
                except subprocess.CalledProcessError as e:
                    logging.error(f"Connection to DB failed: {e.stderr}")
                    sys.exit(1)

    # Git clone process
    if git_clone:
        logging.info("Preparing git clone process.")
        repo = config.get('settings', 'repo')
        branch = config.get('settings', 'branch')

        # Get local and remote versions
        checker = MoodleVersionChecker(full_path, repo, branch)
        local_release, local_build = checker.get_local_version()
        remote_release, remote_build = checker.get_remote_version()

        if local_build != "Unknown" and remote_build != "Unknown":
            try:
                if float(remote_build) == float(local_build):
                    logging.info(f"Local Moodle version ({local_release} - (Version: {local_build})) is up-to-date.")
                elif float(remote_build) > float(local_build):
                    logging.info(f"Newer Moodle version available ({remote_release} - (Version: {remote_build}) > {local_release} - (Version: {local_build})). Proceeding with update.")
                else:
                    logging.critical(f"Local Moodle version ({local_release} - (Version: {local_build})) is newer than remote ({remote_release} - (Version: {remote_build})). Skipping update.")
                    sys.exit(1)

            except Exception as e:
                logging.error(f"Error parsing Moodle versions: local='{local_release}', remote='{remote_release}'. Exception: {e}")

        if not confirm(f"Do you want to copy {configphppath} from the old directory?", "y"):
            customconfigphppath = input("Please enter a config.php path [press enter to skip]: ")
            if customconfigphppath:
                with open(customconfigphppath, 'r') as file:
                    configphp = file.read()
            else:
                logging.info("Restore of old config.php skipped.")
        else:
            with open(configphppath, 'r') as file:
                configphp = file.read()

        if not confirm(f"Do you want to git checkout {branch}?", "y"):
            branch = input("Please enter custom branch: ")

        sync_submodules = confirm("Do you want to sync and update all submodules?", "y")

    if not confirm("Do you want to confirm the installation?"):
        logging.warning("User canceled the operation.")
        exit(1)
    # Start operations
    start_time = time.time()
    logging.info(f"Started at {time.strftime('%Y-%m-%d %H:%M:%S')}")

    service_manager = ServiceManager(dry_run=False)  # Create an instance

    if restart_webserver_flag:
        service_manager.restart_webserver("stop")

    if restart_database_flag:
        service_manager.restart_database("restart")
        time.sleep(2) # Pause to ensure the DB is fully ready

    # Handle multithreading if multiple operations were selected
    if dir_backup and db_dump and git_clone:
        multithreading = True

        t_backup_clone = threading.Thread(
            target=backup_manager.dir_backup_and_git_clone,
            args=(configphp, full_backup, repo, branch, sync_submodules,)
        )
        t_dump = threading.Thread(target=backup_manager.db_dump, args=(dbname, dbuser, dbpass, verbose, db_dump_path,))

        logging.info("Starting directory backup, git clone, and database dump (multithreaded).")
        t_backup_clone.start()
        t_dump.start()
        t_backup_clone.join()
        t_dump.join()
    elif dir_backup and db_dump:
        multithreading = True

        t_backup = threading.Thread(target=backup_manager.dir_backup, args=(full_backup,))
        t_dump = threading.Thread(target=backup_manager.db_dump, args=(dbname, dbuser, dbpass, verbose, db_dump_path,))

        logging.info("Starting directory backup and database dump (multithreaded).")
        t_backup.start()
        t_dump.start()
        t_backup.join()
        t_dump.join()
    elif db_dump and git_clone:
        multithreading = True

        t_dump = threading.Thread(target=backup_manager.db_dump, args=(dbname, dbuser, dbpass, verbose, db_dump_path,))
        t_clone = threading.Thread(target=backup_manager.git_clone, args=(configphp, repo, branch, sync_submodules,))

        logging.info("Starting database dump and git clone (multithreaded).")
        t_dump.start()
        t_clone.start()
        t_dump.join()
        t_clone.join()
    else:
        if dir_backup:
            logging.info("Starting directory backup")
            backup_manager.dir_backup(full_backup,)

        if db_dump:
            logging.info("Starting database dump")
            backup_manager.db_dump(dbname, dbuser, dbpass, verbose, db_dump_path)

        if git_clone:
            logging.info("Starting git clone")
            backup_manager.git_clone(configphp, repo, branch, sync_submodules)

    if restart_webserver_flag:
        service_manager.restart_webserver("start")

    runtime = int(time.time() - start_time)  # Convert to integer seconds
    runtime_backup = backup_manager.runtime_backup
    runtime_dump = backup_manager.runtime_dump
    runtime_clone = backup_manager.runtime_clone

    # Log if any operation times were recorded
    if runtime_backup:
        logging.info("Directory backup time needed: %d seconds", runtime_backup)
    else:
        runtime_backup = 0

    if runtime_dump:
        logging.info("Database dump time needed: %d seconds", runtime_dump)
    else:
        runtime_dump = 0

    if runtime_clone:
        logging.info("Git clone time needed: %d seconds", runtime_clone)
    else:
        runtime_clone = 0

    # Log total runtime
    logging.info("Total execution time (excluding user input): %d seconds", runtime)
    if multithreading:
        logging.info("Time saved with multithreading: %d seconds", runtime_backup + runtime_dump + runtime_clone - runtime)

    logging.info("------------------------------------------------------------------------------------------")
    logging.info("Finished at %s", time.strftime("%Y-%m-%d %H:%M:%S"))

    if dry_run:
        logging.info(SEPARATOR)
        logging.info("[Dry Run] was enabled!")
        logging.info(SEPARATOR)

main()