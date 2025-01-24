import os
import subprocess
import shutil
import time
import threading
import apt
import configparser
import re
import sys

runtime_backup = None
runtime_dump = None
runtime_clone = None
dry_run = False
SEPARATOR = "-------------------------------------------------------------------------"

# Function to load configfile
def load_config(config_path):
    """Load configuration from a file."""
    config = configparser.ConfigParser(interpolation=None)
    config.read(config_path)
    return config

# Function to prompt the user for confirmation, with default responses handled
def confirm(question, default=''):
    """Prompt the user for confirmation with optional default response and cancel functionality."""
    valid_responses = {'y': True, 'n': False, 'c': None}
    option = "Yes(y)/No(n)/Cancel(c)" + (f" Default={default}" if default else "")

    while True:
        user_input = input(f"{question} {option}: ").strip().lower()

        if user_input in valid_responses:
            if user_input == 'c':  # Cancel case
                print("Script aborted")
                exit(1)
            return valid_responses[user_input]
        elif default and user_input == '':
            return valid_responses.get(default.lower(), False)

# Function to handle directory backups using rsync
def f_dir_backup(path, moodle, full_backup, folder_backup_path):
    global runtime_backup
    start = time.time()

    if full_backup:
        path = os.path.join(path, '')
        print(f"Performing a full backup of {path} but skipping unnecessary folders.")
        backup_folder = os.path.join(f"{folder_backup_path}{moodle}_bak_full_{time.strftime('%Y-%m-%d-%H-%M-%S')}")
        if dry_run:
            print(f"[Dry Run] Would run: rsync -r --exclude=<excluded folders> {path} {backup_folder}")
        else:
            subprocess.run([
                'rsync', '-r',
                '--exclude', 'moodledata/cache',
                '--exclude', 'moodledata/localcache',
                '--exclude', 'moodledata/sessions',
                '--exclude', 'moodledata/temp',
                '--exclude', 'moodledata/trashdir',
                path, backup_folder
            ])
    else:
        path = os.path.join(path, moodle, '')
        print(f"Only backing up {path}")
        backup_folder = os.path.join(f"{folder_backup_path}{moodle}_bak_{time.strftime('%Y-%m-%d-%H-%M-%S')}")
        if dry_run:
            print(f"[Dry Run] Would run: rsync -r {path} {backup_folder}")
        else:
            subprocess.run(['rsync', '-r', path, backup_folder])

    print(f"Backup of {path} was successfully saved in {backup_folder}")
    runtime_backup = int(time.time() - start)

# Function to perform database dump
def f_db_dump(dbname, dbuser, dbpass, verbose, db_dump_path):
    global runtime_dump
    start = time.time()

    if dry_run:
        print(f"[Dry Run] Would restart MySQL for better performance.")
    else:
        print("Restarting mysql for better performance...")
        subprocess.run(['sudo', 'service', 'mysql', 'restart', '--innodb-doublewrite=0'])
        print("mysql restart complete")

    dump_path = os.path.join(db_dump_path, f"{dbname}_{time.strftime('%Y-%m-%d-%H-%M-%S')}.sql")

    if dry_run:
        print(f"[Dry Run] Would perform database dump to {dump_path} with user {dbuser}.")
    else:
        with open(dump_path, "w") as f:
            print("DB backing up...")
            if verbose:
                result = subprocess.run([
                    'mysqldump', '-u', dbuser, f'-p{dbpass}', '--single-transaction',
                    '--skip-lock-tables', '--databases', dbname, '--verbose'
                ], stdout=f)
            else:
                result = subprocess.run([
                    'mysqldump', '-u', dbuser, f'-p{dbpass}', '--single-transaction',
                    '--skip-lock-tables', '--databases', dbname
                ], stdout=f)

            if result.returncode != 0:
                print("Error occurred:", result.stderr.decode())
            else:
                print(f"Dump was successfully saved in {dump_path}")

        os.chown(dump_path, os.getuid(), os.getgid())

    runtime_dump = int(time.time() - start)

# Function to clone a git repository
def f_git_clone(path, moodle, config_php, repository, branch, sync_submodules):
    global runtime_clone
    start = time.time()

    clone_path = os.path.join(path, moodle)

    if os.path.exists(clone_path):
        if dry_run:
            print(f"[Dry Run] Would remove directory: {clone_path}")
        else:
            shutil.rmtree(clone_path)

    if dry_run:
        print(f"[Dry Run] Would clone repository: {repository} to {path}")
        print(f"[Dry Run] Would checkout branch: {branch} to {clone_path}")
    else:
        subprocess.run(['sudo', 'git', 'clone', repository], cwd=path)
        subprocess.run(['sudo', 'git', 'checkout', branch], cwd=clone_path)

    if sync_submodules:
        if dry_run:
            print(f"[Dry Run] Would sync and update git submodules in {clone_path}")
        else:
            subprocess.run(['sudo', 'git', 'submodule', 'sync'], cwd=clone_path)
            subprocess.run(['sudo', 'git', 'submodule', 'update', '--init', '--recursive', '--remote'], cwd=clone_path)

    if dry_run:
        print(f"[Dry Run] Would create config.php in {clone_path}")
    else:
        with open(os.path.join(clone_path, 'config.php'), 'w') as config_file:
            config_file.write(config_php)

    if dry_run:
        print(f"[Dry Run] Would set ownership of {clone_path} to www-data:www-data.")
    else:
        subprocess.run(['sudo', 'chown', 'www-data:www-data', clone_path, '-R'])
        print("Finished git clone process")

    runtime_clone = int(time.time() - start)

# Function to perform directory backup and git clone
def f_dir_backup_git_clone(path, moodle, config_php, full_backup, folder_folder_backup_path, configphp, repo, branch, sync_submodules):
    print("----------- Backing up Moodle directory ------------------------------------------------------------")
    f_dir_backup(path, moodle, full_backup, folder_folder_backup_path)

    print("----------- Starting git clone process -------------------------------------------------------------")
    f_git_clone(path, moodle, configphp, repo, branch, sync_submodules)

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
        print(f"Error: File '{config_path}' not found.")
    except Exception as e:
        print(f"An error occurred: {e}")

    return cfg_values

# Function to get commit details
def get_commit_details(commit_hash, pwd):
    """Retrieve commit details (time, author, summary) for a given commit hash."""
    result = subprocess.run(
        ['git', '-C', pwd, 'show', '-s', '--format=%ci|%an|%s', commit_hash],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return result.stdout.strip().split('|')
    return "Unknown", "Unknown", "Unknown"

# Function to start / stop the webserver
def restart_webserver(action, cache):
    if cache['apache2'].is_installed:
        if dry_run:
            print(f"[Dry Run] Would run: sudo systemctl {action} apache2")
        else:
            subprocess.run(['sudo', 'systemctl', action, 'apache2'])
    elif cache['nginx'].is_installed:
        if dry_run:
            print(f"[Dry Run] Would run: sudo systemctl {action} nginx")
        else:
            subprocess.run(['sudo', 'systemctl', action, 'nginx'])
    else:
        print("failed to " + action + " webserver")

# Function to self update from GitHub   
def self_update(pwd, CONFIG_PATH, CONFIG_TEMPLATE_PATH):
    """Check if running inside a Git repo, ensure no local changes, and pull the latest changes."""
    print(SEPARATOR)
    try:
        # Check if .git exists in the script's directory
        git_dir = os.path.join(pwd, '.git')
        if not os.path.exists(git_dir):
            print("Not a Git repository. Skipping self-update.")
            return

        # Get current branch
        branch_result = subprocess.run(
            ['git', '-C', pwd, 'rev-parse', '--abbrev-ref', 'HEAD'],
            capture_output=True, text=True
        )
        current_branch = branch_result.stdout.strip()
        print(f"Branch: {current_branch}")

        # Get current commit details
        current_commit_result = subprocess.run(
            ['git', '-C', pwd, 'rev-parse', 'HEAD'], capture_output=True, text=True
        )
        current_commit = current_commit_result.stdout.strip() if current_commit_result.returncode == 0 else "Unknown"
        current_commit_time, current_commit_author, current_commit_summary = get_commit_details(current_commit, pwd)

        print(f"Commit: {current_commit}")
        print(f"Time: {current_commit_time}")
        print(f"Author: {current_commit_author}")
        print(f"Summary: {current_commit_summary}")

        # Check for uncommitted changes
        status_result = subprocess.run(
            ['git', '-C', pwd, 'status', '--porcelain'], capture_output=True, text=True
        )
        if status_result.stdout.strip():
            print("Local changes detected. Skipping self-update to avoid conflicts.")
            check_config_differences(CONFIG_PATH, CONFIG_TEMPLATE_PATH)
            return

        # Pull the latest changes
        print("Checking for updates...")
        pull_result = subprocess.run(
            ['git', '-C', pwd, 'pull', '--rebase'], capture_output=True, text=True
        )

        if "Already up to date." in pull_result.stdout:
            print("The script is already up to date.")
            check_config_differences(CONFIG_PATH, CONFIG_TEMPLATE_PATH)
        else:
            # Get updated commit details
            updated_commit_result = subprocess.run(
                ['git', '-C', pwd, 'rev-parse', 'HEAD'], capture_output=True, text=True
            )
            updated_commit = updated_commit_result.stdout.strip() if updated_commit_result.returncode == 0 else "Unknown"
            updated_commit_time, updated_commit_author, updated_commit_summary = get_commit_details(updated_commit, pwd)

            print(f"Updated from commit {current_commit} to commit {updated_commit} on branch {current_branch}.")
            print(f"Old commit details:")
            print(f"  Time: {current_commit_time}")
            print(f"  Author: {current_commit_author}")
            print(f"  Summary: {current_commit_summary}")
            print(f"New commit details:")
            print(f"  Time: {updated_commit_time}")
            print(f"  Author: {updated_commit_author}")
            print(f"  Summary: {updated_commit_summary}")

            check_config_differences(CONFIG_PATH, CONFIG_TEMPLATE_PATH)
            # Restart the script with the updated version
            print("Restarting the script...")
            print(SEPARATOR)
            os.execv(sys.executable, [sys.executable] + sys.argv)

    except Exception as e:
        print(f"Error during self-update: {e}")
        print("Continuing with the current version.")

def check_config_differences(config_path, template_path):
    """
    Check for differences between config.ini and config_template.ini.
    If differences are found, print them to the console.
    """
    print(SEPARATOR)
    try:
        # Read config.ini
        if not os.path.exists(config_path):
            print(f"{config_path} not found. Please ensure the file exists.")
            return

        # Read config_template.ini
        if not os.path.exists(template_path):
            print(f"{template_path} not found. Please ensure the file exists.")
            return

        # Parse both files
        config = configparser.ConfigParser(interpolation=None)
        config.read(config_path)

        template = configparser.ConfigParser(interpolation=None)
        template.read(template_path)

        # Compare sections
        all_sections = set(config.sections()).union(set(template.sections()))
        differences_found = False

        for section in all_sections:
            config_items = set(config.items(section)) if config.has_section(section) else set()
            template_items = set(template.items(section)) if template.has_section(section) else set()

            added = template_items - config_items
            removed = config_items - template_items

            if added or removed:
                differences_found = True
                print(f"\n[Differences in section: {section}]")
                if added:
                    print("  Missing in config.ini:")
                    for key, value in added:
                        print(f"    {key} = {value}")
                if removed:
                    print("  Extra in config.ini:")
                    for key, value in removed:
                        print(f"    {key} = {value}")

        if not differences_found:
            print("config.ini matches config_template.ini. No differences found.")

    except Exception as e:
        print(f"Error while checking configuration differences: {e}")

# Main function
def main():
    global runtime_backup, runtime_dump, runtime_clone, dry_run

    # Load configuration
    pwd = os.path.dirname(os.path.abspath(__file__))
    CONFIG_PATH = os.path.join(pwd, 'config.ini')
    CONFIG_TEMPLATE_PATH = os.path.join(pwd, 'config_template.ini')
    config = load_config(CONFIG_PATH)
    print("Loaded config")

    auto_update = config.get('settings', 'auto_update_script', fallback=False)
    if auto_update == "True":
        self_update(pwd, CONFIG_PATH, CONFIG_TEMPLATE_PATH)
    else:
        print(SEPARATOR)
        if confirm("Pull MoodleUpdater from GitHub?", "n"):
            self_update(pwd, CONFIG_PATH, CONFIG_TEMPLATE_PATH)

    if not os.path.exists(CONFIG_PATH):
        print(f"Configuration file '{CONFIG_PATH}' not found.")
        if os.path.exists(CONFIG_TEMPLATE_PATH):
            shutil.copy(CONFIG_TEMPLATE_PATH, CONFIG_PATH)
            print("Configuration file has been created.")
            print("Please edit config.ini to your needs.")
        else:
            print("Please create config.ini")
        print("Update aborted, exiting!")
        exit(1)

    config = load_config(CONFIG_PATH)
    dry_run = config.get('settings', 'dry_run', fallback=False)
    if dry_run != "True":
        dry_run = False
    moodle = config.get('settings', 'moodle', fallback='moodle')
    path = config.get('settings', 'path')
    configphppath = os.path.join(path, moodle, 'config.php')
    multithreading = False

    print(SEPARATOR)
    # User confirmation and configurations
    if dry_run:
        print("[Dry Run] is enabled!")
        print(SEPARATOR)

    dir_backup = confirm("Start directory backup process?", "y")
    db_dump = confirm("Start DB dump process?", "y")
    git_clone = confirm("Start git clone process?", "y")

    print(SEPARATOR)
    print(f"dirbackup: {dir_backup}")
    print(f"dbdump: {db_dump}")
    print(f"gitclone: {git_clone}")
    print(SEPARATOR)

    if not dir_backup and not db_dump and not git_clone:
        print("Script aborted")
        exit(1)

    restart_webserver_flag = confirm("Restart webserver automatically?", "y")
    verbose = confirm("Do you want to enable verbose mode?", default='n')

    if dir_backup or git_clone:
        print("----------- Prepare Moodle Path --------------------------------------------------------------------")
        if not confirm(f"Is this the correct Moodle directory? {path}", "y"):
            path = input("Please enter a path: ").rstrip("/")

    # Directory backup
    if dir_backup:
        folder_backup_path = config.get('settings', 'folder_backup_path', fallback='pwd')
        if folder_backup_path in ["pwd", ""]:
            folder_backup_path = pwd
        if not folder_backup_path.endswith("/"):
            folder_backup_path = os.path.join(folder_backup_path, '')
        full_backup = confirm("Backup entire folder (containing moodle, moodledata, and data)?", "n")

    # Database dump
    if db_dump:
        db_dump_path = config.get('settings', 'db_dump_path', fallback='pwd')
        if db_dump_path in ["pwd", ""]:
            db_dump_path = pwd
        read_db_from_config = config.get('database', 'read_db_from_config', fallback=True)
        dbpass = ""
        if(read_db_from_config == "False"):
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
                print(f"[Dry Run] Would run: mysqlshow to check if DB:{dbname} is accessible with user:{dbuser} password:{dbpass}")
                result = "returncode=0"
        else:
            result = str(subprocess.run(['mysqlshow', '-u', dbuser, f'-p{dbpass}', dbname], stdout=subprocess.PIPE))

        if "returncode=1" in result:
            print("connection to DB failed")
            while not dbpass.strip():
                dbpass = input("Please enter DB password again: ")
                if dbpass.strip():
                    break  # Exit loop if dbpass is not empty

            result = str(subprocess.run(['mysqlshow', '-u', dbuser, f'-p{dbpass}', dbname], stdout=subprocess.PIPE))

            if "returncode=1" in result:
                print("connection to DB failed")
                print("Script aborted")
                exit(1)
        else:
            print("connection to DB established")

    if git_clone:
        print("----------- Prepare git clone process --------------------------------------------------------------")
        repo = config.get('settings', 'repo')
        branch = config.get('settings', 'branch')
        configphppath = os.path.join(path, moodle, 'config.php')

        if not confirm(f"Do you want to copy {configphppath} from the old directory?", "y"):
            customconfigphppath = input("Please enter a config.php path [press enter to skip]: ")
            if customconfigphppath:
                with open(customconfigphppath, 'r') as file:
                    configphp = file.read()
            else:
                print("Restore of old config.php skipped!")
        else:
            with open(configphppath, 'r') as file:
                configphp = file.read()

        if not confirm(f"Do you want to git checkout {branch}?", "y"):
            branch = input("Please enter custom branch: ")

        sync_submodules = confirm("Do you want to sync and update all submodules?", "y")

    if not confirm("Do you want to confirm the installation?"):
        print("Script aborted")
        exit(1)

    # Record the start time
    start0 = time.time()
    print("started at", time.strftime("%Y-%m-%d %H:%M:%S"))

    if restart_webserver_flag:
        cache = apt.Cache()
        restart_webserver("stop", cache)

    # Handle multithreading based on user choices
    if dir_backup and db_dump and git_clone:
        multithreading = True

        t_backup_clone = threading.Thread(
            target=f_dir_backup_git_clone,
            args=(path, moodle, configphp, full_backup, folder_backup_path, configphp, repo, branch, sync_submodules,)
        )
        t_dump = threading.Thread(target=f_db_dump, args=(dbname, dbuser, dbpass, verbose, db_dump_path,))

        print("----------- Backing up Moodle directory & starting git clone process afterwards | multithreading ---")
        t_backup_clone.start()
        print("----------- Starting database dump | multithreading ------------------------------------------------")
        t_dump.start()

        t_backup_clone.join()
        t_dump.join()
    elif dir_backup and db_dump:
        multithreading = True

        t_backup = threading.Thread(target=f_dir_backup, args=(path, moodle, full_backup, folder_backup_path,))
        t_dump = threading.Thread(target=f_db_dump, args=(dbname, dbuser, dbpass, verbose, db_dump_path,))

        print("----------- Backing up Moodle directory | multithreading -------------------------------------------")
        t_backup.start()
        print("----------- Starting database dump | multithreading ------------------------------------------------")
        t_dump.start()

        t_backup.join()
        t_dump.join()
    elif db_dump and git_clone:
        multithreading = True

        t_dump = threading.Thread(target=f_db_dump, args=(dbname, dbuser, dbpass, verbose, db_dump_path,))
        t_clone = threading.Thread(target=f_git_clone, args=(path, moodle, configphp, repo, branch, sync_submodules,))

        print("----------- Starting database dump | multithreading ------------------------------------------------")
        t_dump.start()
        print("----------- Starting git clone process | multithreading --------------------------------------------")
        t_clone.start()

        t_dump.join()
        t_clone.join()
    else:
        if dir_backup:
            print("----------- Backing up Moodle directory ------------------------------------------------------------")
            f_dir_backup(path, moodle, full_backup, folder_backup_path)

        if db_dump:
            print("----------- Starting database dump -----------------------------------------------------------------")
            f_db_dump(dbname, dbuser, dbpass, verbose, db_dump_path)

        if git_clone:
            print("----------- Starting git clone process -------------------------------------------------------------")
            f_git_clone(path, moodle, configphp, repo, branch, sync_submodules)

    if restart_webserver_flag:
        restart_webserver("start", cache)

    # Time calculation
    end0 = time.time()
    runtime = int(end0 - start0)  # Convert to integer seconds

    # Check if any operation times were recorded and print them
    if runtime_backup:
        print("directory backup time needed:", runtime_backup, "seconds")
    else:
        runtime_backup = 0

    if runtime_dump:
        print("database dump time needed:", runtime_dump, "seconds")
    else:
        runtime_dump = 0

    if runtime_clone:
        print("git clone time needed:", runtime_clone, "seconds")
    else:
        runtime_clone = 0

    # Print total runtime
    print("total time needed:", runtime, "seconds")
    if multithreading:
        print("time saved with multithreading:", runtime_backup + runtime_dump + runtime_clone - runtime, "seconds")

    print("------------------------------------------------------------------------------------------")
    print("finished at", time.strftime("%Y-%m-%d %H:%M:%S"))

    if dry_run:
        print(SEPARATOR)
        print(f"[Dry Run] was enabled!")
        print(SEPARATOR)

main()