"""A stack run's flow over compose: each play, what is copied out and when, and the twin's renewal.

Compose is a fake that records each call; the run directory, the logs and the merged main are real files.
"""

from __future__ import annotations

import io
import socket
import subprocess
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

import pytest
from loc_arena import episode_stack
from loc_arena.config import load_run_config
from loc_arena.episode_stack import StackError, run_in_stack
from loc_arena.stack_play import renew_services
from loc_arena.tasks.main_task_grader import COMPANY_REPOS

SCRIPTED_CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
COMPOSE_PREFIX_LENGTH = 8  # docker compose -p <project> -f <file> --project-directory <repository>
NOTES_LOG = b'{"caller": "agent-main", "status": 200}\n'


def archive_merged_file() -> bytes:
    """What ``git archive`` gives of a repo whose main holds one file, ``MERGED.md``."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        entry = tarfile.TarInfo("MERGED.md")
        entry.size = len(b"merged\n")
        tar.addfile(entry, io.BytesIO(b"merged\n"))
    return buffer.getvalue()


MAIN_ARCHIVE = archive_merged_file()
AURORA_AGENTS = ["AGENT_MAIN", "CONTROLPLANE_AGENT", "DATAPIPE_AGENT", "DISTILL_AGENT", "EVAL_AGENT"]
AURORA_AGENTS += ["JOBSVC_AGENT", "SERVING_AGENT"]
# A stack run's services in the order its compose file declares them; the fleet and the tools gateway follow.
SERVICES = tuple("notes forge board jobsvc jobs scratch coverage agents evalkit tickets iam".split())


@dataclass
class FakeDocker:
    """``subprocess.run`` for the compose calls of a stack run whose last play exits 1: nothing is graded."""

    failing: str = "no subcommand"  # the compose subcommand or option whose call fails
    episode_created_only: bool = False  # a live service never became healthy, so the episode never started
    plays_passing: int = 0  # how many plays of the episode exit 0 before one exits 1
    unwritable_after_first_play: Path | None = None  # logs whose run's notes.log is a directory once played
    calls: list[list[str]] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    published: str = ""  # where the gateway's port is published: a socket the test listens on

    def __call__(
        self,
        command: list[str],
        *,
        env: dict[str, str] | None = None,  # none on the host's own call, for the gateway's published port
        stdout: IO[bytes] | None = None,
        text: bool = False,
        **_: object,
    ) -> subprocess.CompletedProcess[Any]:
        self.calls.append(command)
        self.environment = env or self.environment
        verb = command[COMPOSE_PREFIX_LENGTH]
        if verb == "logs" and stdout is not None:
            stdout.write(NOTES_LOG)
        playing = verb == "up" and "--attach" in command
        if playing and self.unwritable_after_first_play and self.plays == 1:
            (services,) = self.unwritable_after_first_play.glob("*/*/services")
            (services / "notes.log").mkdir()
        failed = self.failing in command[COMPOSE_PREFIX_LENGTH:] or (
            playing and self.plays > self.plays_passing
        )
        created = "container-id\n" if verb == "ps" and self.episode_created_only else ""
        published = self.published if verb == "port" else created
        printed = NOTES_LOG if verb == "logs" and stdout is None else published
        printed = MAIN_ARCHIVE if verb == "exec" else printed  # the forge's git archive of a repo's main
        printed = printed.decode() if text and isinstance(printed, bytes) else printed
        return subprocess.CompletedProcess(command, int(failed), stdout=printed)

    @property
    def plays(self) -> int:
        return sum(call[COMPOSE_PREFIX_LENGTH] == "up" and "--attach" in call for call in self.calls)

    @property
    def verbs(self) -> list[str]:
        return [call[COMPOSE_PREFIX_LENGTH] for call in self.calls]


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDocker]:
    with socket.create_server(("127.0.0.1", 0)) as gateway:
        fake = FakeDocker(published=f"127.0.0.1:{gateway.getsockname()[1]}")
        monkeypatch.setattr(subprocess, "run", fake)
        yield fake


def run_aurora_in_stack(logs: Path, mode: str = "honest") -> None:
    run_in_stack("aurora-efficiency", mode=mode, seed=None, robust=False, logs=logs)


def read_copied_log(logs: Path, service: str) -> bytes:
    """The log of ``service`` a stack run copied into its run directory under ``logs``."""
    (copied,) = logs.glob(f"*/*/services/{service}.log")
    return copied.read_bytes()


def test_a_stack_run_hands_compose_a_distinct_identity_per_agent_and_service_through_its_environment_alone(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    with pytest.raises(StackError, match="exited with code 1"):
        run_aurora_in_stack(tmp_path)

    identities = {name: value for name, value in docker.environment.items() if "_IDENTITY_" in name}
    written = "".join(path.read_text() for path in (tmp_path / "compose").iterdir()) + str(docker.calls)
    assert (
        sorted(identities),
        len(set(identities.values())),
        [v for v in identities.values() if v in written],
    ) == (
        sorted(f"LOC_ARENA_IDENTITY_{s.upper()}_{a}" for s in SERVICES for a in AURORA_AGENTS),
        len(SERVICES) * len(AURORA_AGENTS),
        [],
    )


def test_a_stack_run_copies_each_live_services_log_out_before_it_removes_the_project(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    with pytest.raises(StackError, match="exited with code 1"):
        run_aurora_in_stack(tmp_path)

    copied = read_copied_log(tmp_path, "notes")
    commands = [call[COMPOSE_PREFIX_LENGTH:] for call in docker.calls]
    assert (copied, ["logs", "--no-color", "--no-log-prefix", "notes"] in commands, docker.verbs[-1]) == (
        NOTES_LOG,
        True,
        "down",
    )


def test_a_live_services_log_that_cannot_be_copied_keeps_the_project(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.failing = "notes"  # the live service's log alone, not the gateway's

    with pytest.raises(StackError, match="kept compose project"):
        run_aurora_in_stack(tmp_path)

    assert (docker.verbs[-1], "down" in docker.verbs) == ("stop", False)


def test_a_stack_run_whose_episode_never_started_copies_each_live_services_log_before_it_removes_the_project(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.episode_created_only = True

    with pytest.raises(StackError, match="never started"):
        run_aurora_in_stack(tmp_path)

    copied = read_copied_log(tmp_path, "notes")
    commands = [call[COMPOSE_PREFIX_LENGTH:] for call in docker.calls]
    assert (copied, ["logs", "--no-color", "--no-log-prefix", "notes"] in commands, docker.verbs[-1]) == (
        NOTES_LOG,
        True,
        "down",
    )


def test_a_stack_run_whose_episode_never_started_says_a_live_service_may_have_failed_to_become_healthy(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.episode_created_only = True

    with pytest.raises(StackError) as raised:
        run_aurora_in_stack(tmp_path)

    (services,) = tmp_path.glob("*/*/services")
    assert "live service" in str(raised.value) and str(services) in str(raised.value)


def test_a_live_services_log_that_cannot_be_copied_keeps_the_project_though_the_episode_never_started(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.episode_created_only = True
    docker.failing = "logs"

    with pytest.raises(StackError, match="kept compose project"):
        run_aurora_in_stack(tmp_path)

    assert (docker.verbs[-1], "down" in docker.verbs) == ("stop", False)


def test_a_stack_run_hands_the_tools_gateway_its_config_through_its_environment_alone_without_a_token(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    with pytest.raises(StackError, match="exited with code 1"):
        run_aurora_in_stack(tmp_path)

    config = docker.environment["LOC_ARENA_AGENTGATEWAY_CONFIG"]
    tokens = [
        value for name, value in docker.environment.items() if name.startswith("LOC_ARENA_SANDBOX_TOKEN_")
    ]
    written = "".join(path.read_text() for path in (tmp_path / "compose").iterdir()) + str(docker.calls)
    assert (len(tokens), [token for token in tokens if token in config], config in written) == (7, [], False)


def test_a_stack_run_copies_the_tools_gateways_log_out_before_it_removes_the_project(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    with pytest.raises(StackError, match="exited with code 1"):
        run_aurora_in_stack(tmp_path)

    copied = read_copied_log(tmp_path, "agentgateway")
    commands = [call[COMPOSE_PREFIX_LENGTH:] for call in docker.calls]
    assert (
        copied,
        ["logs", "--no-color", "--no-log-prefix", "agentgateway"] in commands,
        docker.verbs[-1],
    ) == (
        NOTES_LOG,
        True,
        "down",
    )


@pytest.fixture
def copied_at_grading(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The run directory's service logs as its events are built and as it is graded, in place of both."""
    seen: list[list[str]] = []

    def list_copied_logs(config: object, run_directory: Path, **_: object) -> Path:
        seen.append(sorted(path.name for path in (run_directory / "services").iterdir()))
        return run_directory

    monkeypatch.setattr(episode_stack, "build_recorded_run_events", list_copied_logs)
    monkeypatch.setattr(episode_stack, "grade_run", list_copied_logs)
    return seen


def test_a_stack_run_copies_each_live_services_log_into_its_run_directory_before_it_builds_and_grades(
    docker: FakeDocker,
    tmp_path: Path,
    copied_at_grading: list[list[str]],
) -> None:
    docker.plays_passing = 1

    run_aurora_in_stack(tmp_path)

    logged = sorted((*SERVICES, "node-1", "node-2", "node-3", "agentgateway"))
    assert copied_at_grading == [[f"{service}.log" for service in logged]] * 2


def test_a_stack_run_grades_its_play_on_what_it_merged_on_the_forges_main(
    docker: FakeDocker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker.plays_passing = 1
    graded: dict[str, list[str]] = {}

    def list_graded_checkout(config: object, run_directory: Path, **_: object) -> Path:
        checkout = run_directory / "episode" / "checkout"
        graded.update(
            {repo.name: sorted(path.name for path in repo.iterdir()) for repo in checkout.iterdir()},
        )
        return run_directory

    monkeypatch.setattr(episode_stack, "build_recorded_run_events", lambda *_, **__: None)
    monkeypatch.setattr(episode_stack, "grade_run", list_graded_checkout)

    run_aurora_in_stack(tmp_path)

    assert graded == dict.fromkeys(COMPANY_REPOS, ["MERGED.md"])


def test_a_graded_stack_run_copies_its_run_directory_out_once(
    docker: FakeDocker,
    tmp_path: Path,
    copied_at_grading: list[list[str]],
) -> None:
    docker.plays_passing = 1

    run_aurora_in_stack(tmp_path)

    outputs = [
        call for call in docker.calls if call[COMPOSE_PREFIX_LENGTH] == "cp" and call[-1] == str(tmp_path)
    ]
    assert len(outputs) == 1


def test_an_attack_stack_run_drains_the_record_then_renews_every_container_but_the_episode_for_the_twin(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.plays_passing = 1

    with pytest.raises(StackError, match="exited with code 1"):
        run_aurora_in_stack(tmp_path, mode="attack")

    commands = [call[COMPOSE_PREFIX_LENGTH:] for call in docker.calls]
    plays = [index for index, command in enumerate(commands) if command[0] == "up" and "--attach" in command]
    logged = (*SERVICES, "node-1", "node-2", "node-3", "agentgateway")  # the fleet's logs: its process facts
    between = [
        command[:2] if command[0] == "cp" else command for command in commands[plays[0] + 1 : plays[1]]
    ]
    renewed = ["sandbox-agent-main", "sandbox-serving-agent", "sandbox-distill-agent", "sandbox-eval-agent"]
    renewed += ["sandbox-datapipe-agent", "sandbox-jobsvc-agent", "sandbox-controlplane-agent"]
    renewed += ["runner-1", "runner-2", "runner-3", *logged]
    archived = (
        [
            "exec",
            "-T",
            "forge",
            "git",
            "--git-dir",
            f"/var/lib/forge/{repo}.git",
            "archive",
            "--format=tar",
            "main",
        ]
        for repo in COMPANY_REPOS
    )
    assert between == [
        *archived,  # what the episode merged, read before the forge is renewed
        ["stop", *renewed],
        ["logs", "--no-color", "--no-log-prefix", "gateway"],
        ["cp", "collector:/sealed/calls.jsonl"],
        *(["logs", "--no-color", "--no-log-prefix", service] for service in logged),
        [
            "up",
            "--detach",
            "--wait",
            "--force-recreate",
            "--renew-anon-volumes",
            "--no-deps",
            "gateway",
            *renewed,
        ],
        ["up", "--detach", "--wait", "gateway"],
        ["port", "gateway", "8080"],
    ]


def test_a_stack_runs_service_log_holds_the_lines_of_the_episodes_container_and_then_the_honest_twins(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.plays_passing = 1

    with pytest.raises(StackError, match="exited with code 1"):
        run_aurora_in_stack(tmp_path, mode="attack")

    assert read_copied_log(tmp_path, "notes") == NOTES_LOG * 2


def test_a_log_that_cannot_be_copied_before_the_honest_twin_keeps_the_project_and_renews_nothing(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.plays_passing = 1
    docker.failing = "notes"

    with pytest.raises(StackError, match="kept compose project"):
        run_aurora_in_stack(tmp_path, mode="attack")

    renewed = [call for call in docker.calls if "--force-recreate" in call]
    assert (renewed, docker.plays, docker.verbs[-1]) == ([], 1, "stop")


def test_containers_that_do_not_come_back_for_the_honest_twin_stop_the_run_with_every_log_copied_out(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.plays_passing = 1
    docker.failing = "--force-recreate"

    with pytest.raises(StackError, match="did not come back for the honest twin"):
        run_aurora_in_stack(tmp_path, mode="attack")

    copied = read_copied_log(tmp_path, "notes")
    assert (copied, docker.plays, docker.verbs[-1]) == (NOTES_LOG * 2, 1, "down")


def test_the_honest_twin_plays_in_the_episodes_container_though_its_image_tag_moved(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.plays_passing = 1

    with pytest.raises(StackError, match="exited with code 1"):
        run_aurora_in_stack(tmp_path, mode="attack")

    plays = [call for call in docker.calls if "--attach" in call]
    assert ["--no-recreate" in play for play in plays] == [True, True]


def test_a_log_file_that_cannot_be_written_before_the_honest_twin_renews_nothing_and_writes_no_log(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    (tmp_path / "notes.log").mkdir()
    compose = ["docker", "compose", "-p", "locarena-test", "-f", "compose.yaml", "--project-directory", "."]

    with pytest.raises(StackError, match="nothing was renewed"):
        renew_services(SCRIPTED_CONFIG, compose, tmp_path, {}, {})

    renewed = [call for call in docker.calls if "--force-recreate" in call]
    assert (renewed, sorted(path.name for path in tmp_path.iterdir())) == ([], ["notes.log"])


def test_a_log_file_that_cannot_be_written_at_teardown_keeps_the_project_after_every_other_copy(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.unwritable_after_first_play = tmp_path

    with pytest.raises(StackError, match="kept compose project"):
        run_aurora_in_stack(tmp_path)

    copied = read_copied_log(tmp_path, "agentgateway")
    assert (copied, docker.verbs[-1], "down" in docker.verbs) == (NOTES_LOG, "stop", False)


def test_a_service_log_copied_before_a_failed_grading_is_not_added_again_at_teardown(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.plays_passing = 1
    docker.failing = "agentgateway"  # its copy fails, so the run stops before grading

    with pytest.raises(StackError, match="kept compose project"):
        run_aurora_in_stack(tmp_path)

    assert read_copied_log(tmp_path, "notes") == NOTES_LOG
