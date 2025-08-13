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
    chown_user = config.get('settings', 'chown_user', fallback="www-data")
    chown_group = config.get('settings', 'chown_group', fallback="www-data")

    if "--dry-run" in sys.argv:
        dry_run = True
        logging.info("Running in dry run mode. No changes will be made.")

    # Check if --help is in the arguments
    if "--help" in sys.argv or "-h" in sys.argv:
        print("Usage: python3 moodle_updater.py [options]")
        print("Options:")
        print("  --non-interactive         Run in non-interactive mode (default: False)")
        print("  --directory-backup        Start directory backup process (default: True unless non-interactive is set then default: False)")
        print("  --db-dump                 Start DB dump process (default: True unless non-interactive is set then default: False)")
        print("  --git-clone               Start git clone process (default: True unless non-interactive is set then default: False)")
        print("  --moodle-cli-upgrade      Start Moodle CLI upgrade process afterwards (default: True unless non-interactive is set then default: False)")
        print("  --enable-maintenance-mode Enable Moodle Maintenance Mode during CLI Upgrade (default: True unless non-interactive is set then default: False)")
        print("  --force-continue          Auto-continue even if system check reports errors (default: False)")
        print("  --restart-webserver       Restart webserver automatically (default: True unless non-interactive is set then default: False)")
        print("  --restart-database        Restart database before dump (default: False)")
        print("  --verbose                 Enable verbose mode (default: False)")
        print("  --full-backup             Backup entire folder (containing moodle, moodledata, and data) (default: False) (used only if --directory-backup is set)")
        print("  --sync-submodules-off     Disable syncing and updating of all submodules (default: False)")
        print("  --restore-submodules      Restore git submodules from backup (default: False) (needs --directory-backup enabled) (if enabled --sync-submodules-off is set to True)")
        print("  --dry-run                 Run in dry run mode (default: False)")
        print("  --help, -h                Show this help message")
        sys.exit(0)

    non_interactive = False
    # Check for non-interactive mode
    if "--non-interactive" in sys.argv:
        non_interactive = True

    # Get user confirmation for operations
    # alternatively to the confirm action, command line arguments can be used
    if "--directory-backup" in sys.argv:
        dir_backup = True
    elif not non_interactive:
        dir_backup = ApplicationSetup.confirm("Start directory backup process?", "y")
    else:
        dir_backup = False

    if "--db-dump" in sys.argv:
        db_dump = True
    elif not non_interactive:
        db_dump = ApplicationSetup.confirm("Start DB dump process?", "y")
    else:
        db_dump = False
    
    if "--git-clone" in sys.argv:
        git_clone = True
    elif not non_interactive:
        git_clone = ApplicationSetup.confirm("Start git clone process?", "y")
    else:
        git_clone = False
    
    if "--moodle-cli-upgrade" in sys.argv:
        moodle_cli_upgrade = True
    elif not non_interactive:
        moodle_cli_upgrade = ApplicationSetup.confirm("Start moodle cli upgrade process afterwards?", "y")
    else:
        moodle_cli_upgrade = False

    logging.info(SEPARATOR)
    logging.info(f"dirbackup: {dir_backup}")
    logging.info(f"dbdump: {db_dump}")
    logging.info(f"gitclone: {git_clone}")
    logging.info(f"moodlecliupgrade: {moodle_cli_upgrade}")
    logging.info(SEPARATOR)

    # Abort if no tasks were selected
    if not dir_backup and not db_dump and not git_clone and not moodle_cli_upgrade:
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

    if "--restart-webserver" in sys.argv:
        restart_webserver_flag = True
    elif not non_interactive:
        restart_webserver_flag = ApplicationSetup.confirm("Restart webserver automatically?", "y")
    else:
        restart_webserver_flag = False

    restart_database_flag = False
    moodle_maintenance_mode_flag = False
    restore_submodules_from_backup = False

    if "--verbose" in sys.argv:
        verbose = True
    elif not non_interactive:
        verbose = ApplicationSetup.confirm("Do you want to enable verbose mode?", default='n')
    else:
        verbose = False

    if dir_backup or git_clone:
        logging.info("Preparing Moodle directory path.")
        if not non_interactive and not ApplicationSetup.confirm(f"Is this the correct Moodle directory? {path}", "y"):
            path = input("Please enter a path: ").rstrip("/")
            full_path = os.path.join(path, moodle)

    # Directory backup process
    if dir_backup:
        if "--full-backup" in sys.argv:
            full_backup = True
        elif not non_interactive:
            full_backup = ApplicationSetup.confirm("Backup entire folder (containing moodle, moodledata, and data)?", "n")
        else:
            full_backup = False

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
      
        if "--restart-database" in sys.argv:
            restart_database_flag = True
        elif not non_interactive:
            restart_database_flag = ApplicationSetup.confirm("Restart database before dump?", "n")
        else:
            restart_database_flag = False

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

        if not non_interactive and not ApplicationSetup.confirm(f"Do you want to copy {configphppath} from the old directory?", "y"):
            customconfigphppath = input("Please enter a config.php path [press enter to skip]: ")
            if customconfigphppath:
                with open(customconfigphppath, 'r') as file:
                    configphp = file.read()
            else:
                logging.info("Restore of old config.php skipped.")
        else:
            with open(configphppath, 'r') as file:
                configphp = file.read()

        if not non_interactive and not ApplicationSetup.confirm(f"Do you want to git checkout {branch}?", "y"):
            branch = input("Please enter custom branch: ")

        if "--sync-submodules-off" in sys.argv:
            sync_submodules = False
        elif not non_interactive:
            sync_submodules = ApplicationSetup.confirm("Do you want to sync and update all submodules?", "y")
        else:
            sync_submodules = True

        if "--restore-submodules" in sys.argv:
            if dir_backup:
                restore_submodules_from_backup = True
                sync_submodules = False
            else:
                logging.error("Cannot restore submodules from backup without directory backup. Skipping. Please enable --directory-backup to restore submodules from backup.")
                exit(1)
        elif not non_interactive and not sync_submodules and not non_interactive and dir_backup:
            restore_submodules_from_backup = ApplicationSetup.confirm("Do you want to restore submodules from the old directory backup?", "y")
        else:
            restore_submodules_from_backup = False

    if moodle_cli_upgrade:
        if "--enable-maintenance-mode" in sys.argv:
            moodle_maintenance_mode_flag = True
        elif not non_interactive:
            moodle_maintenance_mode_flag = ApplicationSetup.confirm("Enable Moodle Maintenance Mode during Moodle CLI Upgrade?", "y")
        else:
            moodle_maintenance_mode_flag = False

        if "--force-continue" in sys.argv:
            force_continue = True
        elif not non_interactive:
            force_continue = ApplicationSetup.confirm("Auto-continue even if Moodle system check reports errors?", "n")
        else:
            force_continue = False

    if not non_interactive and not ApplicationSetup.confirm("Do you want to confirm the installation?"):
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
            args=(configphp, full_backup, repo, branch, sync_submodules, chown_user, chown_group, restore_submodules_from_backup,)
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
        t_clone = threading.Thread(target=backup_manager.git_clone, args=(configphp, repo, branch, sync_submodules, chown_user, chown_group, restore_submodules_from_backup,))

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
            backup_manager.git_clone(configphp, repo, branch, sync_submodules, chown_user, chown_group, restore_submodules_from_backup)

    if restart_webserver_flag:
        service_manager.restart_webserver("start")

    if moodle_cli_upgrade:
        backup_manager.moodle_cli_upgrade(moodle_maintenance_mode_flag, force_continue)
        if restart_webserver_flag:
            service_manager.restart_webserver("restart")

    runtime = int(time.time() - start_time)  # Convert to integer seconds
    runtime_backup = backup_manager.runtime_backup
    runtime_dump = backup_manager.runtime_dump
    runtime_clone = backup_manager.runtime_clone
    runtime_cliupgrade = backup_manager.runtime_cliupgrade

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

    if runtime_cliupgrade:
        logging.info("Moodle CLI Upgrade time needed: %d seconds", runtime_clone)
    else:
        runtime_cliupgrade = 0

    # Log total runtime
    logging.info("Total execution time (excluding user input): %d seconds", runtime)
    if multithreading:
        logging.info("Time saved with multithreading: %d seconds", runtime_backup + runtime_dump + runtime_clone + runtime_cliupgrade - runtime)

    logging.info(SEPARATOR)
    logging.info("Finished at %s", time.strftime("%Y-%m-%d %H:%M:%S"))

    if dry_run:
        logging.info(SEPARATOR)
        logging.info("[Dry Run] was enabled!")
        logging.info(SEPARATOR)

if __name__ == "__main__":
    main()