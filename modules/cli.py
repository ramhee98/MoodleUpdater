import argparse

from modules.application_setup import ApplicationSetup

INTERACTIVE_DEFAULT = "default: True unless non-interactive is set then default: False"


def build_parser():
    """Build the command-line parser. Every flag is a plain on-switch; anything not
    passed is asked interactively or falls back to its non-interactive default."""
    parser = argparse.ArgumentParser(
        prog="moodle_updater.py",
        description="Back up, update and upgrade a Moodle instance.",
    )
    parser.add_argument("--non-interactive", action="store_true",
                        help="Run in non-interactive mode, never prompt (default: False)")
    parser.add_argument("--directory-backup", action="store_true",
                        help=f"Start directory backup process ({INTERACTIVE_DEFAULT})")
    parser.add_argument("--db-dump", action="store_true",
                        help=f"Start DB dump process ({INTERACTIVE_DEFAULT})")
    parser.add_argument("--git-clone", action="store_true",
                        help=f"Start git clone process ({INTERACTIVE_DEFAULT})")
    parser.add_argument("--moodle-cli-upgrade", action="store_true",
                        help=f"Start Moodle CLI upgrade process afterwards ({INTERACTIVE_DEFAULT})")
    parser.add_argument("--enable-maintenance-mode", action="store_true",
                        help=f"Enable Moodle Maintenance Mode during CLI Upgrade ({INTERACTIVE_DEFAULT})")
    parser.add_argument("--force-continue", action="store_true",
                        help="Auto-continue even if system check reports errors (default: False)")
    parser.add_argument("--restart-webserver", action="store_true",
                        help=f"Stop the webserver during the update and start it again afterwards ({INTERACTIVE_DEFAULT})")
    parser.add_argument("--restart-database", action="store_true",
                        help="Restart database before dump (default: False)")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable verbose mode (default: False)")
    parser.add_argument("--full-backup", action="store_true",
                        help="Backup entire folder (containing moodle, moodledata, and data) (default: False) "
                             "(used only if --directory-backup is set)")
    parser.add_argument("--sync-submodules-off", action="store_true",
                        help="Disable syncing and updating of all submodules (default: False)")
    parser.add_argument("--restore-submodules", action="store_true",
                        help="Restore git submodules from backup (default: False) (needs --directory-backup enabled) "
                             "(if enabled --sync-submodules-off is set to True)")
    parser.add_argument("--restore-plugins", action="store_true",
                        help="Copy third-party plugins present in the directory backup but missing from the new clone "
                             "(default: False) (needs --directory-backup and --git-clone enabled)")
    parser.add_argument("--restore-plugins-manual", action="store_true",
                        help="Prompt y/n for each plugin instead of restoring all (default: False) "
                             "(used only with --restore-plugins)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run in dry run mode, no changes are made and root is not required (default: False)")
    return parser


def parse_args(argv=None):
    """Parse command-line arguments. Unknown flags are an error, so a typo such as
    --dryrun can never silently turn into a real run."""
    return build_parser().parse_args(argv)


def resolve_option(flag, question, default, non_interactive, non_interactive_default=False, confirm=None):
    """Decide one yes/no option: an explicit flag wins, otherwise ask the user,
    otherwise (non-interactive) use non_interactive_default."""
    if flag:
        return True
    if non_interactive:
        return non_interactive_default
    confirm = confirm or ApplicationSetup.confirm
    return confirm(question, default)
