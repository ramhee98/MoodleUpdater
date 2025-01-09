# MoodleUpdater

**MoodleUpdater** is a script designed to automate the process of updating a Moodle instance. It supports directory and database backups, cloning the Moodle repository, and restoring configuration files. With multithreading, the script can efficiently handle multiple tasks simultaneously.

## Features

- **Backup Functionality**:
  - Perform a full or partial backup of your Moodle directory.
  - Backup the Moodle database with `mysqldump`.

- **Git Integration**:
  - Clone Moodle's repository from GitHub.
  - Checkout specific branches and sync submodules.

- **Automation**:
  - Multithreaded execution for combined tasks.
  - Prompts to confirm actions with optional default responses.
  - Automatically restart Apache after updates (if chosen).

- **User-Friendly**:
  - Guided prompts for paths, database credentials, and other configurations.
  - Default values and skip options for most steps.

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

3. Make the script executable (optional):
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
   - Backups are stored with timestamps for easy identification.

2. **Database Backup**:
   - Dump the Moodle database to a `.sql` file in the current directory.
   - Database credentials are requested during runtime.

3. **Git Operations**:
   - Clone the Moodle repository, checkout a specific branch, and sync submodules.
   - Optionally restore `config.php` from a previous backup.

4. **Multithreading**:
   - Directory backups, database dumps, and Git operations can run concurrently, saving time.

5. **Post-Process**:
   - Optionally restart Apache to apply changes.

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
