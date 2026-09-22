r"""Process-ownership proofs, tested directly and non-destructively.

`jevmulator.ps1` dot-sources `lib/JevmulatorIdentity.ps1`, and so does this suite. The
cases therefore exercise the code that actually runs, not a copy of it, and no case needs
a process or terminates anything.

These exist because two independent probes found the check accepting things it should
refuse. The first version matched the word `jevmulator` anywhere in a command line. The
second compared the state directory with `String.IndexOf`, which accepted a prefix
collision: a daemon owning `C:\probe\.jevmulator-other` satisfied a check for
`C:\probe\.jevmulator`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

pytestmark = [
    pytest.mark.windows,
    pytest.mark.skipif(sys.platform != "win32", reason="the scriptlet needs Windows"),
]

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IDENTITY_LIB = os.path.join(REPO_ROOT, "lib", "JevmulatorIdentity.ps1")

STATE_DIR = r"C:\probe\.jevmulator"


def verdict(command_line: str, expected_state_dir: str = STATE_DIR) -> dict:
    """Run `Test-DaemonCommandLine` against one command line and return its verdict."""
    script = (
        f". '{IDENTITY_LIB}'; "
        "$v = Test-DaemonCommandLine -CommandLine $env:PROBE_CMDLINE "
        "-ExpectedStateDir $env:PROBE_STATEDIR; "
        "[pscustomobject]@{ Ok = [bool]$v.Ok; Reason = [string]$v.Reason } | ConvertTo-Json -Compress"
    )
    environment = dict(os.environ)
    environment["PROBE_CMDLINE"] = command_line
    environment["PROBE_STATEDIR"] = expected_state_dir
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout.strip())


def tokens(command_line: str) -> list[str]:
    """Run `ConvertFrom-ProcessCommandLine` and return the parsed arguments."""
    script = (
        f". '{IDENTITY_LIB}'; "
        "ConvertFrom-ProcessCommandLine -CommandLine $env:PROBE_CMDLINE | ConvertTo-Json -Compress"
    )
    environment = dict(os.environ)
    environment["PROBE_CMDLINE"] = command_line
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    output = completed.stdout.strip()
    if not output:
        return []
    parsed = json.loads(output)
    return parsed if isinstance(parsed, list) else [parsed]


class TestTheLibraryIsShared:
    def test_the_scriptlet_dot_sources_this_file(self) -> None:
        """The tests must not drift from the code. Both use one file."""
        with open(os.path.join(REPO_ROOT, "jevmulator.ps1"), "r", encoding="utf-8") as handle:
            script = handle.read()
        assert "JevmulatorIdentity.ps1" in script
        assert "Test-DaemonCommandLine" in script

    def test_the_scriptlet_no_longer_compares_with_indexof(self) -> None:
        with open(os.path.join(REPO_ROOT, "jevmulator.ps1"), "r", encoding="utf-8") as handle:
            script = handle.read()
        assert "IndexOf($StateDir" not in script


class TestAcceptedCommandLines:
    def test_the_exact_daemon_is_accepted(self) -> None:
        result = verdict(
            r'"C:\Python314\python.exe" -X utf8 -m jevmulator serve --port 8769 '
            r'--state-dir "C:\probe\.jevmulator"'
        )
        assert result["Ok"] is True, result["Reason"]

    def test_an_unquoted_state_directory_is_accepted(self) -> None:
        result = verdict(r"python.exe -m jevmulator serve --state-dir C:\probe\.jevmulator")
        assert result["Ok"] is True, result["Reason"]

    def test_a_trailing_separator_is_accepted(self) -> None:
        """The scriptlet doubles a trailing backslash, so the value ends with one.

        ConvertTo-ProcessArgument writes the pair, because a lone backslash before the
        closing quote would escape it under the Windows parsing rules.
        """
        result = verdict(
            r'python.exe -m jevmulator serve --state-dir "C:\probe\.jevmulator\\"'
        )
        assert result["Ok"] is True, result["Reason"]

    def test_a_single_backslash_before_the_closing_quote_is_refused(self) -> None:
        """That is an escaped quote, so the value is a path with a quote in it.

        Windows parses it that way, so refusing is correct and fails closed.
        """
        result = verdict(r'python.exe -m jevmulator serve --state-dir "C:\probe\.jevmulator\"')
        assert result["Ok"] is False

    def test_the_joined_argument_form_is_accepted(self) -> None:
        result = verdict(r"python.exe -m jevmulator serve --state-dir=C:\probe\.jevmulator")
        assert result["Ok"] is True, result["Reason"]

    def test_a_different_letter_case_is_accepted(self) -> None:
        """Windows paths are case-insensitive, so the comparison is too."""
        result = verdict(r"python.exe -m jevmulator serve --state-dir C:\PROBE\.JEVMULATOR")
        assert result["Ok"] is True, result["Reason"]

    def test_a_relative_path_that_resolves_to_the_same_place_is_accepted(self) -> None:
        result = verdict(
            r"python.exe -m jevmulator serve --state-dir C:\probe\sub\..\.jevmulator"
        )
        assert result["Ok"] is True, result["Reason"]


class TestPrefixCollisions:
    """The exact case the independent probe found."""

    def test_a_sibling_directory_sharing_the_prefix_is_refused(self) -> None:
        result = verdict(
            r"python.exe -m jevmulator serve --state-dir C:\probe\.jevmulator-other"
        )
        assert result["Ok"] is False
        assert ".jevmulator-other" in result["Reason"]

    def test_a_longer_path_under_the_expected_one_is_refused(self) -> None:
        result = verdict(
            r"python.exe -m jevmulator serve --state-dir C:\probe\.jevmulator\nested"
        )
        assert result["Ok"] is False

    def test_a_shorter_parent_path_is_refused(self) -> None:
        result = verdict(r"python.exe -m jevmulator serve --state-dir C:\probe")
        assert result["Ok"] is False

    def test_a_different_drive_is_refused(self) -> None:
        result = verdict(r"python.exe -m jevmulator serve --state-dir D:\probe\.jevmulator")
        assert result["Ok"] is False


class TestTheDirectoryMustBeTheArgumentValue:
    """The expected path appearing elsewhere in the command line proves nothing."""

    def test_the_path_inside_another_argument_is_refused(self) -> None:
        result = verdict(
            r"python.exe -m jevmulator serve --log-file C:\probe\.jevmulator\daemon.log "
            r"--state-dir C:\other\.jevmulator"
        )
        assert result["Ok"] is False
        assert r"C:\other\.jevmulator" in result["Reason"]

    def test_the_path_as_the_executable_is_refused(self) -> None:
        result = verdict(
            r'"C:\probe\.jevmulator\python.exe" -m jevmulator serve --state-dir C:\elsewhere'
        )
        assert result["Ok"] is False

    def test_no_state_directory_argument_is_refused(self) -> None:
        result = verdict(r"python.exe -m jevmulator serve --port 8769")
        assert result["Ok"] is False
        assert "no --state-dir" in result["Reason"]

    def test_a_dangling_state_directory_flag_is_refused(self) -> None:
        result = verdict(r"python.exe -m jevmulator serve --state-dir")
        assert result["Ok"] is False


class TestModuleInvocationIsAnchored:
    def test_the_text_inside_a_path_does_not_count_as_an_invocation(self) -> None:
        result = verdict(
            r'python.exe "C:\tools\-m jevmulator serve\run.py" '
            r"--state-dir C:\probe\.jevmulator"
        )
        assert result["Ok"] is False
        assert "does not invoke" in result["Reason"]

    def test_a_different_module_is_refused(self) -> None:
        result = verdict(
            r"python.exe -m something_else serve --state-dir C:\probe\.jevmulator"
        )
        assert result["Ok"] is False

    def test_a_different_subcommand_is_refused(self) -> None:
        result = verdict(
            r"python.exe -m jevmulator status --state-dir C:\probe\.jevmulator"
        )
        assert result["Ok"] is False

    def test_a_prefixed_module_name_is_refused(self) -> None:
        result = verdict(
            r"python.exe -m jevmulator_evil serve --state-dir C:\probe\.jevmulator"
        )
        assert result["Ok"] is False

    def test_the_bare_word_is_not_enough(self) -> None:
        """The first version accepted any command line containing this word."""
        result = verdict(r"notepad.exe jevmulator --state-dir C:\probe\.jevmulator")
        assert result["Ok"] is False


class TestFailsClosed:
    def test_an_empty_command_line_is_refused(self) -> None:
        result = verdict("")
        assert result["Ok"] is False
        assert "not readable" in result["Reason"]

    def test_a_whitespace_command_line_is_refused(self) -> None:
        result = verdict("   ")
        assert result["Ok"] is False

    def test_an_unrelated_command_line_is_refused(self) -> None:
        result = verdict(r"C:\Windows\System32\notepad.exe C:\notes.txt")
        assert result["Ok"] is False


class TestCommandLineParsing:
    def test_quoted_arguments_stay_whole(self) -> None:
        parsed = tokens(r'"C:\Program Files\Python\python.exe" -m jevmulator serve')
        assert parsed[0] == r"C:\Program Files\Python\python.exe"
        assert parsed[1:] == ["-m", "jevmulator", "serve"]

    def test_a_quoted_path_with_a_space_survives(self) -> None:
        parsed = tokens(r'python.exe --state-dir "C:\jev workspace\.jevmulator"')
        assert parsed[-1] == r"C:\jev workspace\.jevmulator"

    def test_a_trailing_backslash_before_a_quote_is_literal(self) -> None:
        parsed = tokens(r'python.exe --state-dir "C:\dir\\"')
        assert parsed[-1] == "C:\\dir\\"

    def test_an_escaped_quote_is_literal(self) -> None:
        parsed = tokens(r'python.exe --note "a \"quoted\" word"')
        assert parsed[-1] == 'a "quoted" word'

    def test_repeated_whitespace_does_not_create_empty_arguments(self) -> None:
        parsed = tokens("python.exe   -m    jevmulator\tserve")
        assert parsed == ["python.exe", "-m", "jevmulator", "serve"]

    def test_an_empty_command_line_parses_to_nothing(self) -> None:
        assert tokens("") == []


class TestTheWorkspaceUsedByTheLifecycleSuiteCarriesTheLibrary:
    def test_the_library_file_exists(self) -> None:
        assert os.path.isfile(IDENTITY_LIB)

    def test_the_scriptlet_refuses_to_run_without_it(self) -> None:
        """Losing the library must stop the scriptlet, not silently skip the proof."""
        with open(os.path.join(REPO_ROOT, "jevmulator.ps1"), "r", encoding="utf-8") as handle:
            script = handle.read()
        assert "nothing will be stopped" in script
