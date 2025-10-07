import os
import logging
import re
import requests

class MoodleVersionChecker:
    """Handles retrieval of Moodle version information from local and remote sources."""

    def __init__(self, moodle_path, repo_url, branch):
        self.moodle_path = moodle_path
        self.repo_url = repo_url
        self.branch = branch

    def get_local_version(self):
        """Retrieve Moodle version information from the local installation."""
        # Try new structure first (Moodle 5.1+), then fall back to old structure
        version_file = os.path.join(self.moodle_path, "public", "version.php")
        
        if not os.path.exists(version_file):
            # Fall back to old structure (Moodle < 5.1)
            version_file = os.path.join(self.moodle_path, "version.php")
            
        if not os.path.exists(version_file):
            logging.warning("Moodle version file not found.")
            return "Unknown", "Unknown"

        try:
            with open(version_file, "r") as f:
                content = f.read()

            # Human-friendly version (e.g., "4.1+ (Build: 20240115)")
            release_match = re.search(r"\$release\s*=\s*'([^']+)'", content)
            # Numeric version (e.g., "2024042205.00")
            build_match = re.search(r"\$version\s*=\s*([\d\.]+);", content)

            return release_match.group(1) if release_match else "Unknown", \
                   build_match.group(1) if build_match else "Unknown"

        except Exception as e:
            logging.error(f"Error reading Moodle version: {e}")
            return "Unknown", "Unknown"

    def get_remote_version(self):
        """Retrieve Moodle version information from the remote Git repository."""
        # Try new structure first (Moodle 5.1+)
        version_url = f"{self.repo_url.replace('.git', '')}/raw/{self.branch}/public/version.php"

        try:
            response = requests.get(version_url, timeout=10)
            
            # If 404, try old structure (Moodle < 5.1)
            if response.status_code == 404:
                version_url = f"{self.repo_url.replace('.git', '')}/raw/{self.branch}/version.php"
                response = requests.get(version_url, timeout=10)
            
            if response.status_code == 200:
                # Human-friendly version (e.g., "4.1+ (Build: 20240115)")
                release_match = re.search(r"\$release\s*=\s*'([^']+)'", response.text)
                # Numeric version (e.g., "2024042205.00")
                build_match = re.search(r"\$version\s*=\s*([\d\.]+);", response.text)

                return release_match.group(1) if release_match else "Unknown", \
                       build_match.group(1) if build_match else "Unknown"
            else:
                logging.warning(f"Failed to retrieve remote version (HTTP {response.status_code})")
                return "Unknown", "Unknown"

        except requests.RequestException as e:
            logging.error(f"Error fetching remote version: {e}")
            return "Unknown", "Unknown"