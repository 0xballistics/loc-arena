"""The port of pi's reading of a tool call's arguments, on texts the runners' pi itself read.

Each expected value is what pi-ai 0.99.2's ``parseStreamingJson`` made of the text, serialized as JavaScript
does.
"""

from __future__ import annotations

import pytest
from loc_arena.verifier.pi_tool_arguments import as_js_value, read_tool_arguments
from pydantic import JsonValue

READ_BY_PI: list[tuple[str, JsonValue]] = [
    ('{"command": "ls"}', {"command": "ls"}),
    ('{"command": "ls",}', {"command": "ls"}),
    ('{"command": "echo hi', {"command": "echo hi"}),
    ('{"command": "line 1\nline 2"}', {"command": "line 1\nline 2"}),
    ('{"path": "C:\\q"}', {"path": "C:\\q"}),
    ('{"ids": [1, 2,', {"ids": [1, 2]}),
    ('{"a": NaN, "b": Infinity}', {"a": None, "b": None}),
    ('{"n": 12345678901234567890}', {"n": 12345678901234567000}),
    ('{"a": "x"} trailing', {"a": "x"}),
    ("null", None),
    ("garbage", {}),
    ("  ", {}),
]


@pytest.mark.parametrize(
    ("written", "held"),
    READ_BY_PI,
    ids=[
        "json",
        "trailing-comma",
        "cut-off-string",
        "raw-newline",
        "invalid-escape",
        "cut-off-array",
        "not-finite",
        "past-a-double",
        "trailing-text",
        "null",
        "not-json",
        "blank",
    ],
)
def test_arguments_are_read_as_pi_reads_them(written: str, held: JsonValue) -> None:
    assert as_js_value(read_tool_arguments(written)) == as_js_value(held)
