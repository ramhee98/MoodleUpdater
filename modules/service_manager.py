import apt
import logging
import subprocess

class ServiceManager:
    """Handles web and database service restarts."""

    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.cache = apt.Cache()  # Load package cache once and reuse

    def restart_webserver(self, action):
        """start / stop the apache or nginx webserver, depending on which one is installed"""
        webserver = None

        if self.cache["apache2"].is_installed:
            webserver = "apache2"
        elif self.cache["nginx"].is_installed:
            webserver = "nginx"

        if not webserver:
            logging.warning("No supported web server found (Apache/Nginx).")
            return

        logging.info(f"Attempting to {action} the {webserver} service.")
        self._run_systemctl(action, webserver)

    def restart_database(self, action):
        """Start / stop the installed database service, based on availability."""
        database_services = {
            "mysql-server": "mysql",
            "mariadb-server": "mariadb",
            "postgresql": "postgresql",
            "mssql-server": "mssql-server",
            "mongodb": "mongodb",
            "redis-server": "redis",
        }

        # Identify installed database services
        installed_db_services = [service for service in database_services if service in self.cache and self.cache[service].is_installed]

        if not installed_db_services:
            logging.warning("No supported database server found.")
            return

        for db_service in installed_db_services:
            service_name = database_services[db_service]
            logging.info(f"Attempting to {action} the {service_name} service.")
            self._run_systemctl(action, service_name)

    def _run_systemctl(self, action, service_name):
        """Runs the systemctl command for service management."""
        if self.dry_run:
            logging.info(f"[Dry Run] Would run: systemctl {action} {service_name}")
        else:
            try:
                subprocess.run(['systemctl', action, service_name], check=True)
                logging.info(f"{service_name} service {action}ed successfully.")
            except subprocess.CalledProcessError as e:
                logging.error(f"Failed to {action} the {service_name} service: {e.stderr}")