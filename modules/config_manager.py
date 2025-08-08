import configparser
import logging
import os
import re
from logging.handlers import RotatingFileHandler

class ConfigManager:
    """Manages configuration loading and logging setup."""
    def __init__(self, config_path, script_dir=None):
        self.config_path = config_path
        self.script_dir = script_dir or os.path.dirname(os.path.abspath(config_path))
        self.config = self.load_config()

    def load_config(self):
        """Load configuration from a file."""
        config = configparser.ConfigParser(interpolation=None)
        config.read(self.config_path)
        #logging.info(f"Loaded configuration from {self.config_path}")
        return config

    def configure_logging(self):
        """Configures logging based on the settings in the configuration file."""
        log_to_console = self.config.getboolean("logging", "log_to_console", fallback=True)
        log_to_file = self.config.getboolean("logging", "log_to_file", fallback=True)
        log_file_path = self.config.get("logging", "log_file_path", fallback="moodle_updater.log")
        log_level = self.config.get("logging", "log_level", fallback="INFO").upper()
        numeric_level = getattr(logging, log_level, logging.INFO)

        # Ensure log file path is absolute and relative to script directory
        if not os.path.isabs(log_file_path):
            log_file_path = os.path.join(self.script_dir, log_file_path)

        handlers = []
        if log_to_console:
            handlers.append(logging.StreamHandler())
        if log_to_file:
            handlers.append(RotatingFileHandler(log_file_path, maxBytes=5 * 1024 * 1024, backupCount=3))

        logging.basicConfig(level=numeric_level, format="%(asctime)s - %(levelname)s - %(message)s", handlers=handlers)
        logging.info(f"Logging configured. Level: {log_level}")
        if log_to_console:
            logging.info("Logging to console enabled.")
        if log_to_file:
            logging.info(f"Logging to file enabled. File path: {log_file_path}")

    @staticmethod
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
            logging.error(f"File {config_path} not found.")
        except Exception as e:
            logging.error(f"An error occurred while reading the Moodle config: {e}")

        return cfg_values

    @staticmethod
    def check_config_differences(config_path, template_path):
        """Check for differences between config.ini and config_template.ini."""
        logging.info("Checking configuration differences.")
        try:
            config = configparser.ConfigParser(interpolation=None)
            template = configparser.ConfigParser(interpolation=None)
            config.read(config_path)
            template.read(template_path)

            all_sections = set(config.sections()).union(set(template.sections()))
            for section in all_sections:
                config_items = set(config.items(section)) if config.has_section(section) else set()
                template_items = set(template.items(section)) if template.has_section(section) else set()
                added = template_items - config_items
                removed = config_items - template_items

                if added or removed:
                    logging.warning(f"Differences in section {section}: Added={added}, Removed={removed}")

        except Exception as e:
            logging.error(f"Error while checking configuration differences: {e}")