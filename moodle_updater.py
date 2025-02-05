import os
import subprocess
import shutil
import time
import threading
import apt
import configparser
import re
import sys
import logging
from logging.handlers import RotatingFileHandler

# Constants
SEPARATOR = "-------------------------------------------------------------------------"

# Runtime globals
runtime_backup = None
runtime_dump = None
runtime_clone = None
dry_run = False

def load_config(config_path, log):
    """Load configuration from a file."""
    config = configparser.ConfigParser(interpolation=None)
    config.read(config_path)
    if log:
        logging.info(f"Loaded configuration from {config_path}")
    return config

def configure_logging(config):
    """
    Configures logging based on the settings in the configuration file.
    :param config: ConfigParser object with application configuration
    """
    # Get logging configurations from the config file
    log_to_console = config.getboolean("logging", "log_to_console", fallback=True)
    log_to_file = config.getboolean("logging", "log_to_file", fallback=True)
    log_file_path = config.get("logging", "log_file_path", fallback="moodle_updater.log")
    log_level = config.get("logging", "log_level", fallback="INFO").upper()
    numeric_level = getattr(logging, log_level, logging.INFO)

    # Configure logging handlers
    handlers = []
    if log_to_console:
        handlers.append(logging.StreamHandler())
    if log_to_file:
        handlers.append(
            RotatingFileHandler(
                log_file_path, maxBytes=5 * 1024 * 1024, backupCount=3 # Rotate logs (5 MB, 3 backups)
            )
        )

    # Set up logging
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )
    logging.info(f"Logging configured. Level: {log_level}")
    if log_to_console:
        logging.info("Logging to console enabled.")
    if log_to_file:
        logging.info(f"Logging to file enabled. File path: {log_file_path}")

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

def f_dir_backup(path, moodle, full_backup, folder_backup_path):
    """Handle directory backups using rsync."""
    global runtime_backup
    start = time.time()

    if(full_backup):
        path = os.path.join(path, '')
        backup_type = "full"
    else:
        path = os.path.join(path, moodle, '')
        backup_type = "partial"

    backup_folder = os.path.join(folder_backup_path, f"{moodle}_bak_{backup_type}_{time.strftime('%Y-%m-%d-%H-%M-%S')}")
    exclude_args = [
        '--exclude', 'moodledata/cache',
        '--exclude', 'moodledata/localcache',
        '--exclude', 'moodledata/sessions',
        '--exclude', 'moodledata/temp',
        '--exclude', 'moodledata/trashdir',
    ] if full_backup else []

    logging.info(f"Starting {backup_type} backup of {path} to {backup_folder}")
    if dry_run:
        logging.info(f"[Dry Run] Would run: rsync{' '.join(exclude_args)} {path} {backup_folder}")
    else:
        try:
            subprocess.run(['rsync', '-r', *exclude_args, path, backup_folder], check=True)
            logging.info(f"Backup completed and saved in {backup_folder}")
        except subprocess.CalledProcessError as e:
            logging.error(f"Backup failed: {e.stderr}")

    runtime_backup = int(time.time() - start)

def f_db_dump(dbname, dbuser, dbpass, verbose, db_dump_path):
    """Perform database dump using mysqldump with progress monitoring."""
    global runtime_dump
    start = time.time()

    dump_file = os.path.join(db_dump_path, f"{dbname}_{time.strftime('%Y-%m-%d-%H-%M-%S')}.sql")
    dump_args = ['mysqldump', '-u', dbuser, f'-p{dbpass}', '--single-transaction', '--skip-lock-tables', '--databases', dbname]
    if verbose:
        dump_args.append('--verbose')

    logging.info(f"Starting database dump for {dbname} to {dump_file}")

    # Start the monitoring threads
    stop_event = threading.Event()
    monitor_dump_progress_thread = threading.Thread(target=monitor_dump_progress, args=(dump_file, stop_event))
    monitor_dump_progress_thread.start()
    monitor_memory_usage_thread = threading.Thread(target=monitor_memory_usage, args=(stop_event,))
    monitor_memory_usage_thread.start()

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
                logging.info(f"Database dump saved in {dump_file}")
    except (IOError, OSError) as file_error:
        logging.error(f"Failed to open {dump_file} for writing: {file_error}")
        return
    except subprocess.CalledProcessError as e:
        logging.error(f"Database dump failed: {e.stderr.strip()}")
        return
    finally:
        # Stop the monitoring threads
        stop_event.set()
        monitor_dump_progress_thread.join()
        monitor_memory_usage_thread.join()

    runtime_dump = int(time.time() - start)

def f_git_clone(path, moodle, config_php, repository, branch, sync_submodules):
    """Clone a git repository."""
    global runtime_clone
    start = time.time()

    clone_path = os.path.join(path, moodle)

    if os.path.exists(clone_path):
        if dry_run:
            logging.info(f"[Dry Run] Would remove directory: {clone_path}")
        else:
            shutil.rmtree(clone_path)

    if dry_run:
        logging.info(f"[Dry Run] Would clone repository: {repository} to {path}")
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
                subprocess.run(['sudo', 'git', 'submodule', 'sync'], cwd=clone_path, check=True)
            except subprocess.CalledProcessError as e:
                logging.error(f"Git submodule sync failed: {e.stderr}")
            try:
                subprocess.run(['sudo', 'git', 'submodule', 'update', '--init', '--recursive', '--remote'], cwd=clone_path, check=True)
            except subprocess.CalledProcessError as e:
                logging.error(f"Git submodule update failed: {e.stderr}")

    if dry_run:
        logging.info(f"[Dry Run] Would create config.php in {clone_path}")
        logging.info(f"[Dry Run] Would set ownership of {clone_path} to www-data:www-data.")
    else:
        with open(os.path.join(clone_path, 'config.php'), 'w') as config_file:
            config_file.write(config_php)
        try:
            subprocess.run(['sudo', 'chown', 'www-data:www-data', clone_path, '-R'], check=True)
        except subprocess.CalledProcessError as e:
            logging.error(f"Setting folder ownership failed: {e.stderr}")

    logging.info("Finished git clone process")
    runtime_clone = int(time.time() - start)
    logging.info(f"Git clone completed in {runtime_clone} seconds.")

def f_dir_backup_git_clone(path, moodle, config_php, full_backup, folder_backup_path, repo, branch, sync_submodules):
    """Perform directory backup and then git clone."""
    logging.info("Starting directory backup and git clone.")
    f_dir_backup(path, moodle, full_backup, folder_backup_path)
    f_git_clone(path, moodle, config_php, repo, branch, sync_submodules)

def monitor_dump_progress(dump_file, stop_event, check_interval=5, log_interval=60, stagnation_threshold=60):
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
    stagnation_time = 0  # Counter for how long the file size hasn't changed
    last_log_time = 0  # Last time a log was written

    while not stop_event.is_set():
        if os.path.exists(dump_file):
            current_size = os.path.getsize(dump_file)
            now = time.time()

            if current_size == last_size:
                stagnation_time += check_interval  # Increment stagnation time
                if stagnation_time >= stagnation_threshold:
                    if now - last_log_time >= log_interval:
                        logging.warning(f"Database dump file size hasn't changed for {stagnation_time} seconds. Possible stall?")
                        last_log_time = now  # Update last log time
            else:
                stagnation_time = 0  # Reset stagnation counter
                if now - last_log_time >= log_interval:
                    logging.info(f"Database dump progress: {current_size / (1024 * 1024):.2f} MB")
                    last_log_time = now  # Update last log time

            last_size = current_size  # Update last known size

        time.sleep(check_interval)  # Check more frequently

    logging.info("Database dump monitoring stopped.")

def monitor_memory_usage(stop_event):
    """
    Monitors memory usage and logs more frequently as free memory decreases.
    :param stop_event: A threading event to signal the thread to stop.
    """
    
    while not stop_event.is_set():
        # Get memory statistics
        mem_line = next(line for line in os.popen('free -t -m').readlines() if line.startswith("Mem"))
        total_memory, used_memory, free_memory, shared_memory, buff_cached_memory, available_memory = map(int, mem_line.split()[1:7])

        if available_memory < 250:
            logging.critical(
                "CRITICAL MEMORY WARNING: Available memory is extremely low (%d MB)! "
                "System may soon become unstable.",
                available_memory
            )
            sleep_time = 0.5  # Log frequently in a critical state
        elif available_memory < 500:
            logging.warning(
                "LOW MEMORY WARNING: Available memory is below 500 MB (%d MB). "
                "System performance may degrade soon.",
                available_memory
            )
            sleep_time = 1  # Log at 1-second intervals
        elif available_memory < 1000:
            logging.info(
                "Memory Pressure: Available memory is below 1000 MB (%d MB).",
                available_memory
            )
            sleep_time = 2  # Log every 2 seconds
        elif free_memory < 250:
            logging.warning(
                "LOW FREE MEMORY: Free memory is low (%d MB), but available memory is still sufficient (%d MB).",
                free_memory, available_memory
            )
            sleep_time = 2  # Warning, but not critical
        elif free_memory < 500:
            logging.info(
                "Limited Free Memory: Free memory is below 500 MB (%d MB), but available memory is still sufficient (%d MB).",
                free_memory, available_memory
            )
            sleep_time = 5  # Log every 5 seconds
        else:
            sleep_time = 5  # Normal monitoring cycle

        # General memory stats logging for debugging
        logging.debug(
            "Memory Stats | Total: %d MB | Used: %d MB | Free: %d MB | Shared: %d MB | Buffers/Cached: %d MB | Available: %d MB",
            total_memory, used_memory, free_memory, shared_memory, buff_cached_memory, available_memory
        )

        time.sleep(sleep_time)  # Adjusted logging frequency

    logging.info("Memory monitoring stopped.")

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

def restart_webserver(action, cache=None):
    """start / stop the apache or nginx webserver, depending on which one is installed"""
    cache = cache or apt.Cache()
    if cache["apache2"].is_installed:
        webserver = "apache2"
    elif cache["nginx"].is_installed:
        webserver = "nginx"
    else:
        logging.warning("No supported web server found (Apache/Nginx).")
        return

    logging.info(f"Attempting to {action} the {webserver} service.")
    if dry_run:
        logging.info(f"[Dry Run] Would run: sudo systemctl {action} {webserver}")
    else:
        try:
            subprocess.run(['sudo', 'systemctl', action, webserver], check=True)
            logging.info(f"{webserver} service {action}ed successfully.")
        except subprocess.CalledProcessError as e:
            logging.error(f"{action}ing {webserver} failed: {e.stderr}")

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
    global runtime_backup, runtime_dump, runtime_clone, dry_run

    # Load configuration
    pwd = os.path.dirname(os.path.abspath(__file__))
    CONFIG_PATH = os.path.join(pwd, 'config.ini')
    CONFIG_TEMPLATE_PATH = os.path.join(pwd, 'config_template.ini')
    config = load_config(CONFIG_PATH, False)

    # Configure logging using the config file
    configure_logging(config)

    # Auto-update the script if enabled
    auto_update = config.get('settings', 'auto_update_script', fallback=False)
    if auto_update == "True":
        self_update(pwd, CONFIG_PATH, CONFIG_TEMPLATE_PATH)
    else:
        logging.info(SEPARATOR)
        if confirm("Pull MoodleUpdater from GitHub?", "n"):
            self_update(pwd, CONFIG_PATH, CONFIG_TEMPLATE_PATH)

    # Check if the configuration file exists
    if not os.path.exists(CONFIG_PATH):
        logging.error(f"Configuration file '{CONFIG_PATH}' not found.")
        if os.path.exists(CONFIG_TEMPLATE_PATH):
            shutil.copy(CONFIG_TEMPLATE_PATH, CONFIG_PATH)
            logging.info("Configuration file has been created.")
            logging.info("Please edit config.ini to your needs.")
        else:
            logging.error("Missing both config.ini and config_template.ini. Please create config.ini manually.")
        exit(1)

    # Reload configuration
    config = load_config(CONFIG_PATH, True)
    dry_run = config.get('settings', 'dry_run', fallback="False") == "True"
    moodle = config.get('settings', 'moodle', fallback='moodle')
    path = config.get('settings', 'path', fallback=pwd)
    configphppath = os.path.join(path, moodle, 'config.php')
    multithreading = False

    logging.info(SEPARATOR)

    # Log if dry-run mode is enabled
    if dry_run:
        logging.warning("[Dry Run] is enabled!")
        logging.info(SEPARATOR)

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

    restart_webserver_flag = confirm("Restart webserver automatically?", "y")
    verbose = confirm("Do you want to enable verbose mode?", default='n')

    if dir_backup or git_clone:
        logging.info("Preparing Moodle directory path.")
        if not confirm(f"Is this the correct Moodle directory? {path}", "y"):
            path = input("Please enter a path: ").rstrip("/")

    # Directory backup process
    if dir_backup:
        folder_backup_path = config.get('settings', 'folder_backup_path', fallback='pwd')
        if folder_backup_path in ["pwd", ""]:
            folder_backup_path = pwd
        if not folder_backup_path.endswith("/"):
            folder_backup_path = os.path.join(folder_backup_path, '')
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
            cfg = read_moodle_config(configphppath)
            dbname = cfg.get('dbname')
            dbuser = cfg.get('dbuser')
            dbpass = cfg.get('dbpass')

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

    if restart_webserver_flag:
        cache = apt.Cache()
        restart_webserver("stop", cache)

    # Handle multithreading if multiple operations were selected
    if dir_backup and db_dump and git_clone:
        multithreading = True

        t_backup_clone = threading.Thread(
            target=f_dir_backup_git_clone,
            args=(path, moodle, configphp, full_backup, folder_backup_path, repo, branch, sync_submodules,)
        )
        t_dump = threading.Thread(target=f_db_dump, args=(dbname, dbuser, dbpass, verbose, db_dump_path,))

        logging.info("Starting directory backup, git clone, and database dump (multithreaded).")
        t_backup_clone.start()
        t_dump.start()
        t_backup_clone.join()
        t_dump.join()
    elif dir_backup and db_dump:
        multithreading = True

        t_backup = threading.Thread(target=f_dir_backup, args=(path, moodle, full_backup, folder_backup_path,))
        t_dump = threading.Thread(target=f_db_dump, args=(dbname, dbuser, dbpass, verbose, db_dump_path,))

        logging.info("Starting directory backup and database dump (multithreaded).")
        t_backup.start()
        t_dump.start()
        t_backup.join()
        t_dump.join()
    elif db_dump and git_clone:
        multithreading = True

        t_dump = threading.Thread(target=f_db_dump, args=(dbname, dbuser, dbpass, verbose, db_dump_path,))
        t_clone = threading.Thread(target=f_git_clone, args=(path, moodle, configphp, repo, branch, sync_submodules,))

        logging.info("Starting database dump and git clone (multithreaded).")
        t_dump.start()
        t_clone.start()
        t_dump.join()
        t_clone.join()
    else:
        if dir_backup:
            logging.info("Starting directory backup")
            f_dir_backup(path, moodle, full_backup, folder_backup_path)

        if db_dump:
            logging.info("Starting database dump")
            f_db_dump(dbname, dbuser, dbpass, verbose, db_dump_path)

        if git_clone:
            logging.info("Starting git clone")
            f_git_clone(path, moodle, configphp, repo, branch, sync_submodules)

    if restart_webserver_flag:
        restart_webserver("start", cache)

    # Time calculation
    runtime = int(time.time() - start_time)  # Convert to integer seconds

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
    logging.info("Total time needed: %d seconds", runtime)
    if multithreading:
        logging.info("Time saved with multithreading: %d seconds", runtime_backup + runtime_dump + runtime_clone - runtime)

    logging.info("------------------------------------------------------------------------------------------")
    logging.info("Finished at %s", time.strftime("%Y-%m-%d %H:%M:%S"))

    if dry_run:
        logging.info(SEPARATOR)
        logging.info("[Dry Run] was enabled!")
        logging.info(SEPARATOR)

main()