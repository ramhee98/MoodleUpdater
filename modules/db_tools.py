"""Helpers shared by everything that talks to MySQL/MariaDB through its client tools."""
import os
import shutil
import subprocess


def sanitize_db_output(text, password):
    """Strip the literal password and password-related warnings from
    mysql/mysqldump output before it goes to the log."""
    if not text:
        return ""
    sanitized = text
    if password:
        sanitized = sanitized.replace(password, "*****")
    # Drop the well-known mysqldump/mysql line about using -p on the CLI;
    # it's noise, and depending on locale variants it can be quoted oddly.
    cleaned_lines = [
        line for line in sanitized.splitlines()
        if "password on the command line" not in line.lower()
    ]
    return "\n".join(cleaned_lines).strip()


def resolve_db_tool(mariadb_name, mysql_name):
    """Return the name of whichever DB client binary is installed.

    Newer MariaDB packages (e.g. Debian 13) only ship the mariadb-* names;
    the mysql* aliases live in an optional compat package. MySQL installs
    only have the mysql* names. Prefer mariadb-* since the mysql* aliases are
    deprecated there, and fall back to mysql_name so the error stays readable
    if neither exists."""
    for name in (mariadb_name, mysql_name):
        if shutil.which(name):
            return name
    return mysql_name


def db_host_args(dbhost):
    """Client arguments for $CFG->dbhost; none means the local default (socket)."""
    return ['-h', dbhost] if dbhost else []


def check_db_connection(dbname, dbuser, dbpass, dbhost=None):
    """Return (True, "") if dbname is reachable with these credentials, else
    (False, sanitized error). The password goes through MYSQL_PWD, never argv,
    where any local user could read it with ps. Raises FileNotFoundError if no
    client tool is installed."""
    result = subprocess.run(
        [resolve_db_tool('mariadb-show', 'mysqlshow'), '-u', dbuser, *db_host_args(dbhost), dbname],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        env={**os.environ, 'MYSQL_PWD': dbpass},
    )
    return result.returncode == 0, sanitize_db_output(result.stderr, dbpass)
