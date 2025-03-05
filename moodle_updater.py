# Standard Library
import logging
import os
import subprocess
import sys
import threading
import time

# Third-Party Libraries
from logging.handlers import RotatingFileHandler

# Modules
from modules.application_setup import ApplicationSetup
from modules.moodle_backup import MoodleBackupManager
from modules.config_manager import ConfigManager
from modules.moodle_version import MoodleVersionChecker
from modules.service_manager import ServiceManager

# Constants
SEPARATOR = "-------------------------------------------------------------------------"

def main():
    dry_run = False
    pwd = os.path.dirname(os.path.abspath(__file__))

    # Initialize Application Setup
    setup = ApplicationSetup(
        pwd=pwd,
        config_path=os.path.join(pwd, 'config.ini'),
        config_template_path=os.path.join(pwd, 'config_template.ini')
    )

    config = setup.config
    dry_run = setup.dry_run
    moodle = setup.moodle
    path = setup.path
    full_path = setup.full_path
    configphppath = setup.configphppath
    multithreading = False

    # Get user confirmation for operations
    dir_backup = ApplicationSetup.confirm("Start directory backup process?", "y")
    db_dump = ApplicationSetup.confirm("Start DB dump process?", "y")
    git_clone = ApplicationSetup.confirm("Start git clone process?", "y")

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

    restart_webserver_flag = ApplicationSetup.confirm("Restart webserver automatically?", "y")
    restart_database_flag = False
    verbose = ApplicationSetup.confirm("Do you want to enable verbose mode?", default='n')

    if dir_backup or git_clone:
        logging.info("Preparing Moodle directory path.")
        if not ApplicationSetup.confirm(f"Is this the correct Moodle directory? {path}", "y"):
            path = input("Please enter a path: ").rstrip("/")
            full_path = os.path.join(path, moodle)

    # Directory backup process
    if dir_backup:
        full_backup = ApplicationSetup.confirm("Backup entire folder (containing moodle, moodledata, and data)?", "n")

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
        
        restart_database_flag = ApplicationSetup.confirm("Restart database before dump?", "n")

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

        if not ApplicationSetup.confirm(f"Do you want to copy {configphppath} from the old directory?", "y"):
            customconfigphppath = input("Please enter a config.php path [press enter to skip]: ")
            if customconfigphppath:
                with open(customconfigphppath, 'r') as file:
                    configphp = file.read()
            else:
                logging.info("Restore of old config.php skipped.")
        else:
            with open(configphppath, 'r') as file:
                configphp = file.read()

        if not ApplicationSetup.confirm(f"Do you want to git checkout {branch}?", "y"):
            branch = input("Please enter custom branch: ")

        sync_submodules = ApplicationSetup.confirm("Do you want to sync and update all submodules?", "y")

    if not ApplicationSetup.confirm("Do you want to confirm the installation?"):
        logging.warning("User canceled the operation.")
        exit(1)
    # Start operations
    start_time = time.time()
    logging.info(f"Started at {time.strftime('%Y-%m-%d %H:%M:%S')}")

    service_manager = ServiceManager(dry_run)  # Create an instance

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

if __name__ == "__main__":
    main()