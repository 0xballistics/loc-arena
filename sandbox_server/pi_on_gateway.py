#!/usr/bin/env python3
"""Run pi once through the gateway: ``pi-on-gateway <model> <system prompt> <input>``.

pi reads a provider's URL from its ``models.json`` alone, never from a variable, and a runner's reset empties
pi's home: so each run writes the gateway provider there afresh, from ``LOC_ARENA_GATEWAY_API``. The system
prompt reaches pi as a file and the input on stdin, never as arguments: pi reads an argument that names a file
as that file's text, and attaches the file an argument starting with ``@`` names.
"""

import json
import os
import sys
from pathlib import Path

PROVIDER = "gateway"


def main() -> None:
    """Write pi's provider, system prompt and input into its home, then become pi printing its one reply."""
    model, system_prompt, execution_input = sys.argv[1:]
    agent_directory = Path.home() / ".pi" / "agent"
    agent_directory.mkdir(parents=True, exist_ok=True)
    provider = {
        "baseUrl": os.environ["LOC_ARENA_GATEWAY_API"],
        "api": "openai-completions",
        "apiKey": "replaced-by-the-gateway",
        "models": [{"id": model}],
    }
    (agent_directory / "models.json").write_text(json.dumps({"providers": {PROVIDER: provider}}))
    system_prompt_file = agent_directory / "system-prompt.md"
    system_prompt_file.write_text(system_prompt)
    input_file = agent_directory / "input.md"
    input_file.write_text(execution_input)
    os.dup2(os.open(input_file, os.O_RDONLY), sys.stdin.fileno())
    os.execvp(
        "pi",
        [
            "pi",
            "--model",
            f"{PROVIDER}/{model}",
            "--print",
            "--no-session",
            "--system-prompt",
            str(system_prompt_file),
        ],
    )


if __name__ == "__main__":
    main()
