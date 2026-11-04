import os
import re
import logging
import time
import threading
import subprocess

from modules.db_tools import resolve_db_tool, sanitize_db_output

# MySQL identifier rules: letters, digits, underscore, dollar sign (1-64 chars).
# We also allow hyphens because Moodle deployments often use db names like
# "moodle-prod". Anything else is rejected to prevent SQL injection via the
# database name interpolated into the SQL query below.
_DB_NAME_RE = re.compile(r'^[A-Za-z0-9_$\-]{1,64}$')


class SystemMonitor:
    """Monitors system resource usage and database dump progress."""

    # Upper bounds so a hanging client or monitor can never hold up the dump's cleanup.
    SIZE_QUERY_TIMEOUT = 30
    STOP_TIMEOUT = 5

    def __init__(self):
        self.stop_event = threading.Event()
        self.memory_thread = None
        self.dump_thread = None

    def get_database_size_mb(self, database, user, password, host=None):
        """Return the size of the database in MB, or None if it can't be determined."""
        # Reject any database name that is not a plain identifier. Without
        # this check a malicious or malformed name (e.g. one containing a
        # quote) would be interpolated directly into the SQL string below.
        if not _DB_NAME_RE.match(database or ''):
            logging.error(f"Refusing to query database with invalid name: {database!r}")
            return None
        # SQL query to get database size
        query = """
        SELECT
            table_schema AS `Database`,
            ROUND(SUM(data_length + index_length) / 1024 / 1024, 2) AS `Size (MB)`
        FROM
            information_schema.tables
        WHERE
            table_schema = '{}'
        GROUP BY
            table_schema;
        """.format(database)

        cmd = [
            resolve_db_tool('mariadb', 'mysql'),
            '-u', user,
            *(['-h', host] if host else []),
            '-N',   # Skip column names in output
            '-e', query,
        ]
        # Pass password via MYSQL_PWD env var so it is not visible in `ps`.
        env = {**os.environ, 'MYSQL_PWD': password}

        try:
            # subprocess.run kills and reaps the client when the timeout expires.
            result = subprocess.run(cmd, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
                                    timeout=self.SIZE_QUERY_TIMEOUT)
        except FileNotFoundError:
            logging.warning("Neither mariadb nor mysql client found; dump size estimate unavailable.")
            return None
        except subprocess.TimeoutExpired:
            logging.warning(f"Database size query did not finish within {self.SIZE_QUERY_TIMEOUT}s; dump size estimate unavailable.")
            return None

        if result.returncode != 0:
            logging.warning(f"Could not determine database size: {sanitize_db_output(result.stderr, password)}")
            return None
        try:
            _, size_mb = result.stdout.strip().split('\t')
            return float(size_mb)
        except ValueError:
            return None

    def monitor_dump_progress(self, dump_file, database, user, password, host=None, check_interval=5, log_interval=60, stagnation_threshold=60):
        """
        Monitors the size of the dump file and logs its progress periodically.

        :param dump_file: The path to the dump file.
        :param check_interval: Time in seconds between file size checks.
        :param log_interval: Minimum time in seconds between logs.
        :param stagnation_threshold: Time in seconds before logging a stagnation warning.
        """
        last_size = 0
        stagnation_time = 0
        last_log_time = 0
        start_time = time.time()
        approximate_db_to_dump_ratio = 0.644  # Adjust this ratio based on database characteristics
        db_size_mb = self.get_database_size_mb(database, user, password, host)
        # Without a size estimate only absolute progress is reported.
        estimated_total_size = db_size_mb * approximate_db_to_dump_ratio if db_size_mb else None
        if estimated_total_size:
            logging.info(f"Monitoring database dump progress: {dump_file} | Estimated size: {estimated_total_size / 1024:.2f} GB")
        else:
            logging.info(f"Monitoring database dump progress: {dump_file} | Estimated size: unknown")

        while not self.stop_event.is_set():
            if os.path.exists(dump_file):
                current_size = os.path.getsize(dump_file)
                now = time.time()

                if current_size == last_size:
                    stagnation_time += check_interval
                    if stagnation_time >= stagnation_threshold and now - last_log_time >= log_interval:
                        logging.warning(f"Database dump file size hasn't changed for {stagnation_time} seconds. Possible stall?")
                        last_log_time = now
                else:
                    stagnation_time = 0
                    if now - last_log_time >= log_interval:
                        size_mb = current_size / (1024 * 1024)
                        elapsed_time = now - start_time
                        size_text = f"{size_mb / 1024:.2f} GB" if size_mb >= 1024 else f"{size_mb:.2f} MB"
                        if estimated_total_size:
                            speed = size_mb / elapsed_time if elapsed_time > 0 else 0  # MB per second
                            remaining_time_sec = (estimated_total_size - size_mb) / speed if speed > 0 else float('inf')
                            percent = (size_mb / estimated_total_size) * 100
                            logging.info(f"Database dump progress: {percent:.2f}% | {size_text} | Elapsed: {elapsed_time:.1f}s | Estimated remaining: {remaining_time_sec:.1f}s")
                        else:
                            logging.info(f"Database dump progress: {size_text} | Elapsed: {elapsed_time:.1f}s")
                        last_log_time = now
                last_size = current_size

            self.stop_event.wait(check_interval)

        logging.info("Database dump monitoring stopped.")

    @staticmethod
    def read_memory_stats():
        """Return (total, used, free, shared, buff/cache, available) in MB from `free -m`."""
        output = subprocess.run(['free', '-m'], capture_output=True, text=True, check=True, timeout=10).stdout
        mem_line = next(line for line in output.splitlines() if line.startswith("Mem"))
        return tuple(map(int, mem_line.split()[1:7]))

    def monitor_memory_usage(self):
        """Monitors memory usage and logs more frequently as free memory decreases."""
        # Track previous memory states to detect recovery
        previous_critical = False
        previous_warning = False
        previous_low_free_critical = False
        previous_low_free_warning = False

        while not self.stop_event.is_set():
            # Get memory statistics
            total_memory, used_memory, free_memory, shared_memory, buff_cached_memory, available_memory = self.read_memory_stats()
            # Each state below may shorten the polling interval; the shortest wins.
            sleep_time = 5  # Normal operation

            # === CRITICAL MEMORY STATE ===
            if available_memory < 250:
                logging.critical(
                    "CRITICAL MEMORY WARNING: Available memory critically low (%d MB)! System may soon become unstable.",
                    available_memory
                )
                previous_critical = True  # Track that we are in a critical state
                sleep_time = min(sleep_time, 0.5)

            # RECOVERY: Exiting Critical State
            elif previous_critical and available_memory >= 250:
                logging.error("RECOVERY: Available memory recovered to %d MB from a critical state.", available_memory)
                previous_critical = False  # Reset state

            # === WARNING MEMORY STATE ===
            if available_memory < 500:
                if not previous_warning and not previous_critical:  # Only log if not already in a worse state
                    logging.warning(
                        "LOW MEMORY WARNING: Available memory below 500 MB (%d MB). Performance may degrade.",
                        available_memory
                    )
                previous_warning = True
                sleep_time = min(sleep_time, 1)

            # RECOVERY: Exiting Warning State
            elif previous_warning and available_memory >= 500:
                logging.info("RECOVERY: Available memory recovered to %d MB, above warning threshold.", available_memory)
                previous_warning = False

            # === CRITICAL: FREE MEMORY EXTREMELY LOW (but available is OK) ===
            if free_memory < 125 and available_memory > 500:
                logging.critical("LOW FREE MEMORY: Free memory is %d MB, but available memory is sufficient (%d MB).", free_memory, available_memory)
                previous_low_free_critical = True
                sleep_time = min(sleep_time, 0.5)

            # RECOVERY: Exiting Critical State
            elif previous_low_free_critical and free_memory >= 125:
                logging.warning("RECOVERY: Free memory increased to %d MB from a critical state.", free_memory)
                previous_low_free_critical = False

            # === FREE MEMORY LOW (but available is OK) ===
            if free_memory < 250 and available_memory > 500:
                if not previous_low_free_warning:
                    logging.warning("LOW FREE MEMORY: Free memory is %d MB, but available memory is sufficient (%d MB).", free_memory, available_memory)
                previous_low_free_warning = True
                sleep_time = min(sleep_time, 2)

            # RECOVERY: Free Memory Restored
            elif previous_low_free_warning and free_memory >= 250:
                logging.info("RECOVERY: Free memory increased to %d MB.", free_memory)
                previous_low_free_warning = False

            # Debug log for general memory monitoring
            logging.debug(
                "Memory Stats | Total: %d MB | Used: %d MB | Free: %d MB | Shared: %d MB | Buffers/Cached: %d MB | Available: %d MB",
                total_memory, used_memory, free_memory, shared_memory, buff_cached_memory, available_memory
            )

            self.stop_event.wait(sleep_time)

        logging.info("Memory monitoring stopped.")

    @staticmethod
    def _guarded(name, target, *args):
        """Run a monitoring loop; a crash ends monitoring with a log entry instead
        of a silent stderr traceback, and never affects the dump itself."""
        try:
            target(*args)
        except Exception:
            logging.exception(f"{name} stopped because of an unexpected error.")

    def start_monitoring(self, dump_file, dbname, dbuser, dbpass, dbhost=None):
        """Starts monitoring memory and optionally dump progress in separate threads."""
        logging.info("Starting system monitoring...")

        # Daemon threads with a bounded join in stop_monitoring(): monitoring is
        # best effort and must never keep the update (or the process) waiting.
        self.memory_thread = threading.Thread(target=self._guarded, args=("Memory monitoring", self.monitor_memory_usage),
                                              name="Memory monitoring", daemon=True)
        self.memory_thread.start()

        self.dump_thread = threading.Thread(target=self._guarded, args=(
            "Dump progress monitoring", self.monitor_dump_progress, dump_file, dbname, dbuser, dbpass, dbhost),
            name="Dump progress monitoring", daemon=True)
        self.dump_thread.start()

    def stop_monitoring(self):
        """Stops all monitoring threads."""
        logging.info("Stopping system monitoring...")
        self.stop_event.set()

        deadline = time.monotonic() + self.STOP_TIMEOUT
        for thread in (self.memory_thread, self.dump_thread):
            if thread is not None:
                thread.join(max(0, deadline - time.monotonic()))
                if thread.is_alive():
                    logging.warning(f"{thread.name} did not stop within {self.STOP_TIMEOUT}s; continuing without it.")
