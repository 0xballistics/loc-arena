"""The lifecycle API over HTTP: an agent renews its credential and administers its team through IAM alone."""

from __future__ import annotations

import pytest
from iam_server import serve

from meridian_iam.client import IamClient
from meridian_iam.contracts import Conflict, Forbidden, Unauthenticated
from meridian_iam.core import Iam
from meridian_iam.core.lifecycle import OPERATOR_ROLE, OPERATOR_TEAM


def test_renew_add_remove_and_disable_over_http(iam: Iam, tokens: dict[str, str]) -> None:
    iam.add_member("lead", OPERATOR_TEAM, OPERATOR_ROLE)
    with serve(iam) as (url, _):
        client = IamClient(url, tokens["svc"], retries=0)
        reply = client.renew_credential(tokens["alice"])
        assert reply["generation"] == 2
        assert client.identify(reply["credential"], "notes") == "alice"
        assert client.identify(tokens["alice"], "notes") == "alice"  # still good through the grace window
        with pytest.raises(Conflict):  # the old credential cannot replace the newer one
            client.renew_with(tokens["alice"], "mi_a_different_replacement_secret_value")
        assert (
            client.renew_with(tokens["alice"], reply["credential"])["generation"] == 2
        )  # same one: idempotent
        member = {"principal": "bob", "role": "lead", "authority": {"notes": ["read"]}}
        assert client.add_team_member(tokens["lead"], "research", member, "k1")["authority"] == {
            "notes": ["read"],
        }
        with pytest.raises(Forbidden):
            client.add_team_member(
                tokens["alice"],
                "research",
                member,
                "k2",
            )  # not an administrator of the team
        assert client.remove_team_member(tokens["bob"], "research", "bob", "k3") == {
            "team": "research",
            "principal": "bob",
            "active": False,
        }
        with pytest.raises(Forbidden):
            client.disable_principal(tokens["alice"], "bob", "k4")
        assert client.disable_principal(tokens["lead"], "bob", "k5")["active"] is False
        with pytest.raises(Unauthenticated):
            client.identify(tokens["bob"], "notes")


def test_one_request_is_fetched_by_id_over_http(iam: Iam, tokens: dict[str, str]) -> None:
    with serve(iam) as (url, _):
        client = IamClient(url, tokens["svc"], retries=0)
        body = {"resource": "notes", "permissions": ["write"], "reason": "append the report", "until": 1500}
        made = client.submit_request(tokens["alice"], body, "k")
        for token in (tokens["alice"], tokens["lead"]):  # its requester, and an approver of its resource
            assert client.get_request(token, made["request"])["reason"] == "append the report"
        with pytest.raises(Forbidden):
            client.get_request(tokens["bob"], made["request"])
        with pytest.raises(Forbidden):
            client.get_request(tokens["lead"], "no-such-request")
