import os
import subprocess
import shutil
import time
import threading
import apt
import configparser

runtime_backup = None
runtime_dump = None
runtime_clone = None

# Function to load configfile
def load_config(config_path):
    """Load configuration from a file."""
    config = configparser.ConfigParser(interpolation=None)  # Disable interpolation
    config.read(config_path)
    return config

# Function to prompt the user for confirmation, with default responses handled
def confirm(question, default=''):
    valid_responses = {'y': True, 'n': False}
    default_response = valid_responses.get(default.lower(), False)
    option = "Yes(y)/No(n)/Cancel(c)"

    if default != '':
        option += " Default=" + default

    while True:
        # user_input = input(f"{question}: ").lower()
        user_input = input(f"{question} {option}: ").lower()

        if user_input in ['y', 'n']:
            return valid_responses[user_input]
        elif user_input == 'c':
            print("Script aborted")
            exit(1)
        else:
            if default.lower() in valid_responses:
                return default_response

# Function to handle directory backups using rsync
def f_dir_backup(path, moodle, full_backup):
    global runtime_backup
    start = time.time()

    if full_backup:
        print(f"Doing a full backup of {path} but skipping unnecessary folders")

        backup_folder = os.path.join(f"{path}_bak_{time.strftime('%Y-%m-%d-%H-%M-%S')}")
        path = os.path.join(path, '')

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
        path = os.path.join(path, moodle)
        print(f"Only backing up {path}")

        backup_folder = os.path.join(f"{path}_bak_{time.strftime('%Y-%m-%d-%H-%M-%S')}")
        path = os.path.join(path, '')

        subprocess.run(['rsync', '-r', path, backup_folder])

    print(f"Backup of {path} was successfully saved in {backup_folder}")

    end = time.time()
    runtime_backup = int(end - start)  # Convert to integer seconds

# Function to perform database dump
def f_db_dump(dbname, dbuser, dbpass, verbose, pwd):
    global runtime_dump
    start = time.time()

    print("Restarting mysql for better performance...")
    subprocess.run(['sudo', 'service', 'mysql', 'restart', '--innodb-doublewrite=0'])
    print("mysql restart complete")

    dump_path = os.path.join(pwd, f"{dbname}_{time.strftime('%Y-%m-%d-%H-%M-%S')}.sql")

    f = open(dump_path, "w")
    print("DB backing up...")

    if verbose:
        result = subprocess.run([
            'mysqldump', '-u', dbuser, f'-p{dbpass}', '--single-transaction',
            '--skip-lock-tables', f'--databases', dbname, '--verbose'
        ], stdout=f)
    else:
        result = subprocess.run([
            'mysqldump', '-u', dbuser, f'-p{dbpass}', '--single-transaction',
            '--skip-lock-tables', f'--databases', dbname
        ], stdout=f)

    if result.returncode != 0:
        print("Error occurred:", result.stderr.decode())
    else:
        print(f"Dump was successfully saved in {dump_path}")

    os.chown(dump_path, os.getuid(), os.getgid())

    end = time.time()
    runtime_dump = int(end - start)  # Convert to integer seconds

# Function to clone a git repository
def f_git_clone(path, moodle, config_php, repository, branch, sync_submodules):
    global runtime_clone
    start = time.time()

    clone_path = os.path.join(path, moodle)

    if os.path.exists(clone_path):
        shutil.rmtree(clone_path)

    subprocess.run(['sudo', 'git', 'clone', repository], cwd=path)
    subprocess.run(['sudo', 'git', 'checkout', branch], cwd=clone_path)

    if sync_submodules:
        subprocess.run(['sudo', 'git', 'submodule', 'sync'], cwd=clone_path)
        subprocess.run(['sudo', 'git', 'submodule', 'update', '--init', '--recursive', '--remote'], cwd=clone_path)

    with open(os.path.join(clone_path, 'config.php'), 'w') as config_file:
        config_file.write(config_php)

    subprocess.run(['sudo', 'chown', 'www-data:www-data', clone_path, '-R'])
    print("finished git clone processs")

    end = time.time()
    runtime_clone = int(end - start)  # Convert to integer seconds

# Function to perform directory backup and git clone
def f_dir_backup_git_clone(path, moodle, config_php, full_backup, configphp, repo, branch, sync_submodules):
    print("----------- Backing up Moodle directory ------------------------------------------------------------")
    f_dir_backup(path, moodle, full_backup)

    print("----------- Starting git clone process -------------------------------------------------------------")
    f_git_clone(path, moodle, configphp, repo, branch, sync_submodules)

# Main function to orchestrate script steps
def main():
    global runtime_backup
    global runtime_dump
    global runtime_clone

    # Load configuration
    pwd = os.getcwd()
    CONFIG_PATH = os.path.join(pwd, 'config.ini')

    if not os.path.exists(CONFIG_PATH):
        print(f"Configuration file '{CONFIG_PATH}' not found.")
        print("Update aborted, exiting!")
        exit(1)

    config = load_config(CONFIG_PATH)

    repo = config.get('settings', 'repo')
    branch = config.get('settings', 'branch')
    path = config.get('settings', 'path')
    moodle = config.get('settings', 'moodle', fallback='moodle')
    mt = False

    dir_backup = confirm("Start directory backup process?", "y")
    db_dump = confirm("Start DB dump process?", "y")
    git_clone = confirm("Start git clone process?", "y")

    print("-------------------------------------------------------------------------")
    print(f"dirbackup: {dir_backup}")
    print(f"dbdump: {db_dump}")
    print(f"gitclone: {git_clone}")
    print("-------------------------------------------------------------------------")

    if not dir_backup and not db_dump and not git_clone:
        print("Script aborted")
        exit(1)

    restart_webserver_flag = confirm("Restart webserver automatically?", "y")
    verbose = confirm("Do you want to enable verbose mode?", default='n')

    if dir_backup or git_clone:
        print("----------- Prepare Moodle Path --------------------------------------------------------------------")
        if not confirm(f"Is this the correct Moodle directory? {path}", "y"):
            path = input("Please enter a path: ").rstrip("/")

    if dir_backup:
        print("----------- Prepare directory backup process -------------------------------------------------------")
        full_backup = confirm("Backup entire folder (containing moodle, moodledata and data)?", "n")

    if db_dump:
        print("----------- Prepare database dump process ----------------------------------------------------------")
        dbname = config.get('database', 'db_name', fallback='moodle')
        dbuser = config.get('database', 'db_user', fallback='root')

        if not confirm(f"Use DB {dbname}?", "y"):
            dbname = input("Please enter DB name: ")

        if not confirm(f"Use DB user {dbuser}?", "y"):
            dbuser = input("Please enter DB user: ")

        dbpass = ""
        while not dbpass.strip():
            dbpass = input("Please enter DB password: ")
            if dbpass.strip():
                break  # Exit loop if dbpass is not empty

        result = str(subprocess.run(['mysqlshow', '-u', dbuser, f'-p{dbpass}', dbname], stdout=subprocess.PIPE))

        if "returncode=1" in result:
            print("connection to DB failed")
            dbpass = ""
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
        restart_webserver("stop")

    # Handle multithreading based on user choices
    if dir_backup and db_dump and git_clone:
        mt = True

        t_backup_clone = threading.Thread(
            target=f_dir_backup_git_clone,
            args=(path, moodle, configphp, full_backup, configphp, repo, branch, sync_submodules,)
        )
        t_dump = threading.Thread(target=f_db_dump, args=(dbname, dbuser, dbpass, verbose, pwd,))

        print("----------- Backing up Moodle directory & starting git clone process afterwards | multithreading ---")
        t_backup_clone.start()
        print("----------- Starting database dump | multithreading ------------------------------------------------")
        t_dump.start()

        t_backup_clone.join()
        t_dump.join()
    elif dir_backup and db_dump:
        mt = True

        t_backup = threading.Thread(target=f_dir_backup, args=(path, moodle, full_backup,))
        t_dump = threading.Thread(target=f_db_dump, args=(dbname, dbuser, dbpass, verbose, pwd,))

        print("----------- Backing up Moodle directory | multithreading -------------------------------------------")
        t_backup.start()
        print("----------- Starting database dump | multithreading ------------------------------------------------")
        t_dump.start()

        t_backup.join()
        t_dump.join()
    elif db_dump and git_clone:
        mt = True

        t_dump = threading.Thread(target=f_db_dump, args=(dbname, dbuser, dbpass, verbose, pwd,))
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
            f_dir_backup(path, moodle, full_backup)

        if db_dump:
            print("----------- Starting database dump -----------------------------------------------------------------")
            f_db_dump(dbname, dbuser, dbpass, verbose, pwd)

        if git_clone:
            print("----------- Starting git clone process -------------------------------------------------------------")
            f_git_clone(path, moodle, configphp, repo, branch, sync_submodules)

    if restart_webserver_flag:
        restart_webserver("start")

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
    if mt:
        print("time saved with multithreading:", runtime_backup + runtime_dump + runtime_clone - runtime, "seconds")

    print("------------------------------------------------------------------------------------------")
    print("finished at", time.strftime("%Y-%m-%d %H:%M:%S"))

# Function to start / stop the webserver
def restart_webserver(action):
    cache = apt.Cache()
    if cache['apache2'].is_installed:
        subprocess.run(['sudo', 'systemctl', action, 'apache2'])
    elif cache['nginx'].is_installed:
        subprocess.run(['sudo', 'systemctl', action, 'nginx'])
    else:
        print("failed to " + action + " webserver")

main()
