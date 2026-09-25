"""sys1 profiles: the built-ins, validation, shell gating and model-name resolution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jevmulator.sys1.profiles import (
    ProfileCatalogue,
    ProfileError,
    load_profiles,
    parse_profile,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

GOOD = {
    "name": "custom",
    "description": "A test profile.",
    "tools": ["read", "grep"],
    "brief": "brief.md",
    "workdir": "scratch",
    "read_roots": None,
    "timeout_seconds": 60,
    "harness": {"pi": {"provider": "zai", "model": "glm-5.3-flash", "thinking": "low"}},
}


@pytest.fixture
def brief_dir(tmp_path: Path) -> Path:
    (tmp_path / "brief.md").write_text("brief $workdir", encoding="utf-8")
    return tmp_path


def parse(brief_dir: Path, **changes):
    data = {**GOOD, **changes}
    return parse_profile(data, brief_dir=brief_dir, source="test.json")


class TestBuiltins:
    def test_both_builtins_load(self):
        profiles = load_profiles()
        assert set(profiles) == {"read-only", "prototype-first"}
        assert profiles["read-only"].tools == ("read", "grep", "find", "ls")
        assert not profiles["read-only"].is_shell
        assert profiles["prototype-first"].is_shell
        for profile in profiles.values():
            assert profile.harness_settings("pi") == {
                "provider": "zai", "model": "glm-5.3-flash", "thinking": "high",
            }


class TestValidation:
    def test_good_profile(self, brief_dir):
        profile = parse(brief_dir)
        assert profile.model_name == "sys1-custom"
        assert profile.brief_template == "brief $workdir"

    @pytest.mark.parametrize(
        "changes, fragment",
        [
            ({"name": "Bad Name"}, "'name'"),
            ({"description": ""}, "'description'"),
            ({"tools": []}, "'tools'"),
            ({"tools": ["read", "browser"]}, "unknown tools"),
            ({"tools": ["read", "read"]}, "twice"),
            ({"brief": "missing.md"}, "does not exist"),
            ({"workdir": "relative/dir"}, "'workdir'"),
            ({"read_roots": ["relative"]}, "'read_roots'"),
            ({"timeout_seconds": 0}, "'timeout_seconds'"),
            ({"timeout_seconds": True}, "'timeout_seconds'"),
            ({"harness": {"pi": {"provider": "zai"}}}, "'harness.pi.model'"),
            ({"harness": {"pi": {"provider": "zai", "model": "m", "thinking": "loud"}}}, "'harness.pi.thinking'"),
        ],
    )
    def test_rejections(self, brief_dir, changes, fragment):
        with pytest.raises(ProfileError, match=fragment):
            parse(brief_dir, **changes)

    def test_a_shell_profile_must_use_scratch(self, brief_dir, tmp_path):
        with pytest.raises(ProfileError, match="must use workdir 'scratch'"):
            parse(brief_dir, tools=["read", "shell"], workdir=str(tmp_path))

    def test_a_read_only_profile_may_name_a_directory(self, brief_dir, tmp_path):
        assert parse(brief_dir, workdir=str(tmp_path)).workdir == str(tmp_path)


class TestCatalogue:
    def catalogue(self, **kwargs):
        kwargs.setdefault("default", "read-only")
        kwargs.setdefault("allow_shell", False)
        kwargs.setdefault("accept_unknown", False)
        return ProfileCatalogue(load_profiles(), **kwargs)

    def test_aliases_select_the_default(self):
        catalogue = self.catalogue()
        for alias in ("jev-latest", "jev-preview", "sys1-latest"):
            assert catalogue.resolve(alias).name == "read-only"

    def test_shell_profile_is_hidden_without_the_flag(self):
        catalogue = self.catalogue()
        assert catalogue.resolve("sys1-prototype-first") is None
        assert catalogue.is_shell_gated("sys1-prototype-first")
        assert "sys1-prototype-first" not in catalogue.accepted_names()

    def test_shell_profile_is_served_with_the_flag(self):
        catalogue = self.catalogue(allow_shell=True)
        assert catalogue.resolve("sys1-prototype-first").name == "prototype-first"
        assert "sys1-prototype-first" in catalogue.accepted_names()

    def test_unknown_names(self):
        assert self.catalogue().resolve("gpt-4o") is None
        assert self.catalogue(accept_unknown=True).resolve("gpt-4o").name == "read-only"
        assert not self.catalogue().is_shell_gated("sys1-nonexistent")

    def test_default_must_exist(self):
        with pytest.raises(ProfileError, match="not a profile"):
            self.catalogue(default="nope")

    def test_a_shell_default_needs_the_flag(self):
        with pytest.raises(ProfileError, match="JEVMULATOR_SYS1_ALLOW_SHELL=1"):
            self.catalogue(default="prototype-first")
        assert self.catalogue(default="prototype-first", allow_shell=True).default.name == "prototype-first"


def test_a_bad_default_profile_stops_serve_with_exit_2(tmp_path):
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("JEVMULATOR_")
    }
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "JEVMULATOR_PROVIDER": "fake",
            "JEVMULATOR_API_KEY": "k",
            "JEVMULATOR_SYS1_HARNESS": "fake",
            "JEVMULATOR_SYS1_HOME": str(tmp_path / "home"),
            "JEVMULATOR_SYS1_DEFAULT_PROFILE": "does-not-exist",
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "jevmulator", "serve", "--port", "0", "--state-dir", str(tmp_path / "state")],
        env=env, cwd=tmp_path, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2, result.stderr
    assert "does-not-exist" in result.stderr
