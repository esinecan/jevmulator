"""The sys1 service: what the HTTP daemon calls for everything under ``/sys1``."""

from __future__ import annotations

import logging
import secrets
import threading
from pathlib import Path
from typing import Any

from .. import CONTRACT_PIN_DATE, __version__, errors, wire
from ..config import Config
from . import form
from .harness import build_harness
from .profiles import DEFAULT_ALIASES, Profile, ProfileCatalogue, load_profiles
from .runs import (
    RUN_ID_PATTERN,
    Outcome,
    Registry,
    Run,
    RunSettings,
    RunState,
    fingerprint,
    prune,
    reconcile,
)

LOGGER = logging.getLogger("jevmulator.sys1")

SHUTDOWN_JOIN_SECONDS = 10.0


class Sys1Service:
    """Profiles, the harness, the run registry and the run records of one daemon."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.home = Path(config.sys1_home)
        self.runs_root = self.home / "runs"
        self.catalogue = ProfileCatalogue(
            load_profiles(),
            default=config.sys1_default_profile,
            allow_shell=config.sys1_allow_shell,
            accept_unknown=config.unknown_model == "accept",
        )
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.abandoned_at_startup = reconcile(self.runs_root)
        prune(self.runs_root, config.sys1_keep_runs, set())

        self.harness = build_harness(config)
        self.problems = self.harness.problems()
        self.settings = RunSettings(
            home=self.home,
            max_submissions=config.sys1_max_submissions,
            max_sum_error=config.sys1_max_sum_error,
            normalize=config.normalize_probabilities,
            tolerance=config.probability_tolerance,
            usage_policy=config.usage_policy,
            run_timeout_seconds=config.sys1_run_timeout_seconds,
            stall_seconds=config.sys1_stall_seconds,
            hello_seconds=config.sys1_hello_seconds,
            exit_grace_seconds=config.sys1_exit_grace_seconds,
            max_nudges=config.sys1_max_nudges,
        )
        self.registry = Registry(
            max_runs=config.sys1_max_concurrent_runs,
            result_ttl_seconds=config.sys1_result_ttl_seconds,
            failure_ttl_seconds=config.sys1_failure_ttl_seconds,
        )
        self.base_url = f"http://127.0.0.1:{config.port}"
        self._lock = threading.Lock()
        self._supervisors: dict[str, tuple[Run, threading.Thread]] = {}
        self._metrics = {
            "runs_started": 0,
            "runs_attached": 0,
            "runs_replayed": 0,
            "runs_by_state": {state.value: 0 for state in RunState},
        }

    # -- wiring ------------------------------------------------------------------

    def set_address(self, host: str, port: int) -> None:
        """Point the form URLs at the bound socket. The test daemon binds port 0."""
        if host in ("", "0.0.0.0", "::"):
            host = "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        self.base_url = f"http://{host}:{port}"

    # -- models ------------------------------------------------------------------

    def model_catalogue(self) -> list[dict[str, str]]:
        entries = []
        default = self.catalogue.default
        for alias in DEFAULT_ALIASES:
            entries.append(
                {
                    "name": alias,
                    "description": (
                        f"Alias for the default sys1 profile, {default.name}. "
                        + self._backend(default)
                    ),
                    "release_date": CONTRACT_PIN_DATE,
                }
            )
        for profile in self.catalogue.enabled():
            entries.append(
                {
                    "name": profile.model_name,
                    "description": f"sys1 profile {profile.name}: {profile.description} "
                    + self._backend(profile),
                    "release_date": CONTRACT_PIN_DATE,
                }
            )
        return entries

    def _backend(self, profile: Profile) -> str:
        return (
            f"Answered by an agent run in the {self.harness.name} harness on "
            f"{self.harness.model_id(profile)}, through Jevmulator {__version__}, not by "
            "TypeSafe weights."
        )

    def resolve_profile(self, model: str) -> Profile:
        profile = self.catalogue.resolve(model)
        if profile is not None:
            return profile
        if self.catalogue.is_shell_gated(model):
            message = (
                f"The sys1 profile {model} can write files or run commands, so it is off "
                "until the daemon runs with JEVMULATOR_SYS1_ALLOW_SHELL=1."
            )
        else:
            message = (
                "Unknown model. Available names are returned by GET /sys1/v1/models: "
                + ", ".join(self.catalogue.accepted_names())
            )
        raise errors.RequestValidationError(
            [errors.validation_detail(["body", "model"], message, "model_not_found", input_=model)]
        )

    # -- evaluation ----------------------------------------------------------------

    def evaluate(
        self,
        payload: dict[str, Any],
        request: wire.SystemOneRequest,
        profile: Profile,
        retry_count: str | None,
    ) -> tuple[Outcome, str, str]:
        """Run, join or replay the agent run for one request.

        Returns the outcome, the run id and the coalescing kind.
        """
        aliased = form.alias_request(request)
        blocked = form.unanswerable_aliases(aliased)
        if blocked:
            raise errors.Sys1UnanswerableError(
                "No distribution can answer a choice question with zero options, so no "
                f"run was started. {len(blocked)} question(s) have no options."
            )
        if self.problems:
            raise errors.Sys1HarnessNotConfiguredError(
                "The sys1 harness cannot start: " + " ".join(self.problems)
            )

        key = fingerprint(profile.name, payload)

        def factory() -> Run:
            return Run(
                run_id=secrets.token_hex(16),
                token=secrets.token_urlsafe(32),
                fingerprint=key,
                profile=profile,
                request=request,
                aliased=aliased,
                harness=self.harness,
                settings=self.settings,
                base_url=self.base_url,
            )

        kind, run, replayed, run_id = self.registry.begin(key, factory)
        with self._lock:
            self._metrics[f"runs_{'started' if kind == 'new' else kind}"] += 1
        if kind == "replayed":
            assert replayed is not None
            return replayed, run_id, kind
        assert run is not None
        run.note_attachment(kind, retry_count)
        if kind == "new":
            thread = threading.Thread(
                target=run.supervise,
                args=(self.registry.decided, self._finished),
                name=f"sys1-run-{run.run_id[:8]}",
                daemon=True,
            )
            with self._lock:
                self._supervisors[run.run_id] = (run, thread)
            thread.start()

        if not run.done.wait(timeout=run.handler_wait_seconds()):
            error = errors.Sys1TimeoutError(
                f"sys1 run {run.run_id} published no outcome in time. The run record has the details."
            )
            return Outcome(error.status, error.body(), dict(error.headers), RunState.TIMED_OUT), run.run_id, kind
        assert run.outcome is not None
        return run.outcome, run.run_id, kind

    def _finished(self, run: Run) -> None:
        """A run's job is closed and its final record is written."""
        with self._lock:
            self._supervisors.pop(run.run_id, None)
            self._metrics["runs_by_state"][run.state.value] += 1
            live = set(self._supervisors)
        prune(self.runs_root, self.config.sys1_keep_runs, live)

    # -- what the harness calls --------------------------------------------------------

    def authorized_run(self, run_id: str, token: str) -> Run:
        if not RUN_ID_PATTERN.match(run_id):
            raise errors.NotFoundError("No such sys1 run.")
        run = self.registry.find(run_id)
        if run is None:
            raise errors.NotFoundError("No such sys1 run.")
        if not token or not run.check_token(token):
            raise errors.UnauthorizedError("Invalid or missing run token.")
        return run

    # -- lifecycle ----------------------------------------------------------------------

    def close(self) -> None:
        """End every live run: mark it cancelled, which wakes its handlers, and close its job.

        A run that already has its verdict stops waiting for its harness to exit, so a
        shutdown never sits out an exit grace.
        """
        undecided = self.registry.close()
        for run in undecided:
            run.transition(RunState.CANCELLED, "the daemon is shutting down")
        with self._lock:
            supervisors = list(self._supervisors.values())
        for run, _thread in supervisors:
            run.stop_waiting.set()
        for _run, thread in supervisors:
            thread.join(timeout=SHUTDOWN_JOIN_SECONDS)
        for run in undecided:
            if not run.done.is_set():
                run._close_job()  # noqa: SLF001 - the supervisor did not finish in time
                run._publish(run._error_outcome())  # noqa: SLF001

    # -- operational ---------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        return {
            "ready": not self.problems,
            "harness": self.harness.name,
            "problems": list(self.problems),
            "default_profile": self.catalogue.default.name,
            "profiles": [profile.name for profile in self.catalogue.enabled()],
            "home": str(self.home),
        }

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            snapshot = {
                "runs_started": self._metrics["runs_started"],
                "runs_attached": self._metrics["runs_attached"],
                "runs_replayed": self._metrics["runs_replayed"],
                "runs_by_state": dict(self._metrics["runs_by_state"]),
            }
        snapshot["runs_live"] = len(self.registry.inflight())
        snapshot["abandoned_at_startup"] = self.abandoned_at_startup
        return snapshot
