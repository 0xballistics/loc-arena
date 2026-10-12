"""Gather the agents-service change plan, its steps and the design into ``spec.html``, drawn in the browser.

Run ``python docs/agents-service/render.py`` after changing a spec. The Markdown files stay the source: the
page embeds them verbatim between its markers and draws them with marked and mermaid at load time.
"""

from __future__ import annotations

from pathlib import Path

DIRECTORY = Path(__file__).parent
DOCUMENTS = (
    "README.md",
    "01-registry.md",
    "02-executions.md",
    "03-tracing.md",
    "04-unattended-work.md",
    "design.md",
)
PAGE = DIRECTORY / "spec.html"
START = "<!-- documents:start -->"
END = "<!-- documents:end -->"


def embed(name: str) -> str:
    """One spec as a script block the page renders, named by its file so links between specs resolve."""
    text = (DIRECTORY / name).read_text()
    if "</script" in text:
        raise ValueError(f"{name} contains '</script', which would end its block early")
    return f'<script type="text/markdown" data-file="{name}">\n{text}</script>'


def main() -> None:
    """Replace the page's embedded specs with the current files."""
    page = PAGE.read_text()
    head, rest = page.split(START)
    _, tail = rest.split(END)
    blocks = "\n".join(embed(name) for name in DOCUMENTS)
    PAGE.write_text(f"{head}{START}\n{blocks}\n{END}{tail}")


if __name__ == "__main__":
    main()
