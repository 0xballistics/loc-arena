"""The job service as a stack run's live service: Meridian's cluster, which is company code.

A stack run starts it as the scenario's ``jobsvc`` entry: ``python -m loc_arena.jobsvc_service <run config>
<entry name>``. It serves ``meridian_jobsvc`` under central IAM, which decides every call over its HTTP API
and its MCP tools and issues each job a workload of its own, runs each job as a real process on a node of the
fleet (``loc_arena.node_pool``) through that node's daemon, keeps a pass over the fleet going in the
background, and writes its record of requests and of each job's state to stdout.
"""

from __future__ import annotations

import os
import secrets
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx2
import uvicorn
from pydantic import SecretStr
from starlette.applications import Starlette

from loc_arena.config import RunConfig, load_run_config
from loc_arena.iam_seed import load_seed
from loc_arena.iam_wiring import build_service_app, guard_from_environment, http_dependency, iam_options
from loc_arena.mcp_service import ALL_ADDRESSES, caller_from_token
from loc_arena.node_pool import list_node_services, node_token_secret
from loc_arena.task import resolve_scenario
from loc_arena.tool_records import tool_call_recorder
from meridian_iam.client import IamClient
from meridian_iam.contracts import IamError, Unavailable
from meridian_jobsvc.api import SERVICE, build_jobsvc_server, create_jobsvc_api
from meridian_jobsvc.authority import JobIdentity
from meridian_jobsvc.errors import CredentialRefused, CredentialUnavailable
from meridian_jobsvc.fleet import NodeUnavailable, ProcessOutput, ProcessStatus
from meridian_jobsvc.service import JobService
from meridian_jobsvc.types import Node

RECORD_LOCK = threading.Lock()  # request handlers, the MCP recorder and the fleet pass each write whole lines
LAUNCH_OPERATION = "submit_job"  # the manifest's launch operation: a job's workload is issued through it
SECRET_BYTES = 32  # the entropy of each secret a job's workload holds


@dataclass(frozen=True)
class NodeDaemonClient:
    """One node of the fleet, through its daemon over HTTP, as ``meridian_jobsvc.fleet.NodeDaemon``.

    It holds the node's token and calls the daemon the way the episode calls a sandbox; a daemon that does
    not answer is a ``NodeUnavailable``, which the job service passes over until its next pass.
    """

    node: Node
    url: str
    token: str
    timeout_seconds: float

    def start(
        self,
        job_id: str,
        command: list[str],
        owner: str,
        environment: Mapping[str, SecretStr],
    ) -> None:
        """Start ``owner``'s job ``job_id`` as the node's process, with ``environment``: its credentials."""
        revealed = {name: secret.get_secret_value() for name, secret in environment.items()}
        self._post("/jobs", {"job_id": job_id, "argv": command, "owner": owner, "environment": revealed})

    def status(self) -> ProcessStatus:
        """The node's job and how its process is."""
        return ProcessStatus.model_validate_json(self._get("/status"))

    def output(self) -> ProcessOutput:
        """The tails of the node's job's stdout and stderr."""
        return ProcessOutput.model_validate_json(self._get("/output"))

    def stop(self) -> None:
        """End the node's job's process."""
        self._post("/stop", None)

    def reset(self) -> None:
        """End whatever runs on the node and empty its workspace."""
        self._post("/reset", None)

    def _get(self, path: str) -> bytes:
        return self._send("GET", path, None)

    def _post(self, path: str, body: dict[str, object] | None) -> bytes:
        return self._send("POST", path, body)

    def _send(self, method: str, path: str, body: dict[str, object] | None) -> bytes:
        try:
            response = httpx2.request(
                method,
                f"{self.url}{path}",
                json=body,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except httpx2.HTTPError as error:
            raise NodeUnavailable(
                f"{self.node.node_id} did not answer: {type(error).__name__}: {error}",
            ) from error
        return response.content


def connect_nodes(run_config: RunConfig) -> list[NodeDaemonClient]:
    """A client of each node of the fleet, holding that node's token from the secrets directory."""
    settings = run_config.settings
    return [
        NodeDaemonClient(
            node=Node(
                node_id=node,
                cpu_capacity=settings.stack.node_cpus,
                memory_mb=settings.stack.node_memory_mb,
            ),
            url=f"http://{node}:{settings.stack.sandbox_port}",
            token=(settings.gateway.secrets_dir / node_token_secret(node)).read_text().strip(),
            timeout_seconds=settings.stack.sandbox_response_grace_seconds,
        )
        for node in list_node_services(run_config)
    ]


@dataclass(frozen=True)
class IamWorkloadIssuer:
    """``meridian_jobsvc.authority.WorkloadIssuer`` through central IAM: each job's workload.

    IAM says what a job may be given of what its submitter holds now (the scenario's delegation for the job
    service, as IAM was seeded with it), and issues exactly that, through the launch operation, under the
    job's id as reference and key and its name as purpose. Its lifetime is ``ttl_seconds``, cut to the
    shortest ``max_seconds`` of a service it gets a right on, as IAM requires. Each service's audience is its
    name, so the workload gets one new secret per service, which only the job's process is handed.
    """

    client: IamClient
    ttl_seconds: int
    max_seconds: Mapping[str, int]  # each managed service's longest grant

    def issue(self, credential: str | None, job_id: str, purpose: str) -> JobIdentity:
        """Job ``job_id``'s workload, from its submitter's ``credential``; IAM's refusal is the job's."""
        try:
            grants = self.client.delegable(credential, LAUNCH_OPERATION)
            if not grants:
                raise CredentialRefused("nothing you hold may be given to a job", path=job_id)
            issued = {service: secrets.token_urlsafe(SECRET_BYTES) for service in grants}
            workload = self.client.issue_workload(
                credential,
                LAUNCH_OPERATION,
                reference=job_id,
                purpose=purpose,
                grants=grants,
                ttl=min(self.ttl_seconds, *(self.max_seconds[service] for service in grants)),
                credentials=issued,
                key=job_id,
            )
        except Unavailable as error:
            raise CredentialUnavailable(
                f"IAM did not issue the job a credential: {error}",
                path=job_id,
            ) from error
        except IamError as error:
            raise CredentialRefused(f"IAM refused the job a credential: {error}", path=job_id) from error
        secrets_by_service = {service: SecretStr(secret) for service, secret in issued.items()}
        return JobIdentity(principal=workload.principal, secrets=secrets_by_service)


def connect_issuer(run_config: RunConfig, client: IamClient) -> IamWorkloadIssuer:
    """The issuer of each job's workload through ``client``: the run's lifetime and the seed's limits."""
    seed = load_seed(resolve_scenario(run_config))
    resources = {} if seed is None else seed.resources
    return IamWorkloadIssuer(
        client,
        run_config.settings.stack.job_credential_seconds,
        {name: resource.max_seconds for name, resource in resources.items()},
    )


def write_record(line: str) -> None:
    """One line of the service's record on stdout, never interleaved with another."""
    with RECORD_LOCK:
        print(line, flush=True)


class IamJobsvcAuthority:
    """``meridian_jobsvc.authority.CentralAuthority`` backed by one IAM guard over the jobsvc manifest.

    HTTP goes through the guard's ASGI dependency; MCP through its verifier and, inside the call recorder, its
    tool authorizer, so a refused tool call is still recorded.
    """

    def __init__(self, guard: Any, write: Callable[[str], None]) -> None:  # noqa: ANN401 - a meridian_iam Guard
        """Decide every call through ``guard``; ``write`` records each tool call, around the decision."""
        options = iam_options(guard, SERVICE)
        self.dependencies = [http_dependency(guard)]
        self.verifier = options["verifier"]
        self.middleware = (
            tool_call_recorder(caller_from_token, write, options["annotate"]),
            *options["extra_middleware"],
        )

    def caller(self) -> str | None:
        """The caller of the tool call being served: the principal its credential names."""
        return caller_from_token()


def serving_app(service: JobService, guard: Any, write: Callable[[str], None]) -> Starlette:  # noqa: ANN401
    """The MCP tools at ``/mcp`` and the HTTP API beside them, over one ``service`` and one IAM guard.

    It starts only if its routes and tools together are exactly the manifest's operations, and ``write``
    writes its record of every request and tool call.
    """
    authority = IamJobsvcAuthority(guard, write)
    api = create_jobsvc_api(service, authority, write)
    return build_service_app(guard, build_jobsvc_server(service, authority), SERVICE, api)


def main(arguments: list[str]) -> None:
    """Serve entry ``arguments[1]`` of the run config at ``arguments[0]``, under central IAM."""
    run_config = load_run_config(arguments[0])
    service_entry = next(service for service in run_config.live_services if service.name == arguments[1])
    guard = guard_from_environment(os.environ)  # IAM decides every call: no identity or rights table here
    service = JobService(connect_nodes(run_config), write_record, connect_issuer(run_config, guard.client))
    threading.Thread(
        target=service.run,
        args=(run_config.settings.stack.job_poll_seconds,),
        daemon=True,
    ).start()
    uvicorn.run(
        serving_app(service, guard, write_record),
        host=ALL_ADDRESSES,
        port=service_entry.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main(sys.argv[1:])
