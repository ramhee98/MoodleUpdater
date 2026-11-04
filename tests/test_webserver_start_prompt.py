import pytest

import moodle_updater


def _confirm(answer=None, raises=None):
    calls = []

    def confirm(question, default, timeout):
        calls.append((question, default, timeout))
        if raises:
            raise raises
        return answer
    return confirm, calls


def test_no_failure_starts_without_asking():
    confirm, calls = _confirm(False)
    assert moodle_updater.confirm_webserver_start([], non_interactive=False, confirm=confirm)
    assert calls == []


def test_non_interactive_starts_without_asking():
    confirm, calls = _confirm(False)
    assert moodle_updater.confirm_webserver_start(["Database dump"], non_interactive=True, confirm=confirm)
    assert calls == []


def test_explicit_no_keeps_webserver_stopped():
    confirm, calls = _confirm(False)
    assert not moodle_updater.confirm_webserver_start(["Database dump"], non_interactive=False, confirm=confirm)
    assert calls[0][1:] == ("y", moodle_updater.WEBSERVER_START_PROMPT_TIMEOUT)


@pytest.mark.parametrize("answer", [True, None])  # None: timeout or closed stdin
def test_yes_or_no_answer_starts(answer):
    confirm, _ = _confirm(answer)
    assert moodle_updater.confirm_webserver_start(["Database dump"], non_interactive=False, confirm=confirm)


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit(1)])  # Ctrl-C, Cancel (c)
def test_interrupted_prompt_starts(exc):
    confirm, _ = _confirm(raises=exc)
    assert moodle_updater.confirm_webserver_start(["Database dump"], non_interactive=False, confirm=confirm)
