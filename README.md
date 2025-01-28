# MoodleUpdater

**MoodleUpdater** is a script designed to automate the process of updating a Moodle instance. It supports directory and database backups, cloning the Moodle repository, and restoring configuration files. With multithreading, the script can efficiently handle multiple tasks simultaneously.

## Features

- **Configuration Management**:
  - Centralized configuration using a `config.ini` file.
  - Easily specify repository URL, branch, Moodle path, and folder name.

- **Backup Functionality**:
  - Perform a full or partial backup of your Moodle directory.
  - Backup the Moodle database using `mysqldump`.

- **Git Integration**:
  - Clone Moodle's repository from GitHub.
  - Checkout specific branches and sync submodules.

- **Automation**:
  - Multithreaded execution for combined tasks.
  - Prompts to confirm actions with optional default responses.
  - Automatically restart Apache or Nginx after updates.

- **User-Friendly**:
  - Guided prompts for paths, database credentials, and other configurations.
  - Default values and skip options for most steps.

- **Update MoodleUpdater with Git Pull**:
  - Optionally pull the latest version of the script from the Git repository on start.
  - Configurable through the `auto_update_script` setting in `config.ini`. When enabled, the script automatically checks for updates and pulls them if no local changes are detected.
  - If `auto_update_script` is disabled, user is asked if he wants to pull MoodleUpdater from GitHub.
  - Compares `config.ini` with `config_template.ini` and highlights any differences to ensure proper configuration.
  - Retrieve and display detailed information about the current commit (time, author, summary).
  - Automatically checks and reports the branch name, current commit details, and updated commit details after pulling changes.

- **Enhanced Logging**: 
  - Configurable logging with options for console and file output.
  - Supports log rotation and adjustable logging levels for better debugging and monitoring.

## Requirements

- **Operating System**: Linux-based (e.g., Ubuntu)
- **Dependencies**:
  - `python3`
  - `rsync`
  - `mysqldump` (MySQL or MariaDB tools)
  - `git`
  - Root or sudo permissions for system and database operations.

## Installation

1. Clone this repository:
   ```bash
   git clone https://github.com/ramhee98/MoodleUpdater.git
   cd MoodleUpdater
   ```

2. Ensure required dependencies are installed:
   ```bash
   sudo apt update
   sudo apt install python3 rsync mysql-client git
   ```

3. Set up the configuration file:

   The `config.ini` file contains the following settings:
   - **dry_run**: Enable dry run mode to simulate operations without making any changes.
   - **auto_update_script**: Automatically check and pull updates for MoodleUpdater from the Git repository at the start. Default is True.
   - **repo**: URL of the Moodle repository to clone.
   - **branch**: Branch of the Moodle repository to checkout.
   - **path**: Path to the directory where Moodle is installed.
   - **moodle**: Name of the Moodle folder within the specified path.
   - **folder_backup_path**: Custom directory where backup files will be stored. If left blank, the current working directory will be used.
   - **db_dump_path**: Custom directory where DB dumps will be stored. If left blank, the current working directory will be used.
   - **read_db_from_config** Read database name, username and password from `config.php`, default is True
   - **db_name**: Name of the Moodle database, ignored if read_db_from_config is True.
   - **db_user**: Database username used for DB dump, ignored if read_db_from_config is True.
   - **`log_to_console`**: Enable or disable logging to the console.
   - **`log_to_file`**: Enable or disable logging to a file.
   - **`log_file_path`**: Specify the file path where logs should be saved (only if `log_to_file` is enabled).
   - **`log_level`**: Define the level of detail for logging. Available levels:  
      - `DEBUG`: Detailed information for debugging.  
      - `INFO`: General informational messages (default).  
      - `WARNING`: Indicates potential issues.  
      - `ERROR`: Errors that need immediate attention.  
      - `CRITICAL`: Severe errors causing program termination.  

   `config.ini` will be created on startup if not existing.
   Edit `config.ini` to match your Moodle setup:
   ```ini
   [settings]
   dry_run = False
   auto_update_script = True
   repo = https://github.com/BLC-FHGR/moodle
   branch = MOODLE_404_STABLE
   path = /var/www/moodle
   moodle = moodle
   folder_backup_path = /var/www/moodle
   db_dump_path = /var/www/moodle/db_dump
   [database]
   read_db_from_config = True
   db_name = moodle
   db_user = root
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

### Example Workflow

1. **Directory Backup**:
   - Choose to back up the entire Moodle directory or specific components.
   - Backups are stored with timestamps in a folder defined in `config.ini` for easy identification.

2. **Database Backup**:
   - Dump the Moodle database to a `.sql` file in the directory specified in `config.ini`.
   - Database credentials are read from the `config.php` file if `read_db_from_config` is enabled in `config.ini`. Otherwise, credentials specified in `config.ini` are used, password is requested by the user.

3. **Git Operations**:
   - Clone the Moodle repository.
   - Optionally restore `config.php` from a previous backup.
   - Ensures the local repository is up-to-date before operations if there are no local changes.

4. **Multithreading**:
   - Directory backups, database dumps, and Git operations can run concurrently, saving time.

5. **Post-Process**:
   - Optionally restart Apache or Nginx to apply changes.

### Key Prompts

- **Path**: Confirm the default Moodle installation path or provide a custom path.
- **Database Credentials**: Enter the Moodle database name, user, and password when prompted.
- **Git Repository**: By default, clones the Moodle repository from GitHub with the branch `MOODLE_404_STABLE`.

### Logs and Outputs

- Backups and dumps are timestamped for easy tracking.
- All errors are reported to the terminal, including failed database connections or file operations.

## Multithreading Efficiency

The script optimizes runtime by leveraging Python's threading module. Tasks like backups and Git operations run in parallel, significantly reducing execution time.

## Contribution

Contributions are welcome! Please fork the repository, create a new branch for your changes, and submit a pull request.

## License

This project is licensed under the [MIT License](LICENSE).

## Disclaimer

Use this tool at your own risk. Ensure you have proper backups and permissions before running the script in a production environment.

## Author

Developed by [ramhee98](https://github.com/ramhee98). For questions or suggestions, feel free to open an issue in the repository.

### Highlights:
- The **Features** and **Usage** sections match the script's functionality.
- Multithreading is emphasized as a key benefit.
- Prompts and outputs are explained clearly.
- The script uses its own directory as the reference point for operations, ensuring consistency regardless of where it is executed.
