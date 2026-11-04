import configparser
import logging
import os
import re
import sys
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
    def get_bool(config, section, option, fallback=False):
        """Read a boolean setting. Accepts true/false, yes/no, on/off and 1/0 in any
        case; anything else aborts instead of silently meaning False (a mistyped
        dry_run must never turn into a real run)."""
        try:
            return config.getboolean(section, option, fallback=fallback)
        except ValueError:
            logging.error(f"Invalid boolean for [{section}] {option}: {config.get(section, option)!r}. Use True or False.")
            sys.exit(1)

    @staticmethod
    def read_moodle_config(config_path):
        """Reads the Moodle config.php file and extracts $CFG->dbhost, $CFG->dbname,
        $CFG->dbuser and $CFG->dbpass. Values may use single or double quotes and may
        be empty; missing settings are returned as None."""
        cfg_values = {}

        try:
            with open(config_path, 'r') as file:
                content = file.read()

            for key in ('dbhost', 'dbname', 'dbuser', 'dbpass'):
                # A quoted PHP string at the start of a line (so commented-out lines
                # don't match); backslash escapes are allowed inside it.
                match = re.search(rf"^\s*\$CFG->{key}\s*=\s*(['\"])((?:\\.|(?!\1).)*)\1\s*;", content, re.M)
                cfg_values[key] = ConfigManager._php_unescape(match.group(2), match.group(1)) if match else None

        except FileNotFoundError:
            logging.error(f"File {config_path} not found.")
        except Exception as e:
            logging.error(f"An error occurred while reading the Moodle config: {e}")

        return cfg_values

    @staticmethod
    def _php_unescape(value, quote):
        """Undo PHP's escapes in a quoted literal: an escaped backslash, an escaped
        quote character and (in double quotes) an escaped dollar sign."""
        escapes = {'\\': '\\', quote: quote}
        if quote == '"':
            escapes['$'] = '$'
        return re.sub(r'\\(.)', lambda m: escapes.get(m.group(1), '\\' + m.group(1)), value)

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