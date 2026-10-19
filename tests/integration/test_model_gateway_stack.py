"""The model gateway as a stack run renders it: agentgateway and the Collector, before a stand-in upstream.

Every call is recorded with its caller, request and reply, refused and failed ones included, and the record
is copied out once the Collector holds every call agentgateway logged. The key reaches the upstream alone.
Needs Docker and the images the stack pins; skipped otherwise.
"""

from __future__ import annotations

import functools
import hashlib
import os
import secrets
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest
import yaml
from loc_arena.gateway.model_gateway import (
    MODEL_GATEWAY_SERVICE,
    PROJECT_PREFIX,
    RUNNER_ROUTE_PREFIX,
    GatewayCall,
    RunCompose,
    copy_call_log,
    declare_gateway_secrets,
    issue_gateway_secrets,
    issue_held_tokens,
    render_gateway_services,
    start_gateway,
)
from loc_arena.settings import GatewaySettings, LocArenaSettings
from pydantic import HttpUrl

from tests.integration._docker_support import image_exists

KEY = "sk-or-the-test-key"
SANDBOX_TOKEN, RUNNER_TOKEN = "the-sandbox-token", "the-runner-token"
RUNNER_MODEL = "deepseek/deepseek-v4.1-flash"
CHAT = "/api/v1/chat/completions"
MOCK = Path(__file__).parent / "_mock_upstream.py"
CONFIGURED = LocArenaSettings(gateway=GatewaySettings(runner_calls_per_play=3, drain_timeout_seconds=15))
# A plain-HTTP stand-in for the upstream: settings refuse one, so it is set past their validation.
SETTINGS = CONFIGURED.model_copy(
    update={"gateway": CONFIGURED.gateway.model_copy(update={"upstream": HttpUrl("http://mock:9000")})},
)
IMAGES = (SETTINGS.stack.image, SETTINGS.stack.tools_gateway_image, SETTINGS.stack.collector_image)
pytestmark = pytest.mark.skipif(
    not all(map(image_exists, IMAGES)),
    reason="needs Docker and the stack's images",
)


@dataclass(frozen=True)
class Stack:
    """A running gateway: where its port is published, and how to drive its compose project."""

    url: str
    run: RunCompose
    compose: list[str]
    directory: Path


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    directory = tmp_path_factory.mktemp("model-gateway")
    services = render_gateway_services(SETTINGS, "agent-net", "egress-net")
    services["mock"] = {
        "image": SETTINGS.stack.image,
        "command": ["python", "-u", "/mock_upstream.py"],
        "volumes": [f"{MOCK}:/mock_upstream.py:ro"],
        "networks": ["egress-net"],
    }
    document = {
        "services": services,
        "networks": {"agent-net": {"internal": True}, "egress-net": {}, "record-net": {"internal": True}},
        "volumes": {"sealed": {}},
        "secrets": declare_gateway_secrets(),
    }
    (directory / "compose.yaml").write_text(yaml.safe_dump(document))
    environment = issue_gateway_secrets(
        SETTINGS,
        issue_held_tokens(16),
        {"sandbox-agent-main": SANDBOX_TOKEN},
        {"runner-1": RUNNER_TOKEN},
        "attack",
    )
    run = functools.partial(subprocess.run, env={**os.environ, **environment, "OPENROUTER_API_KEY": KEY})
    compose = [
        "docker",
        "compose",
        "-p",
        f"{PROJECT_PREFIX}{secrets.token_hex(3)}",
        "-f",
        str(directory / "compose.yaml"),
    ]
    try:
        run([*compose, "up", "--detach", "mock"], check=True, capture_output=True)
        assert start_gateway(run, compose, SETTINGS)
        published = run(
            [*compose, "port", MODEL_GATEWAY_SERVICE, str(SETTINGS.gateway.port)],
            capture_output=True,
            text=True,
        )
        yield Stack(f"http://{published.stdout.strip()}", run, compose, directory)
    finally:
        run([*compose, "down", "--volumes"], capture_output=True)


def call(
    stack: Stack,
    model: str,
    *,
    token: str | None = SANDBOX_TOKEN,
    path: str = CHAT,
    **body: object,
) -> httpx2.Response:
    headers = {"authorization": f"Bearer {token}"} if token else {}
    return httpx2.post(
        stack.url + path,
        json={"model": model, "messages": [], **body},
        headers=headers,
        timeout=60,
    )


def recorded(stack: Stack, model: str, deadline_seconds: float = 15.0) -> GatewayCall:
    """The record of the one call that asked for ``model``, copied out once the record has drained."""
    call_log = stack.directory / "locarena-test.calls.jsonl"
    deadline = time.monotonic() + deadline_seconds
    while True:
        assert copy_call_log(stack.run, stack.compose, SETTINGS, call_log)
        calls = [
            GatewayCall.model_validate_json(line)
            for line in call_log.read_text(encoding="utf-8").splitlines()
        ]
        found = [
            each for each in calls if isinstance(each.request, dict) and each.request.get("model") == model
        ]
        if found or time.monotonic() > deadline:
            (record,) = found
            return record
        time.sleep(0.5)


def test_a_call_reaches_the_upstream_with_the_key_in_place_of_the_callers_token(stack: Stack) -> None:
    reply = call(stack, "the-first-call")

    echoed = reply.json()["choices"][0]["message"]["content"]
    assert hashlib.sha256(f"Bearer {KEY}".encode()).hexdigest() in echoed


def test_a_call_is_recorded_with_its_caller_request_and_reply(stack: Stack) -> None:
    reply = call(stack, "a-recorded-call")

    record = recorded(stack, "a-recorded-call")
    assert (record.caller, record.path, record.status, record.response, record.complete) == (
        "sandbox-agent-main",
        CHAT,
        200,
        reply.json(),
        True,
    )


@pytest.mark.parametrize(
    ("model", "token", "path", "status"),
    [
        ("no-token", None, CHAT, 401),
        ("an-unknown-token", "not-a-token", CHAT, 401),
        ("a-path-outside-the-allowlist", SANDBOX_TOKEN, "/api/v1/embeddings", 403),
        ("a-runner-on-the-callers-route", RUNNER_TOKEN, CHAT, 403),
        ("a-sandbox-on-the-runners-route", SANDBOX_TOKEN, f"/{RUNNER_ROUTE_PREFIX}{CHAT}", 403),
        ("a-model-runners-may-not-call", RUNNER_TOKEN, f"/{RUNNER_ROUTE_PREFIX}{CHAT}", 403),
    ],
)
def test_a_refused_call_is_recorded_with_its_status_as_incomplete(
    stack: Stack,
    model: str,
    token: str | None,
    path: str,
    status: int,
) -> None:
    reply = call(stack, model, token=token, path=path)

    record = recorded(stack, model)
    assert (reply.status_code, record.status, record.complete) == (status, status, False)


def test_runners_make_their_calls_per_play_and_then_get_429(stack: Stack) -> None:
    statuses = [
        call(stack, RUNNER_MODEL, token=RUNNER_TOKEN, path=f"/{RUNNER_ROUTE_PREFIX}{CHAT}").status_code
        for _ in range(4)
    ]

    assert statuses == [200, 200, 200, 429]


def test_a_streamed_reply_reaches_the_caller_and_the_record_whole(stack: Stack) -> None:
    with httpx2.stream(
        "POST",
        stack.url + CHAT,
        json={"model": "a-stream", "messages": [], "stream": True},
        headers={"authorization": f"Bearer {SANDBOX_TOKEN}"},
        timeout=60,
    ) as reply:
        received = b"".join(reply.iter_bytes()).decode()

    record = recorded(stack, "a-stream")
    assert (record.response, record.complete) == (received, True)


def test_a_caller_hanging_up_mid_stream_leaves_an_incomplete_record(stack: Stack) -> None:
    with httpx2.stream(
        "POST",
        stack.url + CHAT,
        json={"model": "a-stream-slow", "messages": [], "stream": True},
        headers={"authorization": f"Bearer {SANDBOX_TOKEN}"},
        timeout=60,
    ) as reply:
        next(reply.iter_bytes())

    record = recorded(stack, "a-stream-slow")
    assert (record.status, record.complete) == (200, False)


def test_a_reply_longer_than_a_grpc_message_is_recorded_whole(stack: Stack) -> None:
    reply = call(stack, "a-long-reply", length=6_000_000)

    record = recorded(stack, "a-long-reply")
    assert record.response == reply.json()


def test_no_record_holds_the_key(stack: Stack) -> None:
    call(stack, "a-call-before-the-key-check")

    recorded(stack, "a-call-before-the-key-check")
    raw = (stack.directory / "locarena-test.calls.otlp.jsonl").read_text(encoding="utf-8")
    converted = (stack.directory / "locarena-test.calls.jsonl").read_text(encoding="utf-8")
    assert (KEY in raw, KEY in converted) == (False, False)


def test_a_call_the_upstream_never_answers_is_recorded_with_its_request(stack: Stack) -> None:
    stack.run([*stack.compose, "stop", "mock"], capture_output=True)
    try:
        reply = call(stack, "the-upstream-is-down")
    finally:
        stack.run([*stack.compose, "start", "mock"], capture_output=True)

    record = recorded(stack, "the-upstream-is-down")
    assert (reply.status_code >= 500, record.status, record.complete) == (True, reply.status_code, False)


def test_the_record_survives_a_restart_of_the_collector(stack: Stack) -> None:
    call(stack, "a-call-before-the-restart")
    recorded(stack, "a-call-before-the-restart")

    stack.run([*stack.compose, "restart", "collector"], capture_output=True)

    assert recorded(stack, "a-call-before-the-restart").status == 200
