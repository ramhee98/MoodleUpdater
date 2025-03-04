import os
import logging
import time
import threading

class SystemMonitor:
    """Monitors system resource usage and database dump progress."""

    def __init__(self):
        self.stop_event = threading.Event()

    def monitor_dump_progress(self, dump_file, check_interval=5, log_interval=60, stagnation_threshold=60):
        """
        Monitors the size of the dump file and logs its progress periodically.
        
        :param dump_file: The path to the dump file.
        :param stop_event: A threading event to signal the thread to stop.
        :param check_interval: Time in seconds between file size checks.
        :param log_interval: Minimum time in seconds between logs.
        :param stagnation_threshold: Time in seconds before logging a stagnation warning.
        """
        logging.info(f"Monitoring database dump progress: {dump_file}")

        last_size = 0
        stagnation_time = 0
        last_log_time = 0

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
                        if size_mb >= 1024:
                            logging.info(f"Database dump progress: {size_mb / 1024:.2f} GB")
                        else:
                            logging.info(f"Database dump progress: {size_mb:.2f} MB")
                        last_log_time = now

                last_size = current_size

            time.sleep(check_interval)

        logging.info("Database dump monitoring stopped.")

    def monitor_memory_usage(self):
        """Monitors memory usage and logs more frequently as free memory decreases."""
        # Track previous memory states to detect recovery
        previous_critical = False
        previous_warning = False
        previous_low_free_critical = False
        previous_low_free_warning = False

        while not self.stop_event.is_set():
            # Get memory statistics
            mem_line = next(line for line in os.popen('free -t -m').readlines() if line.startswith("Mem"))
            total_memory, used_memory, free_memory, shared_memory, buff_cached_memory, available_memory = map(int, mem_line.split()[1:7])

            # === CRITICAL MEMORY STATE ===
            if available_memory < 250:
                logging.critical(
                    "CRITICAL MEMORY WARNING: Available memory critically low (%d MB)! System may soon become unstable.",
                    available_memory
                )
                previous_critical = True  # Track that we are in a critical state
                sleep_time = 0.5

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
                sleep_time = 1

            # RECOVERY: Exiting Warning State
            elif previous_warning and available_memory >= 500:
                logging.info("RECOVERY: Available memory recovered to %d MB, above warning threshold.", available_memory)
                previous_warning = False

            # === CRITICAL: FREE MEMORY EXTREMELY LOW (but available is OK) ===
            if free_memory < 125 and available_memory > 500:
                logging.critical("LOW FREE MEMORY: Free memory is %d MB, but available memory is sufficient (%d MB).", free_memory, available_memory)
                previous_low_free_critical = True
                sleep_time = 0.5

            # RECOVERY: Exiting Critical State
            elif previous_low_free_critical and free_memory >= 125:
                logging.warning("RECOVERY: Free memory increased to %d MB from a critical state.", free_memory)
                previous_low_free_critical = False

            # === FREE MEMORY LOW (but available is OK) ===
            if free_memory < 250 and available_memory > 500:
                if not previous_low_free_warning:
                    logging.warning("LOW FREE MEMORY: Free memory is %d MB, but available memory is sufficient (%d MB).", free_memory, available_memory)
                previous_low_free_warning = True
                sleep_time = 2

            # RECOVERY: Free Memory Restored
            elif previous_low_free_warning and free_memory >= 250:
                logging.info("RECOVERY: Free memory increased to %d MB.", free_memory)
                previous_low_free_warning = False

            # === NORMAL STATE ===
            else:
                sleep_time = 5  # Normal operation

            # Debug log for general memory monitoring
            logging.debug(
                "Memory Stats | Total: %d MB | Used: %d MB | Free: %d MB | Shared: %d MB | Buffers/Cached: %d MB | Available: %d MB",
                total_memory, used_memory, free_memory, shared_memory, buff_cached_memory, available_memory
            )

            time.sleep(sleep_time)

        logging.info("Memory monitoring stopped.")

    def start_monitoring(self, dump_file):
        """Starts monitoring memory and optionally dump progress in separate threads."""
        logging.info("Starting system monitoring...")
        
        self.memory_thread = threading.Thread(target=self.monitor_memory_usage)
        self.memory_thread.start()

        self.dump_thread = threading.Thread(target=self.monitor_dump_progress, args=(dump_file,))
        self.dump_thread.start()

    def stop_monitoring(self):
        """Stops all monitoring threads."""
        logging.info("Stopping system monitoring...")
        self.stop_event.set()

        self.memory_thread.join()
        if hasattr(self, "dump_thread"):
            self.dump_thread.join()