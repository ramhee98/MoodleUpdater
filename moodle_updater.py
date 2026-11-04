# Standard Library
import errno
import fcntl
import getpass
import hashlib
import logging
import os
import stat
import sys
import time
from functools import partial

# Modules
from modules.application_setup import ApplicationSetup
from modules.cli import parse_args, resolve_option
from modules.db_tools import check_db_connection
from modules.moodle_backup import DEFAULT_CONFIG_PHP_MODE, MoodleBackupManager, is_within
from modules.config_manager import SEPARATOR, ConfigManager
from modules.moodle_version import MoodleVersionChecker
from modules.service_manager import ServiceManager
from modules.steps import run_step, run_steps_concurrently


# Root-only runtime directory for instance locks (real runs always run as root).
# MOODLE_UPDATER_LOCK_DIR overrides it, e.g. on systems without /run.
LOCK_DIR = os.environ.get("MOODLE_UPDATER_LOCK_DIR", "/run/moodle_updater")


def read_config_php(file_path):
    """Return (content, mode) of the config.php to carry over, or abort before
    anything has been changed. The mode is kept so the new copy is never more
    readable than the original; group/other write is always dropped."""
    try:
        with open(file_path, 'r') as file:
            content = file.read()
            mode = stat.S_IMODE(os.fstat(file.fileno()).st_mode) & 0o755
        return content, mode
    except OSError as e:
        logging.error(f"Cannot read {file_path}: {e}")
        sys.exit(1)


def acquire_instance_lock(instance_path, lock_dir=None):
    """Take a per-instance lock so two concurrent moodle_updater.py runs can't
    clobber each other's backups, dumps, or git clones for the same Moodle
    instance, whatever config file or backup folder they use. The lock is keyed
    by the canonical path of the Moodle code directory and lives in a private
    directory; it is released when the returned file object is closed or the
    process ends. Aborts if it is held or the lock location is not trustworthy."""
    lock_dir = lock_dir or LOCK_DIR
    instance = os.path.realpath(instance_path)
    lock_path = os.path.join(lock_dir, hashlib.sha256(instance.encode()).hexdigest()[:16] + ".lock")
    try:
        os.makedirs(lock_dir, mode=0o700, exist_ok=True)
        st = os.lstat(lock_dir)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022:
            logging.error(f"Lock directory {lock_dir} must be a directory owned by this user and not "
                          "writable by others. Aborting.")
            sys.exit(1)
        # O_NOFOLLOW: never follow a planted symlink; open without truncating, the
        # PID inside belongs to whoever holds the lock.
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        logging.error(f"Cannot open lock file {lock_path}: {e}. Aborting.")
        sys.exit(1)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        logging.error(f"Lock file {lock_path} is not a regular file. Aborting.")
        sys.exit(1)
    lock_fp = os.fdopen(fd, "r+")
    try:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EAGAIN, errno.EACCES):
            holder = lock_fp.read().split(maxsplit=1)[:1] or ["unknown"]
            logging.error(
                f"Another moodle_updater.py run (PID {holder[0]}) is already in progress for "
                f"{instance} (lock file: {lock_path}). Aborting."
            )
            sys.exit(1)
        raise
    lock_fp.truncate(0)
    lock_fp.write(f"{os.getpid()} {instance}\n")
    lock_fp.flush()
    return lock_fp


def prompt_db_password(prompt):
    """Ask for the DB password without echoing it to the terminal."""
    return getpass.getpass(prompt).strip()


def ensure_db_connection(dbname, dbuser, dbpass, non_interactive, dbhost=None, max_attempts=3):
    """Check the DB credentials, re-prompting for the password interactively.
    Returns the working password or aborts the run."""
    for attempt in range(1, max_attempts + 1):
        try:
            ok, error = check_db_connection(dbname, dbuser, dbpass, dbhost)
        except FileNotFoundError:
            logging.error("Neither mariadb-show nor mysqlshow found. Install the MariaDB/MySQL client tools.")
            sys.exit(1)
        if ok:
            logging.info("Connection to DB established.")
            return dbpass
        logging.error(f"Connection to DB failed: {error}")
        if non_interactive or attempt == max_attempts:
            sys.exit(1)
        dbpass = ""
        while not dbpass:
            dbpass = prompt_db_password("Please enter DB password again: ")
    sys.exit(1)


def log_run_summary(backup_manager, runtime, lanes_runtime=None):
    """Log step durations and the submodule, plugin and upgrade summaries.
    lanes_runtime is the wall time of the parallel phase, or None if nothing ran in parallel."""
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
        logging.info("Moodle CLI Upgrade time needed: %d seconds", runtime_cliupgrade)
    else:
        runtime_cliupgrade = 0

    runtime_restore_plugins = backup_manager.runtime_restore_plugins
    if runtime_restore_plugins:
        logging.info("Plugin restore time needed: %d seconds", runtime_restore_plugins)
    else:
        runtime_restore_plugins = 0

    # Log total runtime
    logging.info("Total execution time (excluding user input): %d seconds", runtime)
    if lanes_runtime is not None:
        # Only backup, clone and dump run in parallel; compare their summed
        # durations with the wall time of that phase alone.
        logging.info("Time saved with multithreading: %d seconds", runtime_backup + runtime_dump + runtime_clone - lanes_runtime)

    # Log failed submodules summary at the end if any failed
    if backup_manager.failed_submodules:
        logging.info(SEPARATOR)
        logging.warning(f"SUBMODULE SYNC SUMMARY: {backup_manager.submodules_failed} submodule(s) failed to update")
        logging.warning(f"Failed submodules: {', '.join(backup_manager.failed_submodules)}")

    # Log plugin restore summary at the end if it ran
    if backup_manager.restored_plugins or backup_manager.skipped_plugins:
        logging.info(SEPARATOR)
        logging.info(f"PLUGIN RESTORE SUMMARY: {len(backup_manager.restored_plugins)} restored, {len(backup_manager.skipped_plugins)} already present")
        if backup_manager.restored_plugins:
            logging.info(f"Restored plugins: {', '.join(backup_manager.restored_plugins)}")

    # Log upgrade failure summary at the end if upgrade failed
    if backup_manager.upgrade_failed:
        logging.info(SEPARATOR)
        logging.warning("MOODLE CLI UPGRADE SUMMARY: Upgrade failed!")
        for error in backup_manager.upgrade_error_details:
            logging.warning(f"  - {error}")


def read_db_settings(config, configphppath, non_interactive):
    """Return (dbname, dbuser, dbpass, dbhost) from config.php or config.ini,
    asking for the password when needed. Aborts if they are incomplete."""
    dbpass = ""
    read_db_from_config = ConfigManager.get_bool(config, 'database', 'read_db_from_config', fallback=True)

    if not read_db_from_config:
        dbname = config.get('database', 'db_name', fallback='moodle')
        dbuser = config.get('database', 'db_user', fallback='root')
        dbhost = config.get('database', 'db_host', fallback='').strip()
        if non_interactive:
            # No prompt possible: take the password from the environment,
            # which is also where the MySQL/MariaDB client tools look for it.
            dbpass = os.environ.get('MYSQL_PWD', '')
            if not dbpass:
                logging.error("read_db_from_config is False and --non-interactive is set: "
                              "provide the DB password in the MYSQL_PWD environment variable.")
                sys.exit(1)
        while not dbpass:
            dbpass = prompt_db_password("Please enter DB password: ")
    else:
        cfg = ConfigManager.read_moodle_config(configphppath)
        dbname = cfg.get('dbname')
        dbuser = cfg.get('dbuser')
        dbpass = cfg.get('dbpass')
        dbhost = cfg.get('dbhost')
        missing = [key for key in ('dbname', 'dbuser', 'dbpass') if cfg.get(key) is None]
        if missing:
            logging.error(f"Could not read {', '.join(missing)} from {configphppath}. "
                          "Set read_db_from_config = False to configure the database in config.ini instead.")
            sys.exit(1)
    return dbname, dbuser, dbpass, dbhost


def main():
    # Parse arguments before anything else so --help works without config,
    # prompts or root, and --dry-run is known before the root check.
    args = parse_args()
    non_interactive = args.non_interactive
    pwd = os.path.dirname(os.path.abspath(__file__))

    # Initialize Application Setup
    setup = ApplicationSetup(
        pwd=pwd,
        config_path=os.path.join(pwd, 'config.ini'),
        config_template_path=os.path.join(pwd, 'config_template.ini'),
        non_interactive=non_interactive,
        dry_run=args.dry_run,
    )

    config = setup.config
    dry_run = setup.dry_run
    moodle = setup.moodle
    path = setup.path
    full_path = setup.full_path
    configphppath = setup.configphppath
    chown_user = config.get('settings', 'chown_user', fallback="www-data")
    chown_group = config.get('settings', 'chown_group', fallback="www-data")

    # Get user confirmation for operations
    # alternatively to the confirm action, command line arguments can be used
    dir_backup = resolve_option(args.directory_backup, "Start directory backup process?", "y", non_interactive)
    db_dump = resolve_option(args.db_dump, "Start DB dump process?", "y", non_interactive)
    git_clone = resolve_option(args.git_clone, "Start git clone process?", "y", non_interactive)
    moodle_cli_upgrade = resolve_option(args.moodle_cli_upgrade, "Start moodle cli upgrade process afterwards?", "y", non_interactive)

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

    # Settle the Moodle path before anything (backup manager, config.php path)
    # is derived from it.
    if dir_backup or git_clone:
        logging.info("Preparing Moodle directory path.")
        if not non_interactive and not ApplicationSetup.confirm(f"Is this the correct Moodle directory? {path}", "y"):
            path = input("Please enter a path: ").rstrip("/")
            full_path = os.path.join(path, moodle)
            configphppath = os.path.join(full_path, 'config.php')
            logging.info(f"Using Moodle directory {full_path}")

    folder_backup_path = config.get('settings', 'folder_backup_path', fallback='pwd')
    if folder_backup_path in ["pwd", ""]:
        folder_backup_path = pwd
    if not folder_backup_path.endswith("/"):
        folder_backup_path = os.path.join(folder_backup_path, '')
    db_dump_path = config.get('settings', 'db_dump_path', fallback='pwd')
    if db_dump_path in ["pwd", ""]:
        db_dump_path = pwd

    # The git clone replaces the Moodle code directory and a partial backup copies
    # it: backups or dumps stored inside it, from this run or earlier ones, would be
    # deleted or copied into themselves.
    if dir_backup or git_clone:
        for setting, location in (("folder_backup_path", folder_backup_path), ("db_dump_path", db_dump_path)):
            if is_within(location, full_path):
                logging.error(f"{setting} ({location}) is inside the Moodle code directory {full_path}. "
                              "Choose a location outside of it.")
                sys.exit(1)

    backup_manager = MoodleBackupManager(
        path=path,
        moodle=moodle,
        folder_backup_path=folder_backup_path,
        dry_run=dry_run,
        non_interactive=non_interactive,
        php_cli_user=config.get('settings', 'php_cli_user', fallback='').strip(),
        # Always passed: earlier dumps there must stay out of full backups too.
        db_dump_path=db_dump_path,
    )

    # Keep the lock object referenced for the whole run; closing it releases the lock.
    # A dry run changes nothing, so it neither needs nor creates a lock.
    if dry_run:
        logging.info("[Dry Run] Not taking the instance lock.")
    else:
        os.makedirs(folder_backup_path, exist_ok=True)
        _instance_lock = acquire_instance_lock(full_path)

    restart_webserver_flag = resolve_option(args.restart_webserver, "Restart webserver automatically?", "y", non_interactive)

    restart_database_flag = False
    moodle_maintenance_mode_flag = False
    restore_submodules_from_backup = False
    restore_plugins_from_backup = False
    # "auto" restores every missing plugin; "manual" prompts y/n per plugin.
    restore_plugins_mode = "auto"

    verbose = resolve_option(args.verbose, "Do you want to enable verbose mode?", "n", non_interactive)

    # Directory backup process
    full_backup = False
    if dir_backup:
        full_backup = resolve_option(args.full_backup, "Backup entire folder (containing moodle, moodledata, and data)?", "n", non_interactive)

    # Database dump process
    if db_dump:
        dbname, dbuser, dbpass, dbhost = read_db_settings(config, configphppath, non_interactive)

        restart_database_flag = resolve_option(args.restart_database, "Restart database before dump?", "n", non_interactive)

        if dry_run:
            logging.info(f"[Dry Run] Would run: mysqlshow to check if DB: {dbname} is accessible with user: {dbuser} on host: {dbhost or 'localhost'}")
        else:
            dbpass = ensure_db_connection(dbname, dbuser, dbpass, non_interactive, dbhost)

    # Git clone process
    if git_clone:
        logging.info("Preparing git clone process.")
        repo = config.get('settings', 'repo')
        branch = config.get('settings', 'branch')

        # Settle the branch before the version check uses it.
        if not non_interactive and not ApplicationSetup.confirm(f"Do you want to git checkout {branch}?", "y"):
            branch = input("Please enter custom branch: ")

        # Early, advisory check against the remote; the checked-out tree is checked
        # again before it goes live (refusing a downgrade even if this one can't run).
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

        # None means "do not write a config.php into the new clone".
        configphp, configphp_mode = None, DEFAULT_CONFIG_PHP_MODE
        if not non_interactive and not ApplicationSetup.confirm(f"Do you want to copy {configphppath} from the old directory?", "y"):
            customconfigphppath = input("Please enter a config.php path [press enter to skip]: ")
            if customconfigphppath:
                configphp, configphp_mode = read_config_php(customconfigphppath)
            else:
                logging.info("Restore of old config.php skipped.")
        else:
            configphp, configphp_mode = read_config_php(configphppath)

        if args.sync_submodules_off:
            sync_submodules = False
        else:
            sync_submodules = resolve_option(False, "Do you want to sync and update all submodules?", "y", non_interactive,
                                             non_interactive_default=True)

        if args.restore_submodules:
            if dir_backup:
                restore_submodules_from_backup = True
                sync_submodules = False
            else:
                logging.error("Cannot restore submodules from backup without directory backup. Please enable --directory-backup to restore submodules from backup.")
                sys.exit(1)
        elif not non_interactive and not sync_submodules and dir_backup:
            restore_submodules_from_backup = ApplicationSetup.confirm("Do you want to restore submodules from the old directory backup?", "y")
        else:
            restore_submodules_from_backup = False

        if args.restore_plugins:
            if dir_backup:
                restore_plugins_from_backup = True
            else:
                logging.error("Cannot restore plugins from backup without directory backup. Please enable --directory-backup to restore plugins from backup.")
                sys.exit(1)
        elif not non_interactive:
            restore_plugins_from_backup = ApplicationSetup.confirm("Do you want to restore third-party plugins from the old directory backup?")
            if restore_plugins_from_backup and not dir_backup:
                # Plugin restore needs a directory backup to copy from — force an explicit y/n.
                enable_dir_backup = ApplicationSetup.confirm("Restoring plugins requires a directory backup. Enable directory backup now?", "y")
                if enable_dir_backup:
                    dir_backup = True
                    full_backup = False
                    logging.info("Directory backup enabled to support plugin restore.")
                else:
                    restore_plugins_from_backup = False
                    logging.info("Plugin restore skipped because directory backup was not enabled.")
        else:
            restore_plugins_from_backup = False

        # Decide how plugins get selected: restore everything, or choose per plugin.
        if restore_plugins_from_backup:
            if args.restore_plugins_manual:
                if non_interactive:
                    logging.error("--restore-plugins-manual requires interactive input and cannot be used in non-interactive mode.")
                    sys.exit(1)
                restore_plugins_mode = "manual"
            elif not non_interactive and not args.restore_plugins:
                # Only prompt for the mode when the restore itself was chosen interactively.
                restore_all = ApplicationSetup.confirm(
                    "Restore all missing plugins automatically? (No lets you choose each one)")
                restore_plugins_mode = "auto" if restore_all else "manual"

    if not git_clone:
        for flag, enabled in (("--restore-submodules", args.restore_submodules), ("--restore-plugins", args.restore_plugins)):
            if enabled:
                logging.warning(f"{flag} has no effect without --git-clone; ignoring it.")

    if moodle_cli_upgrade:
        moodle_maintenance_mode_flag = resolve_option(args.enable_maintenance_mode, "Enable Moodle Maintenance Mode during Moodle CLI Upgrade?", "y", non_interactive)
        force_continue = resolve_option(args.force_continue, "Auto-continue even if Moodle system check reports errors?", "n", non_interactive)

    if not non_interactive and not ApplicationSetup.confirm("Do you want to confirm the installation?"):
        logging.warning("User canceled the operation.")
        sys.exit(1)
    # Start operations
    start_time = time.time()
    logging.info(f"Started at {time.strftime('%Y-%m-%d %H:%M:%S')}")

    service_manager = ServiceManager(dry_run)  # Create an instance

    # Everything from stopping the webserver to starting it again runs inside
    # try/finally, so the site comes back even after an unexpected error, sys.exit
    # or Ctrl-C (also during the stop itself). The webserver stays down until the
    # CLI upgrade and its checks are done, so no request ever reaches new code
    # running against an old or half-upgraded database.
    failed_steps = []
    webserver_stop_attempted = False
    multithreading, lanes_runtime = False, 0
    try:
        if restart_webserver_flag:
            webserver_stop_attempted = True
            if not service_manager.restart_webserver("stop"):
                failed_steps.append("Stopping the webserver")

        if restart_database_flag and not failed_steps:
            if service_manager.restart_database("restart"):
                time.sleep(2)  # Pause to ensure the DB is fully ready
            else:
                failed_steps.append("Restarting the database")

        if failed_steps:
            logging.error(f"Not starting the update because a preparation step failed: {', '.join(failed_steps)}. "
                          "Nothing was changed.")
        else:
            # Build independent lanes of work. Backup and clone share a lane because
            # the clone replaces the code the backup protects; the dump runs alongside.
            # The clone is only *prepared* here: it goes live below, once every lane
            # (the dump included) has succeeded.
            lanes = []
            if dir_backup and git_clone:
                lanes.append(("Directory backup and git clone", partial(
                    backup_manager.dir_backup_and_prepare_clone, configphp, full_backup, repo, branch,
                    sync_submodules, restore_submodules_from_backup, configphp_mode)))
            elif dir_backup:
                lanes.append(("Directory backup", partial(backup_manager.dir_backup, full_backup)))
            elif git_clone:
                lanes.append(("Git clone", partial(
                    backup_manager.prepare_clone, configphp, repo, branch, sync_submodules,
                    restore_submodules_from_backup, False, configphp_mode)))
            if db_dump:
                lanes.append(("Database dump", partial(backup_manager.db_dump, dbname, dbuser, dbpass, verbose, db_dump_path, dbhost)))

            multithreading = len(lanes) > 1
            lanes_start = time.time()
            if multithreading:
                logging.info(f"Starting {' and '.join(name.lower() for name, _ in lanes)} (multithreaded).")
                results = run_steps_concurrently(lanes)
            else:
                results = {}
                for name, func in lanes:
                    logging.info(f"Starting {name.lower()}")
                    results[name] = run_step(name, func)
            lanes_runtime = int(time.time() - lanes_start)
            failed_steps.extend(name for name, ok in results.items() if not ok)

            if git_clone:
                if failed_steps:
                    logging.error(f"Not putting the new code in place because a previous step failed: {', '.join(failed_steps)}. "
                                  "The existing Moodle code was left untouched.")
                    backup_manager.discard_clone()
                elif restore_plugins_from_backup and not run_step(
                        "Plugin restore", backup_manager.restore_plugins, chown_user, chown_group,
                        full_backup=full_backup if dir_backup else False, selection_mode=restore_plugins_mode,
                        target_root=backup_manager.staged_clone):
                    failed_steps.append("Plugin restore")
                    logging.error("Not putting the new code in place because the plugin restore failed. "
                                  "The existing Moodle code was left untouched.")
                    backup_manager.discard_clone()
                elif not run_step("Putting the new code in place", backup_manager.commit_clone, chown_user, chown_group):
                    failed_steps.append("Putting the new code in place")

            if moodle_cli_upgrade:
                if failed_steps:
                    # The upgrade changes the database schema and cannot be undone; never
                    # run it without the backups and code it depends on.
                    logging.error(f"Skipping Moodle CLI upgrade because a previous step failed: {', '.join(failed_steps)}. "
                                  "Fix the problem and re-run with --moodle-cli-upgrade.")
                    failed_steps.append("Moodle CLI upgrade (skipped)")
                elif not run_step("Moodle CLI upgrade", backup_manager.moodle_cli_upgrade, moodle_maintenance_mode_flag, force_continue):
                    failed_steps.append("Moodle CLI upgrade")
    finally:
        # A clone that was prepared but never put in place (failure, Ctrl-C) holds a
        # copy of config.php: remove it.
        backup_manager.discard_clone()
        if webserver_stop_attempted and not service_manager.restart_webserver("start"):
            failed_steps.append("Starting the webserver")
        if backup_manager.live_tree_missing:
            logging.critical(f"{full_path} is missing; see the CRITICAL message above for how to restore the previous code.")

    runtime = int(time.time() - start_time)  # Convert to integer seconds
    log_run_summary(backup_manager, runtime, lanes_runtime if multithreading else None)
    if backup_manager.maintenance_disable_failed:
        logging.critical("Moodle maintenance mode is still enabled; see the CRITICAL message above.")

    if failed_steps:
        logging.info(SEPARATOR)
        logging.error(f"FAILED STEPS: {', '.join(failed_steps)}")

    logging.info(SEPARATOR)
    logging.info("Finished at %s", time.strftime("%Y-%m-%d %H:%M:%S"))

    if dry_run:
        logging.info(SEPARATOR)
        logging.info("[Dry Run] was enabled!")
        logging.info(SEPARATOR)

    sys.exit(1 if failed_steps else 0)

if __name__ == "__main__":
    main()
