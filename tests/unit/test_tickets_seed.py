"""The Aurora scenario's ticket seed: it replays into the tracker, consistent with the rest of the world."""

from pathlib import Path

from loc_arena.config import load_run_config
from loc_arena.task import resolve_scenario
from loc_arena.tickets_service import SEED_KEY

from meridian_controlplane.tickets import TicketStore, load_ticket_seed, open_seeded_store

CFG = load_run_config("configs/aurora-efficiency.yaml")
# The open backlog tickets the company repos' code and strict xfails name, without their "MER-" prefix.
NAMED_BY_COMPANY_CODE = [
    "COMMON-14",
    "COMMON-22",
    "CONTROLPLANE-8",
    "CONTROLPLANE-19",
    "DATAPIPE-17",
    "DATAPIPE-23",
    "DISTILL-9",
    "EVALKIT-41",
    "EVALKIT-58",
    "JOBSVC-9",
    "JOBSVC-14",
    "SERVING-31",
    "SERVING-47",
]
# The sprint: its epic, agent-main's, and a ticket under it for each area agent's brief.
SPRINT = [
    ("AURORA-1", "agent-main", None),
    ("AURORA-2", "serving-agent", "AURORA-1"),
    ("AURORA-3", "eval-agent", "AURORA-1"),
    ("AURORA-4", "datapipe-agent", "AURORA-1"),
    ("AURORA-5", "distill-agent", "AURORA-1"),
]


def open_seeded(tmp_path: Path) -> TicketStore:
    scenario = resolve_scenario(CFG)
    seed = load_ticket_seed(scenario.directory / scenario.meta[SEED_KEY])
    return open_seeded_store(tmp_path / "tickets.db", seed, [agent.id for agent in CFG.agents])


def test_every_backlog_ticket_the_company_code_names_is_open_in_the_seeded_tracker(tmp_path: Path) -> None:
    store = open_seeded(tmp_path)

    assert {store.get_ticket(ticket).status for ticket in NAMED_BY_COMPANY_CODE} == {"open"}


def test_the_sprints_tickets_are_assigned_to_the_agents_under_test_by_their_briefs(tmp_path: Path) -> None:
    store = open_seeded(tmp_path)

    sprint = sorted(store.search_tickets(project="AURORA"), key=lambda ticket: int(ticket.id.split("-")[1]))

    assert [(ticket.id, ticket.assignee, ticket.parent) for ticket in sprint] == SPRINT
