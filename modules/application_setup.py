import os
import sys
import logging
import shutil
from modules.config_manager import ConfigManager
from modules.git_manager import GitManager
from modules.moodle_version import MoodleVersionChecker

# Constants
SEPARATOR = "-------------------------------------------------------------------------"

class ApplicationSetup:
    """Handles configuration loading, logging setup, and initial checks."""
    
    def __init__(self, pwd, config_path, config_template_path):
        self.pwd = pwd
        self.config_path = config_path
        self.config_template_path = config_template_path
        self.config_manager = ConfigManager(self.config_path)
        
        # Load configuration
        self.config = self.config_manager.config
        
        # Configure logging
        self.config_manager.configure_logging()
        
        # Perform initial setup tasks
        self.handle_auto_update()  # 🔹 This may modify config.ini

        # Ensure config is reloaded if changed
        self.config_manager = ConfigManager(self.config_path)  
        self.config = self.config_manager.config  

        # Ensure config file exists
        self.ensure_config_exists()

        # Load essential settings
        self.load_core_settings()

        # Exit script if dry run is not enabled and root permissions are missing 
        if not self.dry_run and os.geteuid() != 0:
            logging.error(f"This script must be run as root. Use 'sudo python3 {__file__}'")
            sys.exit(1)

    def handle_auto_update(self):
        """Checks if auto-update is enabled and runs it if necessary."""
        auto_update = self.config.get('settings', 'auto_update_script', fallback=False)
        if auto_update == "True":
            GitManager.self_update(self.pwd, self.config_path, self.config_template_path)
        else:
            logging.info(SEPARATOR)
            if self.confirm("Pull MoodleUpdater from GitHub?", "n"):
                GitManager.self_update(self.pwd, self.config_path, self.config_template_path)

    def ensure_config_exists(self):
        """Ensures the config file exists, otherwise creates one from the template."""
        if not os.path.exists(self.config_path):
            logging.error(f"Configuration file '{self.config_path}' not found.")
            if os.path.exists(self.config_template_path):
                shutil.copy(self.config_template_path, self.config_path)
                logging.info("Configuration file has been created.")
                logging.info("Please edit config.ini to your needs.")
            else:
                logging.error("Missing both config.ini and config_template.ini. Please create config.ini manually.")
            sys.exit(1)

    def load_core_settings(self):
        """Loads core settings like paths, dry-run mode, and Moodle version."""
        self.dry_run = self.config.get('settings', 'dry_run', fallback="False") == "True"
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
            logging.warning("[Dry Run] is enabled!")
            logging.info(SEPARATOR)

    @staticmethod
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