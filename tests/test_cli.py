import pytest

from modules.cli import build_parser, parse_args, resolve_option


def test_all_existing_flags_are_accepted():
    flags = [
        "--non-interactive", "--directory-backup", "--db-dump", "--git-clone",
        "--moodle-cli-upgrade", "--enable-maintenance-mode", "--force-continue",
        "--restart-webserver", "--restart-database", "--verbose", "--full-backup",
        "--sync-submodules-off", "--restore-submodules", "--restore-plugins",
        "--restore-plugins-manual", "--dry-run",
    ]
    args = parse_args(flags)
    assert all(vars(args).values())


def test_defaults_are_all_false():
    args = parse_args([])
    assert not any(vars(args).values())


def test_unknown_flag_is_rejected(capsys):
    # A typo in --dry-run must not silently become a real run.
    with pytest.raises(SystemExit) as exc:
        parse_args(["--dryrun"])
    assert exc.value.code == 2


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        parse_args(["--help"])
    assert exc.value.code == 0
    assert "--restore-plugins-manual" in capsys.readouterr().out


def test_parser_prog_name():
    assert build_parser().prog == "moodle_updater.py"


def _never_called(*_args, **_kwargs):
    raise AssertionError("confirm must not be called")


def test_resolve_option_flag_wins_without_prompt():
    assert resolve_option(True, "q?", "n", non_interactive=False, confirm=_never_called) is True


def test_resolve_option_non_interactive_uses_default_without_prompt():
    assert resolve_option(False, "q?", "y", non_interactive=True, confirm=_never_called) is False
    assert resolve_option(False, "q?", "n", non_interactive=True, non_interactive_default=True,
                          confirm=_never_called) is True


def test_resolve_option_interactive_asks():
    asked = []

    def confirm(question, default):
        asked.append((question, default))
        return True

    assert resolve_option(False, "Proceed?", "y", non_interactive=False, confirm=confirm) is True
    assert asked == [("Proceed?", "y")]
