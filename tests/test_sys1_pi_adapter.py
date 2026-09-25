"""The pi adapter, without running pi: command line, environment, readiness, events."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jevmulator.config import config_from_env
from jevmulator.sys1.harness import pi as pi_module
from jevmulator.sys1.harness.base import LaunchSpec
from jevmulator.sys1.harness.events import digest
from jevmulator.sys1.harness.pi import KICKOFF, PI_SETTINGS, PiHarness
from jevmulator.sys1.profiles import load_profiles

STATE_WITH_SHELL_CHARACTERS = 'refund & delete | echo ^ %PATH% "quoted"'


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A fake node, a fake pi entry point and a fake user pi directory."""
    node = tmp_path / "node.exe"
    node.write_bytes(b"")
    cli = tmp_path / "pi" / "cli.js"
    cli.parent.mkdir()
    cli.write_text("// fake", encoding="utf-8")
    user_home = tmp_path / "userhome"
    user_bin = user_home / ".pi" / "agent" / "bin"
    user_bin.mkdir(parents=True)
    for name in pi_module.SEARCH_BINARIES:
        (user_bin / name).write_bytes(b"binary")
    monkeypatch.setattr(Path, "home", lambda: user_home)
    monkeypatch.setenv("JEVMULATOR_SYS1_NODE", str(node))
    monkeypatch.setenv("JEVMULATOR_SYS1_PI_CLI", str(cli))
    monkeypatch.setenv("ZAI_API_KEY", "zai-test-key")
    monkeypatch.setenv("JEVMULATOR_SYS1_HARNESS", "pi")
    return {"node": node, "cli": cli, "user_bin": user_bin}


def harness() -> PiHarness:
    return PiHarness(config_from_env(port=8769))


def spec(tmp_path: Path, profile_name: str = "read-only") -> LaunchSpec:
    profile = load_profiles()[profile_name]
    run_dir = tmp_path / "runs" / ("a" * 32)
    (run_dir / "work").mkdir(parents=True)
    (run_dir / "session").mkdir()
    (run_dir / "state.json").write_text(json.dumps(STATE_WITH_SHELL_CHARACTERS), encoding="utf-8")
    brief = run_dir / "brief.md"
    brief.write_text("brief holding " + STATE_WITH_SHELL_CHARACTERS, encoding="utf-8")
    return LaunchSpec(
        run_id="a" * 32,
        run_dir=run_dir,
        work_dir=run_dir / "work",
        session_dir=run_dir / "session",
        brief_path=brief,
        events_path=run_dir / "events.jsonl",
        stderr_path=run_dir / "stderr.log",
        tools=profile.tools,
        read_roots=profile.read_roots,
        settings=profile.harness_settings("pi"),
        submit_url="http://127.0.0.1:8769/_jevmulator/sys1/runs/" + "a" * 32 + "/submission",
        hello_url="http://127.0.0.1:8769/_jevmulator/sys1/runs/" + "a" * 32 + "/hello",
        token="run-token",
        max_nudges=2,
    )


class TestCommand:
    def test_read_only_command_line(self, install, tmp_path):
        adapter = harness()
        launch = spec(tmp_path)
        argv = adapter.command(launch)
        assert argv[:2] == [str(install["node"]), str(install["cli"])]
        assert argv[-1] == KICKOFF
        flags = argv[2:-1]
        for isolation in ("--no-extensions", "--no-skills", "--no-context-files",
                          "--no-prompt-templates", "--no-themes", "--offline", "--no-approve"):
            assert isolation in flags
        assert flags[flags.index("--mode") + 1] == "json"
        assert flags[flags.index("--provider") + 1] == "zai"
        assert flags[flags.index("--model") + 1] == "glm-5.3-flash"
        assert flags[flags.index("--thinking") + 1] == "high"
        assert flags[flags.index("--tools") + 1] == "read,grep,find,ls,submit_verdict"
        assert flags[flags.index("--system-prompt") + 1] == str(launch.brief_path)
        assert flags[flags.index("-e") + 1] == str(pi_module.EXTENSION)
        assert flags[flags.index("--session-dir") + 1] == str(launch.session_dir)
        assert pi_module.EXTENSION.is_file()

    def test_caller_text_never_reaches_the_command_line(self, install, tmp_path):
        argv = harness().command(spec(tmp_path))
        joined = "\n".join(argv)
        for fragment in ("refund", "delete", "%PATH%", "quoted"):
            assert fragment not in joined

    def test_shell_profile_maps_shell_to_bash_and_never_grants_powershell(self, install, tmp_path):
        argv = harness().command(spec(tmp_path, "prototype-first"))
        tools = argv[argv.index("--tools") + 1].split(",")
        assert tools == ["read", "grep", "find", "ls", "bash", "write", "edit", "submit_verdict"]
        assert "powershell" not in tools

    def test_identity(self, install):
        adapter = harness()
        profiles = load_profiles()
        assert adapter.model_id(profiles["read-only"]) == "zai/glm-5.3-flash"
        assert adapter.tool_names(profiles["read-only"]) == ["find", "grep", "ls", "read", "submit_verdict"]
        assert adapter.tool_names(profiles["prototype-first"]) == sorted(
            ["read", "grep", "find", "ls", "bash", "write", "edit", "submit_verdict"]
        )


class TestEnvironment:
    def test_built_from_the_allowlist(self, install, tmp_path, monkeypatch):
        monkeypatch.setenv("JEVMULATOR_API_KEY", "daemon-key")
        monkeypatch.setenv("OPENAI_API_KEY", "other-provider-key")
        monkeypatch.setenv("GITHUB_TOKEN", "a-token")
        adapter = harness()
        env = adapter.environment(spec(tmp_path))
        assert env["ZAI_API_KEY"] == "zai-test-key"
        assert env["PI_CODING_AGENT_DIR"] == str(adapter.agent_dir)
        assert env["SYS1_RUN_TOKEN"] == "run-token"
        assert env["SYS1_SUBMIT_URL"].endswith("/submission")
        assert env["SYS1_HELLO_URL"].endswith("/hello")
        assert env["SYS1_REPLACE_BASH"] == "0"
        assert json.loads(env["SYS1_READ_ROOTS"]) == []
        assert env["NO_PROXY"] == "127.0.0.1,localhost"
        assert env["GIT_CEILING_DIRECTORIES"].endswith("a" * 32)
        for secret in ("JEVMULATOR_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN"):
            assert secret not in env
        if os.name == "nt":
            # Hand test H6 (2026-09-25): without ProgramFiles, pi finds no Git Bash.
            assert env["ProgramFiles"] == os.environ["ProgramFiles"]
        assert not [name for name in env if name.startswith("JEVMULATOR_")]
        assert "daemon-key" not in json.dumps(env)

    def test_shell_profile_asks_the_extension_to_replace_bash(self, install, tmp_path):
        env = harness().environment(spec(tmp_path, "prototype-first"))
        assert env["SYS1_REPLACE_BASH"] == "1"


class TestReadiness:
    def test_ready_with_everything_present(self, install, monkeypatch, tmp_path):
        program_files = tmp_path / "ProgramFiles"
        (program_files / "Git" / "bin").mkdir(parents=True)
        (program_files / "Git" / "bin" / "bash.exe").write_bytes(b"")
        monkeypatch.setenv("ProgramFiles", str(program_files))
        adapter = harness()
        assert adapter.problems() == []
        settings = json.loads((adapter.agent_dir / "settings.json").read_text(encoding="utf-8"))
        assert settings["packages"] == []
        assert {key: value for key, value in settings.items() if key != "shellPath"} == PI_SETTINGS
        if os.name == "nt":
            # pi finds Git Bash only under %ProgramFiles%; the settings name it outright.
            assert settings["shellPath"] == str(program_files / "Git" / "bin" / "bash.exe")
        for name in pi_module.SEARCH_BINARIES:
            assert (adapter.agent_dir / "bin" / name).is_file()

    def test_no_shell_path_without_git_bash(self, install, monkeypatch, tmp_path):
        for variable in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            monkeypatch.setenv(variable, str(tmp_path / "empty"))
        adapter = harness()
        settings = json.loads((adapter.agent_dir / "settings.json").read_text(encoding="utf-8"))
        assert "shellPath" not in settings

    def test_each_missing_piece_is_named(self, install, monkeypatch, tmp_path):
        monkeypatch.setenv("JEVMULATOR_SYS1_NODE", str(tmp_path / "no-node.exe"))
        monkeypatch.setenv("JEVMULATOR_SYS1_PI_CLI", str(tmp_path / "no-cli.js"))
        monkeypatch.delenv("ZAI_API_KEY")
        for name in pi_module.SEARCH_BINARIES:
            (install["user_bin"] / name).unlink()
        problems = " ".join(harness().problems())
        assert "JEVMULATOR_SYS1_NODE" in problems
        assert "JEVMULATOR_SYS1_PI_CLI" in problems
        assert "ZAI_API_KEY" in problems
        assert "search tools" in problems

    def test_a_missing_brief_is_refused_before_pi_starts(self, install, tmp_path):
        launch = spec(tmp_path)
        launch.brief_path.unlink()
        with pytest.raises(OSError, match="does not exist"):
            harness().launch(launch)

    def test_the_entry_point_is_found_next_to_the_shim(self, tmp_path, monkeypatch):
        shim_dir = tmp_path / "npm"
        cli = shim_dir / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist" / "bundle" / "cli.js"
        cli.parent.mkdir(parents=True)
        cli.write_text("// fake", encoding="utf-8")
        (shim_dir / "pi.cmd").write_text("@echo off", encoding="utf-8")
        monkeypatch.setattr(pi_module.shutil, "which", lambda name: str(shim_dir / "pi.cmd"))
        assert pi_module.derive_cli() == str(cli)


class TestEvents:
    #: The shapes pi 0.85.1 writes in --mode json (AssistantMessage in pi-ai's types.d.ts).
    STREAM = [
        {"type": "session", "version": 3, "id": "s"},
        {"type": "agent_start"},
        {"type": "message_end", "message": {"role": "user", "content": "Judge the questions"}},
        {"type": "message_end", "message": {
            "role": "assistant", "provider": "zai", "model": "glm-5.3-flash", "stopReason": "toolUse",
            "usage": {"input": 5200, "output": 180, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 5380}}},
        {"type": "tool_execution_end", "toolName": "read", "isError": False},
        {"type": "message_end", "message": {
            "role": "assistant", "provider": "zai", "model": "glm-5.3-flash", "stopReason": "toolUse",
            "usage": {"input": 1100, "output": 240, "cacheRead": 5100, "cacheWrite": 0, "totalTokens": 6440}}},
        {"type": "tool_execution_end", "toolName": "submit_verdict", "isError": False},
        {"type": "agent_end", "messages": []},
        {"type": "agent_settled"},
    ]

    def test_a_recorded_pi_stream(self):
        """pi 0.85.1's real stream from hand test H3, reduced to the fields the parser reads.

        The response to that run reported 12331 input and 2526 output tokens.
        """
        path = Path(__file__).parent / "fixtures" / "sys1" / "pi-0.85.1-read-only-events.jsonl"
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        calls = [info for info in map(digest, events) if info.is_model_call]
        assert len(calls) == 3
        assert sum(info.input_tokens for info in calls) == 12331
        assert sum(info.output_tokens for info in calls) == 2526
        assert {info.model for info in calls} == {"zai/glm-5.3-flash"}
        assert digest(events[-1]).settled

    def test_usage_and_model_from_a_pi_stream(self):
        digests = [digest(event) for event in self.STREAM]
        calls = [info for info in digests if info.is_model_call]
        assert len(calls) == 2
        assert sum(info.input_tokens for info in calls) == 5200 + 1100 + 5100
        assert sum(info.output_tokens for info in calls) == 420
        assert {info.model for info in calls} == {"zai/glm-5.3-flash"}
        assert digests[-1].settled
