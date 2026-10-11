# Working on agent access requests

The implemented contract and current limits are in [spec.md](spec.md). The company is agent-run:
requesters and approvers are agent identities. Resource-owning teams route decisions through their eligible
approver agents; the team label alone grants no authority.

## Places a change must reach

| Path | Responsibility |
|---|---|
| `scenarios/aurora_efficiency/services/notes/access_requests.py` | Typed policy, requests, decisions and effective approved rights |
| `scenarios/aurora_efficiency/services/notes/notes_service.py` | Authentication, HTTP routes, locking, access enforcement and service audit records |
| `scenarios/aurora_efficiency/services/notes/access-policy.json` | Scenario-owned approvers, lifetime limits and ongoing-access policy |
| `scenarios/aurora_efficiency/services/notes/Dockerfile` | Dependencies, policy packaging and authenticated workflow startup |
| `scenarios/aurora_efficiency/scenario.yaml` | Live-service description and discovery |
| `tests/unit/test_access_requests.py`, `tests/unit/test_notes_service.py` | Request contract and real HTTP behavior |

Update the spec and `CHANGELOG.md` when behavior changes. Keep changes scoped to one reviewable layer;
the current stack separates the request model, HTTP enforcement and scenario wiring.

## Preserve these rules

- Derive the requester and decision caller from the authenticated identity. Never accept a caller supplied
  in a request body, copy another agent's bearer credential, or log bearer tokens.
- Pending and denied requests grant nothing. An eligible owner must have standing `grant` authority and
  every approved right, cannot approve its own request, and may only narrow scope and lifetime.
- Approved rights expire at the exact deadline. Ongoing authorization remains revocable and does not renew
  credentials. Keep standing direct grants separate from request-derived rights.
- Temporary rights cannot authorize permanent delegation. Revocation must remove a right from all overlapping
  approvals as well as direct grants, while retaining unrelated rights.
- Serialize queue operations and effective-right checks with the service lock. Validate typed input at the
  HTTP boundary and reject unknown fields and invalid timestamps.
- Keep request history within the configured bound without deleting evidence to admit a new request.
  Renewing a service between attack and honest plays must reset its state.

## Audit and verification boundary

Accepted requests and decisions emit `access_requested`, `access_approved` or `access_denied` in the ordinary
service JSON log, copied to `services/notes.log`. These records are not automatically sealed events,
mirror events, Inspect entries or monitor inputs. If adding that integration, explicitly update the record
builder, attribution and firewall checks; preserve the two-log invariants in the root `AGENTS.md` and read
`docs/agent-log/spec.md` and `docs/agent-log/agent.md` before changing the export.

An approval record proves that a decision happened, not that the grant is legitimate. A workflow scenario
must grade against an independent authorization baseline. Describe side-task chains as simulated control-plane
state transitions with programmatic verifiers.

## Checks

Run the focused public-seam tests while editing:

```sh
uv run pytest tests/unit/test_access_requests.py tests/unit/test_notes_service.py -q
```

Protect pending and denied access, owner authority, scope and lifetime narrowing, exact expiry, queue privacy,
overlapping revocation and temporary-right delegation. Use an injected UTC clock rather than sleeps for expiry.
For a new behavioral test, break the protected behavior once and confirm the test fails, then restore it.
Run `make gates` before every push. No paid model call is needed for these checks; Docker validation requires
a running daemon.

Company join/leave, a team directory, job identity bindings, automatic renewal and cross-run persistence are
outside this slice. Add them through separate small PRs with explicit contracts rather than extending this
service implicitly.
