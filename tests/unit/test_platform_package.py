"""The platform package (meridian-iam) sits beside the work repos but is never one of them."""

from pathlib import Path

from loc_arena.tasks.main_task_grader import COMPANY_REPOS, seed_checkout

REPOSITORY = Path(__file__).resolve().parents[2]


def test_the_platform_package_is_never_in_the_agents_checkout(tmp_path: Path) -> None:
    assert (REPOSITORY / "company/meridian-iam").is_dir()
    assert "meridian-iam" not in COMPANY_REPOS
    checkout = seed_checkout(tmp_path / "checkout")
    assert sorted(path.name for path in checkout.iterdir()) == list(COMPANY_REPOS)
    for repo in COMPANY_REPOS:  # and no work repo needs it to import: they are copied without it
        sources = list((checkout / repo).rglob("*.py"))
        assert not any("meridian_iam" in path.read_text() for path in sources), repo
