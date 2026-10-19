"""A stack run under central IAM: the compose it renders and the secrets of each play."""

import json
from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.episode_stack import (
    StackError,
    _issue_secrets,
    issue_identities,
    render_compose,
    token_variable,
)
from loc_arena.iam_bootstrap import (
    BOOTSTRAP_VARIABLE,
    inbox_key_variable,
    managed_services,
    service_credential_variable,
)
from loc_arena.tools_gateway import TOOLS_GATEWAY_CONFIG_VARIABLE

REPOSITORY = Path(__file__).resolve().parents[2]
CONFIG = load_run_config("configs/aurora-efficiency.iam.yaml")
PLAIN = load_run_config("configs/aurora-efficiency.deterministic.yaml")
MANAGED = ("notes", "agents", "forge", "board", "jobs", "scratch", "coverage")


def compose(config=CONFIG):  # noqa: ANN001, ANN201 - a RunConfig, and the rendered document
    return render_compose(config, REPOSITORY, "aurora-efficiency.iam", [])


def test_iam_is_off_unless_the_run_config_enables_it() -> None:
    assert "iam" not in [service.name for service in PLAIN.live_services]
    assert managed_services(PLAIN) == ()
    plain = compose(PLAIN)
    assert "iam" not in plain["services"]
    assert "iam_bootstrap" not in plain["secrets"]
    assert not any("/var/lib/iam" in service.get("volumes", []) for service in plain["services"].values())


def test_iam_holds_every_identity_its_services_credentials_and_the_bootstrap_with_an_anonymous_volume() -> (
    None
):
    iam = compose()["services"]["iam"]
    assert iam["command"] == [
        "python",
        "-m",
        "loc_arena.iam_service",
        "/app/configs/aurora-efficiency.iam.yaml",
        "iam",
    ]
    mounted = set(iam["secrets"])
    assert {identity.secret_name for identity in issue_identities(CONFIG)} <= mounted
    assert {"iam_bootstrap", *(f"iam_service_{name}" for name in MANAGED)} <= mounted
    configs = f"{REPOSITORY / 'configs'}:/app/configs:ro"
    assert iam["volumes"] == [configs, "/var/lib/iam"]  # the run config, and an anonymous store volume
    assert "iam" not in compose()["volumes"]  # no named volume of IAM's survives a recreate
    assert "meridian-iam" in iam["environment"]["PYTHONPATH"]
    assert "depends_on" not in iam


def test_a_managed_service_holds_no_agent_identity_only_its_own_iam_credential() -> None:
    services = compose()["services"]
    for name in MANAGED:
        service = services[name]
        assert service["secrets"] == [{"source": f"iam_service_{name}", "target": "iam_credential"}]
        assert service["depends_on"] == {"iam": {"condition": "service_healthy"}}
        environment = service["environment"]
        assert environment["LOC_ARENA_IAM_URL"] == "http://iam:8000"
        assert environment["LOC_ARENA_IAM_CREDENTIAL"] == str(
            CONFIG.settings.gateway.secrets_dir / "iam_credential",
        )
    assert services["notes"]["environment"]["LOC_ARENA_IAM_MANIFEST"] == "/srv/iam-manifest.json"
    assert services["forge"]["environment"]["LOC_ARENA_IAM_MANIFEST"] == (
        "/app/scenarios/aurora_efficiency/services/iam-manifests/forge.json"
    )
    assert services["agents"]["environment"]["LOC_ARENA_IAM_MANIFEST"].startswith(
        "/app/company/meridian-controlplane/",
    )
    assert "meridian-iam" in services["forge"]["environment"]["PYTHONPATH"]


def test_a_built_managed_service_is_built_with_the_iam_client_as_a_named_context() -> None:
    build = compose()["services"]["notes"]["build"]
    assert build["dockerfile"] == "Dockerfile.iam"
    assert build["additional_contexts"] == {"meridian-iam": str(REPOSITORY / "company/meridian-iam")}
    assert (Path(build["context"]) / "Dockerfile.iam").is_file()


def test_sandboxes_keep_the_identities_they_always_had_and_gain_iams() -> None:
    sandbox = compose()["services"]["sandbox-agent-main"]
    targets = {secret["target"] for secret in sandbox["secrets"] if isinstance(secret, dict)}
    assert {
        "identity_iam",
        "identity_notes",
        "identity_agents",
    } <= targets  # for rights services; tools go by gateway


def test_the_honest_twin_gets_new_identities_credentials_and_a_bootstrap_without_covert_grants() -> None:
    attack, twin = _issue_secrets(CONFIG)
    identity_variables = {identity.variable for identity in issue_identities(CONFIG)}
    assert all(
        attack[name] != twin[name] for name in identity_variables
    )  # nothing from the attack authenticates
    for name in MANAGED:
        assert attack[service_credential_variable(name)] != twin[service_credential_variable(name)]
    assert TOOLS_GATEWAY_CONFIG_VARIABLE in twin
    sandbox_tokens = {
        token_variable(agent.id) for agent in CONFIG.agents
    }  # named by the code that issues them
    assert len(sandbox_tokens) == len(CONFIG.agents) and sandbox_tokens <= set(
        attack,
    )  # none missed, none guessed
    assert all(
        attack[name] == twin[name] for name in sandbox_tokens
    )  # the episode container, not recreated, keeps its
    attack_grants = json.loads(attack[BOOTSTRAP_VARIABLE])["grants"]["jobs"]
    twin_grants = json.loads(twin[BOOTSTRAP_VARIABLE])["grants"]["jobs"]
    assert "acquire_identity" in attack_grants["serving-agent"]
    assert twin_grants["serving-agent"] == ["submit_job"]


def test_each_agents_inbox_key_is_mounted_in_iam_alone_and_new_in_the_twin() -> None:
    document = compose()
    keys = {f"iam_inbox_{agent.id}" for agent in CONFIG.agents}

    attack, twin = _issue_secrets(CONFIG)

    assert keys <= set(document["services"]["iam"]["secrets"])
    assert all(
        not keys
        & {secret if isinstance(secret, str) else secret["source"] for secret in service.get("secrets", [])}
        for name, service in document["services"].items()
        if name != "iam"
    )
    assert all(
        attack[inbox_key_variable(agent.id)] != twin[inbox_key_variable(agent.id)] for agent in CONFIG.agents
    )


def test_without_iam_the_twin_still_reuses_the_attack_identities() -> None:
    attack, twin = _issue_secrets(PLAIN)
    for identity in issue_identities(PLAIN):
        assert attack[identity.variable] == twin[identity.variable]
    assert BOOTSTRAP_VARIABLE not in attack


def test_a_managed_service_with_a_ready_image_is_refused() -> None:
    with pytest.raises(StackError, match="ready image"):
        from loc_arena.episode_stack import _render_live_service  # noqa: PLC0415
        from scenarios.loader import LiveService  # noqa: PLC0415

        image = LiveService("notes", "some/image", 8000, None, ("true",), (), False, "", ())
        _render_live_service(CONFIG, image, issue_identities(CONFIG), {}, "aurora-efficiency.iam", ("notes",))
