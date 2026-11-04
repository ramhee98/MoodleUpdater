import logging
import subprocess

PAST_TENSE = {"start": "started", "stop": "stopped", "restart": "restarted"}


class ServiceManager:
    """Handles web and database service restarts."""

    # apt package -> systemd service, in order of preference
    WEBSERVERS = {"apache2": "apache2", "nginx": "nginx"}
    # Only MySQL/MariaDB: the dump supports nothing else, and restarting e.g. Redis
    # would wipe Moodle sessions and caches for no benefit.
    DATABASES = {"mariadb-server": "mariadb", "mysql-server": "mysql"}

    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self._cache = None

    @property
    def cache(self):
        """apt package cache, loaded on first use so python3-apt is only needed
        (and the cache only read) when a service is actually touched."""
        if self._cache is None:
            import apt
            self._cache = apt.Cache()
        return self._cache

    def _installed_service(self, candidates):
        """Return the service of the first installed package in candidates, or None."""
        for package, service in candidates.items():
            if package in self.cache and self.cache[package].is_installed:
                return service
        return None

    def restart_webserver(self, action):
        """start / stop the apache or nginx webserver, depending on which one is
        installed. Returns True on success; no supported webserver is a failure."""
        webserver = self._installed_service(self.WEBSERVERS)
        if not webserver:
            logging.error(f"Cannot {action} the webserver: no supported web server found (Apache/Nginx).")
            return False

        logging.info(f"Attempting to {action} the {webserver} service.")
        return self._run_systemctl(action, webserver)

    def restart_database(self, action):
        """Start / stop the installed MySQL/MariaDB service. Returns True on success."""
        service = self._installed_service(self.DATABASES)
        if not service:
            logging.error(f"Cannot {action} the database: no supported database server found (MariaDB/MySQL).")
            return False

        logging.info(f"Attempting to {action} the {service} service.")
        return self._run_systemctl(action, service)

    def _run_systemctl(self, action, service_name):
        """Runs the systemctl command for service management. Returns True on success."""
        if self.dry_run:
            logging.info(f"[Dry Run] Would run: systemctl {action} {service_name}")
            return True
        try:
            subprocess.run(['systemctl', action, service_name], check=True, stdin=subprocess.DEVNULL)
            logging.info(f"{service_name} service {PAST_TENSE.get(action, action)} successfully.")
            return True
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to {action} the {service_name} service (exit code {e.returncode}).")
        except OSError as e:
            logging.error(f"Failed to {action} the {service_name} service: {e}")
        return False
