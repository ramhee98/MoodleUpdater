import configparser
import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler

# Visual divider between sections of the log
SEPARATOR = "-------------------------------------------------------------------------"

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
        $CFG->dbuser and $CFG->dbpass.

        Only plain single- or double-quoted string literals are understood. Comments
        are ignored and, as in PHP, the last assignment wins. A setting that is
        missing, or whose last assignment is not a plain literal (a function call,
        a constant, a string with variable interpolation), is returned as None, so
        the caller fails instead of using a wrong value."""
        cfg_values = {}

        try:
            with open(config_path, 'r') as file:
                content = ConfigManager._strip_php_comments(file.read())

            for key in ('dbhost', 'dbname', 'dbuser', 'dbpass'):
                cfg_values[key] = ConfigManager._last_literal_assignment(content, key)

        except FileNotFoundError:
            logging.error(f"File {config_path} not found.")
        except Exception as e:
            logging.error(f"An error occurred while reading the Moodle config: {e}")

        return cfg_values

    @staticmethod
    def _strip_php_comments(content):
        """Remove //, # and /* */ comments, leaving string literals untouched."""
        out, i, n = [], 0, len(content)
        while i < n:
            char = content[i]
            if char in "'\"":
                end = i + 1
                while end < n and content[end] != char:
                    end += 2 if content[end] == '\\' else 1
                out.append(content[i:end + 1])
                i = end + 1
            elif content.startswith('/*', i):
                end = content.find('*/', i + 2)
                out.append(' ')
                i = n if end < 0 else end + 2
            elif content.startswith('//', i) or char == '#':
                end = content.find('\n', i)
                i = n if end < 0 else end
            else:
                out.append(char)
                i += 1
        return ''.join(out)

    @staticmethod
    def _last_literal_assignment(content, key):
        """Value of the last `$CFG->key = ...;` in content, or None if there is none
        or it is not a plain string literal."""
        assignments = list(re.finditer(rf"\$CFG\s*->\s*{key}\s*=\s*", content))
        if not assignments:
            return None
        start = assignments[-1].end()
        literal = re.match(r"(['\"])((?:\\.|(?!\1).)*)\1\s*;", content[start:], re.S)
        if not literal:
            logging.error(f"$CFG->{key} in config.php is not a plain string literal; cannot read it.")
            return None
        quote, raw = literal.group(1), literal.group(2)
        if quote == '"' and re.search(r'(?<!\\)(?:\\\\)*\$[A-Za-z_{]', raw):
            logging.error(f"$CFG->{key} in config.php uses variable interpolation; cannot read it.")
            return None
        return ConfigManager._php_unescape(raw, quote)

    # Escapes PHP understands in double-quoted strings (besides octal, \x and \u{})
    _DOUBLE_QUOTE_ESCAPES = {'n': '\n', 't': '\t', 'r': '\r', 'v': '\v', 'e': '\x1b', 'f': '\f',
                             '\\': '\\', '$': '$', '"': '"'}

    @staticmethod
    def _php_unescape(value, quote):
        """Undo PHP's escapes in a quoted literal. Single quotes only know an escaped
        backslash and quote; double quotes also know \\n, \\t, octal, \\x and \\u{}.
        Unknown escapes keep their backslash, as in PHP."""
        if quote == "'":
            return re.sub(r"\\([\\'])", r"\1", value)

        def replace(match):
            escape = match.group(1)
            if escape[0] in '01234567':
                return chr(int(escape, 8) & 0xFF)
            if escape[0] == 'x':
                return chr(int(escape[1:], 16))
            if escape[0] == 'u':
                return chr(int(escape[2:-1], 16))
            return ConfigManager._DOUBLE_QUOTE_ESCAPES.get(escape, '\\' + escape)

        return re.sub(r'\\([0-7]{1,3}|x[0-9A-Fa-f]{1,2}|u\{[0-9A-Fa-f]+\}|.)', replace, value, flags=re.S)

    @staticmethod
    def check_config_differences(config_path, template_path):
        """Report settings that config_template.ini has but config.ini lacks, and
        the reverse. Only keys are compared: customized values are expected."""
        logging.info("Checking configuration differences.")
        try:
            config = configparser.ConfigParser(interpolation=None)
            template = configparser.ConfigParser(interpolation=None)
            config.read(config_path)
            template.read(template_path)

            for section in sorted(set(config.sections()) | set(template.sections())):
                config_keys = set(config.options(section)) if config.has_section(section) else set()
                template_keys = set(template.options(section)) if template.has_section(section) else set()
                missing = sorted(template_keys - config_keys)
                unknown = sorted(config_keys - template_keys)
                if missing:
                    logging.warning(f"[{section}] settings missing from config.ini (see config_template.ini): {', '.join(missing)}")
                if unknown:
                    logging.warning(f"[{section}] settings in config.ini that the template does not know: {', '.join(unknown)}")

        except Exception as e:
            logging.error(f"Error while checking configuration differences: {e}")
