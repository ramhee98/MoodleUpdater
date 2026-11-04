import os
import sys
import logging
import shutil
import signal
from modules.config_manager import SEPARATOR, ConfigManager
from modules.git_manager import GitManager
from modules.moodle_version import MoodleVersionChecker


def running_as_root():
    """True if the process has root privileges (real runs require them)."""
    return os.geteuid() == 0


class ApplicationSetup:
    """Handles configuration loading, logging setup, and initial checks."""

    def __init__(self, pwd, config_path, config_template_path, non_interactive=False, dry_run=False):
        self.pwd = pwd
        self.config_path = config_path
        self.config_template_path = config_template_path
        self.non_interactive = non_interactive
        self.cli_dry_run = dry_run

        # Ensure config file exists before doing anything else, including setting up
        # file logging (which would create the log file first)
        self.ensure_config_exists()

        # Load configuration
        self.config_manager = ConfigManager(self.config_path, pwd)
        self.config = self.config_manager.config

        # Dry run must be known before anything that could change the system,
        # self-update in particular.
        self.dry_run = ConfigManager.get_bool(self.config, 'settings', 'dry_run') or self.cli_dry_run

        # Configure logging
        self.config_manager.configure_logging()

        # Perform initial setup tasks
        self.handle_auto_update()  # 🔹 This may modify config.ini

        # Ensure config is reloaded if changed
        self.config_manager = ConfigManager(self.config_path, pwd)
        self.config = self.config_manager.config

        # Load essential settings
        self.load_core_settings()

        # Exit script if dry run is not enabled and root permissions are missing
        if not self.dry_run and not running_as_root():
            script = os.path.join(self.pwd, 'moodle_updater.py')
            logging.error(f"This script must be run as root. Use 'sudo python3 {script}'")
            sys.exit(1)

        self.warn_if_install_writable_by_others()

    def warn_if_install_writable_by_others(self):
        """Warn when root runs code that a non-root user can modify (directly, or
        through what self-update pulls in): that user can then run code as root."""
        if os.geteuid() != 0:
            return
        for path in (self.pwd, os.path.join(self.pwd, 'moodle_updater.py'), os.path.join(self.pwd, 'modules')):
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_uid != 0 or st.st_mode & 0o022:
                logging.warning(
                    f"SECURITY: {path} is owned by uid {st.st_uid} or writable by group/others, but this "
                    f"script runs as root. Anyone who can modify it can run code as root. "
                    f"Consider: chown -R root:root {self.pwd} && chmod -R go-w {self.pwd}")
                return

    def handle_auto_update(self):
        """Checks if auto-update is enabled and runs it if necessary."""
        if self.dry_run:
            logging.info("[Dry Run] Skipping self-update (it would pull and run new code).")
        elif ConfigManager.get_bool(self.config, 'settings', 'auto_update_script'):
            GitManager.self_update(self.pwd, self.config_path, self.config_template_path, self.non_interactive)
        elif self.non_interactive:
            logging.info("auto_update_script is disabled; skipping self-update in non-interactive mode.")
        else:
            logging.info(SEPARATOR)
            if self.confirm("Pull MoodleUpdater from GitHub?", "n"):
                GitManager.self_update(self.pwd, self.config_path, self.config_template_path)

    def ensure_config_exists(self):
        """Ensures the config file exists, otherwise creates one from the template."""
        if not os.path.exists(self.config_path):
            # File logging is not set up yet (its settings live in the missing file):
            # report on the console only.
            logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s",
                                handlers=[logging.StreamHandler()])
            logging.error(f"Configuration file '{self.config_path}' not found.")
            if self.cli_dry_run:
                logging.info("[Dry Run] Would create config.ini from config_template.ini.")
            elif os.path.exists(self.config_template_path):
                shutil.copy(self.config_template_path, self.config_path)
                logging.info("Configuration file has been created.")
                logging.info("Please edit config.ini to your needs.")
            else:
                logging.error("Missing both config.ini and config_template.ini. Please create config.ini manually.")
            sys.exit(1)

    def load_core_settings(self):
        """Loads core settings like paths, dry-run mode, and Moodle version."""
        self.dry_run = ConfigManager.get_bool(self.config, 'settings', 'dry_run') or self.cli_dry_run
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
            logging.warning("[Dry Run] is enabled! No changes will be made.")
            logging.info(SEPARATOR)

    @staticmethod
    def confirm(question, default='', timeout=None):
        """
        Prompt the user for confirmation with optional default response, cancel functionality, and timeout support.

        Parameters:
        - question (str): The confirmation prompt.
        - default (str, optional): Default response if user just presses Enter (e.g., 'y' or 'n').
        - timeout (int, optional): Number of seconds to wait before returning default.

        Returns:
        - True if user confirms (y)
        - False if user declines (n)
        - None if timeout occurs or stdin is closed with no valid default.
        Answering 'c' exits the script.
        """
        valid_responses = {'y': True, 'n': False, 'c': None}
        option = "Yes(y)/No(n)/Cancel(c)" + (f" Default={default}" if default else "")

        if timeout:
            signal.signal(signal.SIGALRM, lambda signum, frame: (_ for _ in ()).throw(TimeoutError))
            signal.alarm(timeout)  # Start countdown

        try:
            while True:
                user_input = input(f"{question} {option}: ").strip().lower()

                if user_input in valid_responses:
                    signal.alarm(0)  # Reset timeout
                    if user_input == 'c':  # Cancel case
                        logging.warning("User canceled the operation.")
                        sys.exit(1)
                    return valid_responses[user_input]

                elif default and user_input == '':
                    signal.alarm(0)  # Reset timeout
                    return valid_responses.get(default.lower(), False)

        except TimeoutError:  # Catch built-in TimeoutError
            print("\n")
            logging.warning(f"Timeout reached! Using default response: {default or 'No response'}")
            return valid_responses.get(default.lower(), None)

        except EOFError:  # stdin closed (cron, pipe): nobody can answer
            print()
            logging.warning(f"No input available for '{question}'. Using default response: {default or 'No response'}")
            return valid_responses.get(default.lower(), None)

        finally:
            signal.alarm(0)  # Ensure timeout is reset after function exits
