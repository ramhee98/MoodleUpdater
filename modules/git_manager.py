import os
import sys
import subprocess
import logging
from modules.config_manager import SEPARATOR, ConfigManager


def git_environment(non_interactive):
    """Environment for git subprocesses. In non-interactive mode git and ssh must
    never wait for a username, password or passphrase: terminal prompts are
    disabled and ssh runs in batch mode. Credentials that are already set up
    (credential helpers, ssh keys/agents, tokens in the URL) keep working."""
    env = dict(os.environ)
    if non_interactive:
        env['GIT_TERMINAL_PROMPT'] = '0'
        env['GCM_INTERACTIVE'] = 'never'
        env['SSH_ASKPASS_REQUIRE'] = 'never'
        # Respect an explicitly configured ssh command; otherwise force batch mode.
        env.setdefault('GIT_SSH_COMMAND', 'ssh -o BatchMode=yes')
    return env


def git_stdin(non_interactive):
    """stdin for git subprocesses: closed in non-interactive mode, so nothing can block reading it."""
    return subprocess.DEVNULL if non_interactive else None


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
            # Use maxsplit=2 so a commit summary containing literal '|' is
            # preserved intact rather than truncated or causing the length
            # check below to fail.
            output = result.stdout.strip().split('|', 2)

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
    def _git(pwd, *args, non_interactive=False):
        """Run a git command in pwd. Returns its stripped stdout, or None (after
        logging the error) if it failed."""
        try:
            result = subprocess.run(['git', '-C', pwd, *args], capture_output=True, text=True, check=True,
                                    env=git_environment(non_interactive), stdin=git_stdin(non_interactive))
        except subprocess.CalledProcessError as e:
            logging.error(f"git {' '.join(args)} failed: {e.stderr.strip()}")
            return None
        except FileNotFoundError:
            logging.error("Git command not found. Ensure Git is installed and accessible.")
            return None
        return result.stdout.strip()

    @staticmethod
    def self_update(pwd, CONFIG_PATH, CONFIG_TEMPLATE_PATH, non_interactive=False):
        """Check if running inside a Git repo, ensure no local changes, and pull the latest changes."""
        logging.info(SEPARATOR)
        logging.info("Starting self-update process...")
        try:
            git_dir = os.path.join(pwd, '.git')
            if not os.path.exists(git_dir):
                logging.warning("Not a Git repository. Skipping self-update.")
                return

            current_branch = GitManager._git(pwd, 'rev-parse', '--abbrev-ref', 'HEAD')
            current_commit = GitManager._git(pwd, 'rev-parse', 'HEAD')
            if current_branch is None or current_commit is None:
                logging.info("Continuing with the current version.")
                return
            logging.info(f"Current branch: {current_branch}")

            current_commit_time, current_commit_author, current_commit_summary = GitManager.get_commit_details(current_commit, pwd)
            logging.info(f"Current commit: {current_commit}")
            logging.info(f"Commit time: {current_commit_time}")
            logging.info(f"Author: {current_commit_author}")
            logging.info(f"Summary: {current_commit_summary}")

            # Check for uncommitted changes
            status = GitManager._git(pwd, 'status', '--porcelain')
            if status is None:
                logging.info("Continuing with the current version.")
                return
            if status:
                logging.warning("Local changes detected. Skipping self-update to avoid conflicts.")
                ConfigManager.check_config_differences(CONFIG_PATH, CONFIG_TEMPLATE_PATH)
                return

            # Pull the latest changes from the remote repository to update the script.
            logging.info("Checking for updates...")
            if GitManager._git(pwd, 'pull', '--rebase', non_interactive=non_interactive) is None:
                logging.info("Continuing with the current version.")
                ConfigManager.check_config_differences(CONFIG_PATH, CONFIG_TEMPLATE_PATH)
                return

            # Compare commits rather than parsing git's (localized) output: matching
            # "Already up to date." failed under other locales and caused a restart loop.
            updated_commit = GitManager._git(pwd, 'rev-parse', 'HEAD')
            if updated_commit is None or updated_commit == current_commit:
                logging.info("The script is already up to date.")
                ConfigManager.check_config_differences(CONFIG_PATH, CONFIG_TEMPLATE_PATH)
                return

            updated_commit_time, updated_commit_author, updated_commit_summary = GitManager.get_commit_details(updated_commit, pwd)

            logging.info(f"Updated from commit {current_commit} to commit {updated_commit} on branch {current_branch}")
            logging.info("Old commit details:")
            logging.info(f"  Time: {current_commit_time}")
            logging.info(f"  Author: {current_commit_author}")
            logging.info(f"  Summary: {current_commit_summary}")
            logging.info("New commit details:")
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
