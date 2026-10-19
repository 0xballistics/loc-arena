"""The agents service as a stack run's live service: Meridian's agent registry, which is company code.

A stack run starts it as the scenario's ``agents`` entry: ``python -m loc_arena.agents_service <run config>
<entry name>``. It serves ``meridian_controlplane.agents`` with every agent's identity mounted in the secrets
directory and the starting rights the run config gives each agent, runs executions on the runners the entry
declares (``loc_arena.runner_pool``), and writes the audit record to stdout.
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass

import uvicorn
from pydantic import TypeAdapter
from sandbox_server.server import SandboxToken
from sandbox_server.wire import CommandRequest

from loc_arena.config import RunConfig, load_run_config
from loc_arena.iam_wiring import guard_from_environment, load_iam_authority
from loc_arena.mcp_service import ALL_ADDRESSES, read_identities
from loc_arena.runner_pool import RUNNER_WORKSPACE, list_runner_services, runner_token_secret
from loc_arena.sandbox import SandboxClient, SandboxError
from meridian_controlplane.agents import (
    AccessControl,
    AgentRegistry,
    Executions,
    RunnerResult,
    create_agents_service_app,
    load_seeded_agents,
)

AUDIT_LOCK = threading.Lock()  # request handlers and the execution workers each write whole lines


@dataclass(frozen=True)
class RunnerClient:
    """One runner of the pool, through its command server, as ``meridian_controlplane.agents.Runner``."""

    name: str
    sandbox: SandboxClient

    def run(self, argv: list[str], timeout_seconds: float) -> RunnerResult:
        """Run ``argv`` in the runner's workspace; with no exit code, stderr says why: not run, or killed."""
        command = CommandRequest(argv=argv, directory=RUNNER_WORKSPACE, timeout_seconds=timeout_seconds)
        try:
            result = self.sandbox.run(command)
        except SandboxError as error:
            return RunnerResult(returncode=None, stdout="", stderr=str(error))
        killed = "" if result.returncode is not None else f"killed at its {timeout_seconds:g} s time limit\n"
        return RunnerResult(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr + killed)

    def reset(self) -> None:
        """End what runs on the runner and empty its workspace and home."""
        self.sandbox.reset(RUNNER_WORKSPACE / "next")


def connect_runners(run_config: RunConfig) -> list[RunnerClient]:
    """A client of each runner of the pool, holding that runner's token from the secrets directory."""
    settings = run_config.settings
    read_token = TypeAdapter(SandboxToken).validate_python
    return [
        RunnerClient(
            runner,
            SandboxClient(
                f"http://{runner}:{settings.stack.sandbox_port}",
                read_token((settings.gateway.secrets_dir / runner_token_secret(runner)).read_text().strip()),
                settings.stack.sandbox_response_grace_seconds,
                settings.stack.sandbox_recovery_seconds,
                settings.stack.gateway_health_interval_seconds,
            ),
        )
        for runner in list_runner_services(run_config)
    ]


def write_audit_line(line: str) -> None:
    """One line of the audit record on stdout, never interleaved with another."""
    with AUDIT_LOCK:
        print(line, flush=True)


def main(arguments: list[str]) -> None:
    """Serve entry ``arguments[1]`` of the run config at ``arguments[0]``, once an identity is mounted."""
    run_config = load_run_config(arguments[0])
    service_entry = next(service for service in run_config.live_services if service.name == arguments[1])
    registry = AgentRegistry(load_seeded_agents())
    executions = Executions(
        registry,
        connect_runners(run_config),
        run_config.settings.stack.runner_timeout_seconds,
        write_audit_line,
    )
    guard = guard_from_environment(os.environ)
    if guard is not None:  # central IAM decides every call: no identity or rights table is kept here
        app = create_agents_service_app(
            registry,
            None,
            write_audit_line,
            authority=load_iam_authority(guard),
            executions=executions,
        )
        uvicorn.run(app, host=ALL_ADDRESSES, port=service_entry.port, log_level="warning", access_log=False)
        return
    identities_by_agent = read_identities(run_config.settings.gateway.secrets_dir)
    if not identities_by_agent:
        raise SystemExit(f"no identity in {run_config.settings.gateway.secrets_dir}")
    access_control = AccessControl(
        identities_by_caller=identities_by_agent,
        declared_rights=service_entry.rights,
        starting_rights_by_caller={
            agent.id: agent.sandbox.rights.get(service_entry.name, ()) for agent in run_config.agents
        },
        grant_right_is_grantable=service_entry.transitive,
    )
    app = create_agents_service_app(registry, access_control, write_audit_line, executions=executions)
    uvicorn.run(app, host=ALL_ADDRESSES, port=service_entry.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main(sys.argv[1:])
