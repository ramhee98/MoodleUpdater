# MoodleUpdater

**MoodleUpdater** is a script designed to automate the process of updating a Moodle instance. It supports directory and database backups, cloning the Moodle repository, and restoring configuration files. With multithreading, the script can efficiently handle multiple tasks simultaneously.

## Features

- **Configuration Management**:
  - Centralized configuration using a `config.ini` file.
  - Easily specify repository URL, branch, Moodle path, and folder name.

- **Backup Functionality**:
  - Perform a full or partial backup of your Moodle directory with `rsync -a`, which keeps symlinks, permissions, ownership and timestamps.
  - Backup the Moodle database using `mariadb-dump` / `mysqldump`.

- **Git Integration**:
  - Clone the configured Moodle repository (for example from GitHub).
  - Checkout specific branches and sync submodules.
  - Submodules are updated individually and every failure is logged. If any submodule fails to sync or update, the new clone is not used: the existing code stays live and the run exits with code 1. A missing submodule usually means a missing plugin.
  - A summary of failed submodule updates is displayed at the end of the process.
  - Before updating Moodle, MoodleUpdater compares the local Moodle version with the version in the configured Git repository and branch. It refuses to "update" to an older version.

- **Automation**:
  - Multithreaded execution for combined tasks.
  - Prompts to confirm actions with optional default responses.
  - Optionally stop Apache or Nginx for the whole update, including the Moodle CLI upgrade and its checks, and start it again at the end.
  - Optionally perform a Moodle CLI upgrade via `admin/cli/upgrade.php`, with pre-upgrade and post-upgrade system checks.
  - The script runs `admin/cli/checks.php` before and after the upgrade to detect potential issues. Its exit code decides: 0 (OK) and 1 (warnings) are fine. 2 (error/critical), 3 (unknown), any other code, or a missing script count as a failed check.
  - Optionally enable Moodle Maintenance Mode during Moodle CLI upgrade.
  - Optionally restart the MariaDB/MySQL service before a database dump.

- **User and Group Ownership**:
  - Optionally set the user and group ownership for Moodle files and directories after cloning or updating Moodle.
  - Configure the ownership settings through the `chown_user` and `chown_group` options in `config.ini` to ensure proper file permissions (e.g., `www-data`).

- **User-Friendly**:
  - Guided prompts for paths, database credentials, and other configurations.
  - Default values and skip options for most steps.

- **Update MoodleUpdater with Git Pull**:
  - Optionally pull the latest version of the script from the Git repository on start.
  - Configurable through the `auto_update_script` setting in `config.ini`. When enabled, the script automatically checks for updates and pulls them if no local changes are detected.
  - If `auto_update_script` is disabled, the user is asked whether to pull MoodleUpdater from GitHub (not in `--non-interactive` mode).
  - Compares `config.ini` with `config_template.ini` and reports settings that are missing from `config.ini` (for example new options) or unknown to the template.
  - Retrieve and display detailed information about the current commit (time, author, summary).
  - Automatically checks and reports the branch name, current commit details, and updated commit details after pulling changes.
  - After successfully pulling updates from the Git repository, the script automatically restarts itself. This ensures that the latest changes take effect immediately, eliminating the need for manual restarts.
  - Updates are detected by comparing the commit before and after the pull, so this works with any system language.
  - **Security note**: self-update runs `git pull` as root and then executes the pulled code without verifying it. Anyone who can push to the configured remote, or who can write to the MoodleUpdater directory, can run code as root on your server. Keep the checkout owned by root and not writable by other users (`chown -R root:root /path/to/MoodleUpdater && chmod -R go-w /path/to/MoodleUpdater`), and only enable `auto_update_script` if you trust the remote. The script logs a `SECURITY` warning when it runs as root from a directory other users can modify.

- **Progress and Memory Monitoring**:
  - During a database dump, the script actively monitors the dump file progress and system memory usage in real-time. One monitoring thread checks if the dump file is increasing in size (and warns if progress stalls), while another thread tracks available and free memory. This dual monitoring ensures that any potential stalls or memory issues are detected early, helping to maintain system stability during the update process.

- **Enhanced Logging**:
  - Configurable logging with options for console and file output.
  - Supports log rotation and adjustable logging levels for better debugging and monitoring.
  - The script measures and logs the execution time for key operations—directory backup, database dump, and Git clone. It also records the total runtime and calculates the time saved through multithreading when multiple tasks run concurrently. These detailed statistics provide valuable insights into the performance and efficiency of the update process.

- **Modular Architecture**:
  - Built with a modular design using separate components for different functionalities:
    - `application_setup.py` - Handles initial configuration and setup
    - `cli.py` - Command-line options and the "flag or prompt" logic
    - `config_manager.py` - Manages configuration file operations
    - `db_tools.py` - MySQL/MariaDB client helpers (tool detection, connection check)
    - `git_manager.py` - Handles all Git-related operations
    - `moodle_backup.py` - Manages backup operations
    - `moodle_version.py` - Version checking and comparison
    - `service_manager.py` - Web server and database service management
    - `steps.py` - Runs update steps (also in parallel) and records failures
    - `system_monitor.py` - System resource monitoring during operations

## Requirements

- **Operating System**: Debian-based Linux (e.g., Debian, Ubuntu)
- **Dependencies**:
  - `python3` (3.10 or newer)
  - `python3-requests` (remote version check)
  - `python3-apt` (detecting the installed webserver and database, only needed with `--restart-webserver` / `--restart-database`)
  - `rsync`
  - `git`
  - MariaDB or MySQL client tools (`mariadb-client` or `default-mysql-client` on Debian)
  - `systemctl`, `runuser` and `free`, which are part of a standard Debian/Ubuntu system
  - Root or sudo permissions for real runs. `--dry-run` works without root.

## Installation

1. Clone this repository:
   ```bash
   git clone https://github.com/ramhee98/MoodleUpdater.git
   cd MoodleUpdater
   ```

2. Ensure required dependencies are installed:
   ```bash
   sudo apt update
   sudo apt install python3 python3-requests python3-apt rsync git mariadb-client
   ```

3. Set up the configuration file:

   Copy `config_template.ini` to `config.ini` (or let the script auto-create it on first startup) and adjust it to your needs. The `config.ini` file contains the following settings:
   - **dry_run**: Enable dry run mode to simulate operations without making any changes (see [Dry Run](#dry-run)).
   - **auto_update_script**: Automatically check and pull updates for MoodleUpdater from the Git repository at the start. Default is False.
   - **repo**: URL of the Moodle repository to clone.
   - **branch**: Branch of the Moodle repository to checkout.
   - **path**: Path to the directory where Moodle is installed.
   - **moodle**: Name of the Moodle folder within the specified path.
   - **chown_user**: Specifies the user to set as the owner for Moodle files and directories, useful for setting file ownership after cloning or updating Moodle (e.g., www-data).
   - **chown_group**: Specifies the group to set as the owner for Moodle files and directories, ensuring correct group ownership and permissions (e.g., www-data).
   - **php_cli_user**: Optional. When set (e.g. `www-data`) and the script runs as root, Moodle's PHP CLI scripts (`upgrade.php`, `checks.php`, `maintenance.php`) run as this user through `runuser`, as Moodle recommends, so core and plugin upgrade code does not run with root privileges. Leave it empty to keep running them as root. Before you switch, make sure `moodledata` is writable by that user.
   - **folder_backup_path**: Directory where backup folders are stored. `pwd` or an empty value means the MoodleUpdater directory itself (not the current working directory).
   - **db_dump_path**: Directory where DB dumps are stored. `pwd` or an empty value means the MoodleUpdater directory itself.
   - Neither location may be inside the Moodle code folder (`path`/`moodle`), because the git clone replaces that folder. The script refuses to start if it is. If they are inside `path`, a full backup skips earlier backups, dumps and the lock file instead of copying them again.
   - **read_db_from_config**: Read the database host, name, username and password from `config.php`. Default is True. Plain single- or double-quoted strings are supported. Comments are ignored and the last assignment wins, as in PHP. If a value is computed (for example `getenv()` or variable interpolation), the run stops instead of guessing.
   - **db_name**: Name of the Moodle database, ignored if read_db_from_config is True.
   - **db_user**: Database username used for DB dump, ignored if read_db_from_config is True.
   - **db_host**: Database host, ignored if read_db_from_config is True. Leave it empty to use the local server.
   - **Note**: When `read_db_from_config` is set to False, the script uses the credentials in `config.ini` and asks for the database password while it runs (the input is not shown). In `--non-interactive` mode it reads the password from the `MYSQL_PWD` environment variable instead.
   - The database connection is checked before any work starts. If the check fails in interactive mode, you can enter the password again, up to 3 attempts in total. The password is passed to the MySQL/MariaDB tools through the `MYSQL_PWD` environment variable, never on the command line.
   - **`log_to_console`**: Enable or disable logging to the console.
   - **`log_to_file`**: Enable or disable logging to a file.
   - **`log_file_path`**: Specify the file path where logs should be saved (only if `log_to_file` is enabled).
   - **`log_level`**: Define the level of detail for logging. Available levels:
      - `DEBUG`: Detailed information for debugging.
      - `INFO`: General informational messages (default).
      - `WARNING`: Indicates potential issues.
      - `ERROR`: Errors that need immediate attention.
      - `CRITICAL`: Severe errors causing program termination.

   Boolean settings accept `True`/`False`, `yes`/`no`, `on`/`off` or `1`/`0`, in any case. Any other value is an error, so a typo can't quietly switch off `dry_run`.

   `config.ini` will be created on startup if not existing.
   Edit `config.ini` to match your Moodle setup:
   ```ini
   [settings]
   dry_run = False
   auto_update_script = False
   repo = https://github.com/BLC-FHGR/moodle
   branch = MOODLE_502_STABLE
   path = /var/www/moodle
   moodle = moodle
   chown_user = www-data
   chown_group = www-data
   php_cli_user =
   folder_backup_path = /var/www/moodle
   db_dump_path = /var/www/moodle/db_dump
   [database]
   read_db_from_config = True
   db_name = moodle
   db_user = root
   db_host =
   [logging]
   log_to_console = True
   log_to_file = True
   log_file_path = moodle_updater.log
   log_level = INFO
   ```

4. Make the script executable (optional):
   ```bash
   chmod +x moodle_updater.py
   ```

## Usage

Run the script from the terminal:
```bash
python3 moodle_updater.py
```

### Command-Line Options

The script supports various command-line arguments to control its behavior:

```bash
python3 moodle_updater.py [options]
```

**Available Options:**

- `--help`, `-h` - Show help message and exit
- `--non-interactive` - Run in non-interactive mode (default: False)
- `--directory-backup` - Start directory backup process (default: True unless non-interactive is set, then default: False)
- `--db-dump` - Start database dump process (default: True unless non-interactive is set, then default: False)
- `--git-clone` - Start git clone process (default: True unless non-interactive is set, then default: False)
- `--moodle-cli-upgrade` - Start Moodle CLI upgrade process afterwards (default: True unless non-interactive is set, then default: False)
- `--enable-maintenance-mode` - Enable Moodle Maintenance Mode during CLI upgrade (default: True unless non-interactive is set, then default: False)
- `--force-continue` - Auto-continue even if system check reports errors (default: False)
- `--restart-webserver` - Stop the webserver (Apache or Nginx) before the backup/dump/clone and keep it stopped through the CLI upgrade and its checks; start it again at the end, also after a failure (default: True unless non-interactive is set, then default: False). If the webserver can't be stopped (or isn't installed), nothing is changed and the run exits with code 1.
- `--restart-database` - Restart the MariaDB/MySQL service before the dump (default: False). Other services such as PostgreSQL or Redis are never restarted.
- `--verbose` - Enable verbose mode (default: False)
- `--full-backup` - Backup entire folder (containing moodle, moodledata, and data) (default: False) (used only if --directory-backup is set)
- `--sync-submodules-off` - Disable syncing and updating of all submodules (default: False)
- `--restore-submodules` - Restore git submodules from backup (default: False) (requires --directory-backup enabled) (automatically sets --sync-submodules-off to True)
- `--restore-plugins` - Copy third-party plugins present in the directory backup but missing from the new clone (requires --directory-backup and --git-clone enabled)
- `--restore-plugins-manual` - Prompt y/n for each plugin instead of restoring all of them (default: False) (used only with --restore-plugins; requires interactive mode)
- `--dry-run` - Run in dry run mode without making any changes (default: False). Does not require root. See [Dry Run](#dry-run).

`--restore-submodules` and `--restore-plugins` always restore from the directory backup made in the same run, never from an older backup that happens to be on disk.

Unknown or misspelled options are rejected with an error, so a typo such as `--dryrun` can never turn a simulation into a real run.

**Example Commands:**

```bash
# Run with all default prompts
python3 moodle_updater.py

# Non-interactive mode with specific operations
python3 moodle_updater.py --non-interactive --directory-backup --db-dump --git-clone --moodle-cli-upgrade

# Dry run to test without making changes
python3 moodle_updater.py --dry-run

# Full backup with verbose output
python3 moodle_updater.py --directory-backup --full-backup --verbose

# Update with CLI upgrade and maintenance mode
python3 moodle_updater.py --git-clone --moodle-cli-upgrade --enable-maintenance-mode

# Restore submodules from backup instead of syncing from remote
python3 moodle_updater.py --directory-backup --git-clone --restore-submodules

# Carry third-party plugins from the old install across the upgrade
python3 moodle_updater.py --directory-backup --git-clone --restore-plugins --moodle-cli-upgrade

# Same, but choose interactively which plugins to restore
python3 moodle_updater.py --directory-backup --git-clone --restore-plugins --restore-plugins-manual --moodle-cli-upgrade
```

### Example Workflow

1. **Directory Backup**:
   - Choose to back up the entire Moodle directory or specific components.
   - Backups are stored with timestamps in a folder defined in `config.ini` for easy identification.

2. **Database Backup**:
   - Dump the Moodle database to a `.sql` file in the directory specified in `config.ini` (the directory is created if needed). If the dump fails, the incomplete file is deleted so it can't be mistaken for a valid backup.
   - Database credentials are read from the `config.php` file if `read_db_from_config` is enabled in `config.ini`. Otherwise the credentials in `config.ini` are used and the password is requested from the user (or read from `MYSQL_PWD` in non-interactive mode).
   - Before dumping, the script asks if the database should be restarted.
   - If restarted, a **2-second pause** is added to ensure the database is fully initialized before proceeding.

3. **Git Operations**:
   - Clone the Moodle repository and check out the configured branch.
   - Copy `config.php` from the old installation into the new clone (or from another path you enter, or skip it). The copy keeps the original's permissions, minus any group/world write bit (default `0640` when unknown). A `config.php` that the cloned repository itself contains (even a symlink) is replaced, never written through.
   - Sync submodules, or restore them from the directory backup with `--restore-submodules`.

4. **Multithreading**:
   - The database dump runs in parallel with the directory backup and git clone. The backup and the clone run one after the other, because the clone replaces the code the backup protects. The new code is only *prepared* in parallel: it is put in place after the dump (and every other step) has succeeded.

5. **Moodle CLI Upgrade**:
   - After updating Moodle, you can run the Moodle upgrade script (`admin/cli/upgrade.php`) automatically.
   - The script asks for confirmation before proceeding.
   - You will be asked whether to enable Maintenance Mode before upgrading.
   - If enabled, Maintenance Mode will be automatically disabled once the upgrade is complete.
   - Any errors encountered during the CLI upgrade are logged.
   - If the web server was stopped for the update, it stays stopped during the upgrade and its checks and is started again at the end.
   - Maintenance mode is required to work when requested: if it can't be enabled, the upgrade is not started; if it can't be disabled afterwards, the run exits with code 1 and logs the command to disable it by hand.
   - The upgrade is skipped if any earlier step failed (see [Failure Handling and Exit Codes](#failure-handling-and-exit-codes)).

### Key Prompts

- **Path**: Confirm the default Moodle installation path or provide a custom path.
- **Database Credentials**: Enter the Moodle database name, user, and password when prompted.
- **Git Branch**: Confirm the branch from `config.ini` (the template uses `MOODLE_502_STABLE`) or enter another one.

### Logs and Outputs

- Backups and dumps are timestamped for easy tracking.
- Backup folders are created with mode `0700` and dumps with `0600`, because they contain `config.php` (with the DB password) and all user data. Only root can read them. The backup is copied inside a private `<moodle>_bak_inprogress_<timestamp>` folder and moved into place when complete. Dumps are written to a hidden `.<db>_<timestamp>.partial.sql` file and renamed when complete. A failed backup or dump is deleted, and an existing file is never overwritten.
- All errors are reported to the terminal and the log file, including failed database connections or file operations.

### Non-Interactive Mode

For automated deployments and scripting, MoodleUpdater supports a non-interactive mode:

- Use `--non-interactive` to run without user prompts
- The "Pull MoodleUpdater from GitHub?" prompt is skipped; self-update only runs when `auto_update_script = True`
- The script never waits for input. If the Moodle system check before the upgrade fails, the upgrade is skipped unless `--force-continue` is set. A failed check after the upgrade makes the run exit with code 1
- Git and SSH never prompt for credentials (`GIT_TERMINAL_PROMPT=0`, SSH batch mode, stdin closed). Credentials that are already set up (credential helpers, SSH keys or agents, deploy tokens) keep working. An explicitly set `GIT_SSH_COMMAND` is respected
- With `read_db_from_config = False`, the DB password is read from the `MYSQL_PWD` environment variable
- In non-interactive mode, all operations default to `False` unless explicitly enabled with command-line flags
- Combine with specific operation flags to control which tasks to execute
- Perfect for CI/CD pipelines, cron jobs, and automated update scripts

Example for automated updates:
```bash
# Automated nightly backup and update
python3 moodle_updater.py --non-interactive --directory-backup --db-dump --git-clone --moodle-cli-upgrade --restart-webserver
```

### Failure Handling and Exit Codes

Each step reports whether it succeeded, and steps that depend on a failed step are skipped:

- If the directory backup fails, the git clone does not run, so the existing Moodle code is left untouched.
- The git clone is built in a private, hidden staging directory next to the Moodle folder (`.<moodle>_new_<timestamp>`). It contains clone, checkout, submodules, `config.php`, restored plugins and ownership. The existing code is replaced only after all of these **and the directory backup and database dump** have succeeded, so a failed step (for example a network error or a failed dump) leaves the site unchanged. During the clone you need enough free disk space for a second copy of the code. The Moodle code folder must not be a mount point, because the swap renames it.
- The checked-out tree's `version.php` is compared with the installed one before the swap, and an older clone is refused. If either version can't be read, a warning is logged and the update proceeds.
- If moving the new code into place fails, the previous code is moved back. If even that fails, the log names the exact `mv` command that restores the previous code, which is kept intact.
- If the directory backup, database dump, git clone or plugin restore fails, the Moodle CLI upgrade is skipped, because the upgrade changes the database schema and cannot be undone.
- Stopping or starting the webserver, restarting the database, and enabling or disabling maintenance mode all count as steps: a failure makes the run exit with code 1.
- Unexpected errors in background threads are written to the log with a traceback and count as failures.
- With `--restart-webserver`, the webserver is always started again, also after an error or Ctrl-C (even one during the stop). Maintenance mode is always disabled again after the CLI upgrade, even if the upgrade crashes.
- In interactive mode, if a step failed, you are asked whether to start the webserver anyway. Answer `n` to keep the site offline while you investigate, then start it yourself with `systemctl start apache2` (or `nginx`). Any other answer, no answer within 5 minutes, Cancel or Ctrl-C starts it. Non-interactive runs always start it without asking.
- If the pre-check or the CLI upgrade fails after the new code is already in place, maintenance mode is disabled and the webserver is started again. The site then runs the new code against the old or partially upgraded database until you fix the problem and re-run with `--moodle-cli-upgrade`, or restore the backup and dump.
- Only one run per Moodle instance at a time: a lock keyed by the canonical path of the Moodle code folder is taken in `/run/moodle_updater` (root-only; override with the `MOODLE_UPDATER_LOCK_DIR` environment variable).
- A `FAILED STEPS` summary is logged at the end of the run.

Exit codes:

- `0`: every selected step succeeded, including the service and maintenance-mode actions and the system check after the upgrade
- `1`: at least one step failed or was skipped because of an earlier failure, or the run was aborted
- `2`: invalid command-line options

### Dry Run

`--dry-run` (or `dry_run = True`) logs what would happen without changing anything:

- no self-update (`git pull`), even when `auto_update_script = True`
- no backup, dump, clone, lock file, `config.ini` creation, service, maintenance-mode or PHP command
- no database connection: neither the connection check nor the dump-size query runs

The only things a dry run writes are its log output. If `log_to_file` is enabled, that includes the log file. It does read the live installation and, for the remote version check, fetches `version.php` from the configured repository over HTTP.

## Multithreading Efficiency

The script runs the database dump in its own thread alongside the directory backup and git clone, which can significantly reduce the total runtime. The log reports how much time this saved.

## Development

The tests use pytest and need `git` and `rsync`; they never touch a real Moodle install, database or service.

```bash
pip install pytest ruff requests
ruff check .
pytest tests
```

GitHub Actions runs both on every push and pull request (`.github/workflows/ci.yml`).

## Contribution

Contributions are welcome! Please fork the repository, create a new branch for your changes, and submit a pull request.

## License

This project is licensed under the [MIT License](LICENSE).

## Disclaimer

Use this tool at your own risk. Ensure you have proper backups and permissions before running the script in a production environment.

## Author

Developed by [ramhee98](https://github.com/ramhee98). For questions or suggestions, feel free to open an issue in the repository.
