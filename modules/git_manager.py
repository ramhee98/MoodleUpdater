import os
import sys
import subprocess
import logging
from modules.config_manager import ConfigManager

# Constants
SEPARATOR = "-------------------------------------------------------------------------"

class GitManager:
    """Handles Git operations such as fetching commit details and self-updating the script."""

    @staticmethod
    def get_commit_details(commit_hash, pwd):
        """Retrieve commit details (time, author, summary) for a given commit hash."""
        try:
            result = subprocess.run(
                ['git', '-C', pwd, 'show', '-s', '--format=%ci|%an|%s', commit_hash],
                capture_output=True, text=True, check=True
            )
            output = result.stdout.strip().split('|')

            if len(output) == 3:
                return output  # Returns (time, author, summary)
            else:
                logging.warning(f"Unexpected output format from Git for commit {commit_hash}: {output}")
                return "Unknown", "Unknown", "Unknown"
        
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to retrieve commit details for {commit_hash}: {e.stderr}")
        except FileNotFoundError:
            logging.error("Git command not found. Ensure Git is installed and accessible.")
        except Exception as e:
            logging.error(f"Unexpected error retrieving commit details: {e}")

        return "Unknown", "Unknown", "Unknown"

    @staticmethod
    def self_update(pwd, CONFIG_PATH, CONFIG_TEMPLATE_PATH):
        """Check if running inside a Git repo, ensure no local changes, and pull the latest changes."""
        logging.info(SEPARATOR)
        logging.info("Starting self-update process...")
        try:
            git_dir = os.path.join(pwd, '.git')
            if not os.path.exists(git_dir):
                logging.warning("Not a Git repository. Skipping self-update.")
                return

            # Get current branch
            try:
                branch_result = subprocess.run(
                    ['git', '-C', pwd, 'rev-parse', '--abbrev-ref', 'HEAD'],
                    capture_output=True, text=True, check=True
                )
            except subprocess.CalledProcessError as e:
                logging.error(f"Retrieving current branch failed: {e.stderr}")
            current_branch = branch_result.stdout.strip()
            logging.info(f"Current branch: {current_branch}")

            # Get current commit details
            try:
                current_commit_result = subprocess.run(
                    ['git', '-C', pwd, 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True
                )
            except subprocess.CalledProcessError as e:
                logging.error(f"Retrieving commit details failed: {e.stderr}")
            current_commit = current_commit_result.stdout.strip() if current_commit_result.returncode == 0 else "Unknown"
            current_commit_time, current_commit_author, current_commit_summary = GitManager.get_commit_details(current_commit, pwd)

            logging.info(f"Current commit: {current_commit}")
            logging.info(f"Commit time: {current_commit_time}")
            logging.info(f"Author: {current_commit_author}")
            logging.info(f"Summary: {current_commit_summary}")

            # Check for uncommitted changes
            try:
                status_result = subprocess.run(
                    ['git', '-C', pwd, 'status', '--porcelain'], capture_output=True, text=True, check=True
                )
            except subprocess.CalledProcessError as e:
                logging.error(f"Checking for uncommited changes failed: {e.stderr}")
            if status_result.stdout.strip():
                logging.warning("Local changes detected. Skipping self-update to avoid conflicts.")
                ConfigManager.check_config_differences(CONFIG_PATH, CONFIG_TEMPLATE_PATH)
                return

            # Pull the latest changes from the remote repository to update the script.
            logging.info("Checking for updates...")
            try:
                pull_result = subprocess.run(
                    ['git', '-C', pwd, 'pull', '--rebase'], capture_output=True, text=True, check=True
                )
            except subprocess.CalledProcessError as e:
                logging.error(f"Git pull failed: {e.stderr}")


            if "Already up to date." in pull_result.stdout:
                logging.info("The script is already up to date.")
                ConfigManager.check_config_differences(CONFIG_PATH, CONFIG_TEMPLATE_PATH)
            else:
                # Get updated commit details
                try:
                    updated_commit_result = subprocess.run(
                        ['git', '-C', pwd, 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True
                    )
                except subprocess.CalledProcessError as e:
                    logging.error(f"Retrieving updated commit failed: {e.stderr}")

                updated_commit = updated_commit_result.stdout.strip() if updated_commit_result.returncode == 0 else "Unknown"
                updated_commit_time, updated_commit_author, updated_commit_summary = GitManager.get_commit_details(updated_commit, pwd)

                logging.info(f"Updated from commit {current_commit} to commit {updated_commit} on branch {current_branch}")
                logging.info(f"Old commit details:")
                logging.info(f"  Time: {current_commit_time}")
                logging.info(f"  Author: {current_commit_author}")
                logging.info(f"  Summary: {current_commit_summary}")
                logging.info(f"New commit details:")
                logging.info(f"  Time: {updated_commit_time}")
                logging.info(f"  Author: {updated_commit_author}")
                logging.info(f"  Summary: {updated_commit_summary}")

                ConfigManager.check_config_differences(CONFIG_PATH, CONFIG_TEMPLATE_PATH)

                # Restart the script with the updated version
                logging.info("Restarting the script...")
                logging.info(SEPARATOR)
                os.execv(sys.executable, [sys.executable] + sys.argv)

        except Exception as e:
            logging.error(f"Error during self-update: {e}")
            logging.info("Continuing with the current version.")