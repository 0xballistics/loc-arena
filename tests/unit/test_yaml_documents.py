"""The YAML the stack writes for compose, agentgateway and the Collector keeps every string a string."""

from __future__ import annotations

import pytest
from loc_arena.yaml_documents import dump_document


@pytest.mark.parametrize(  # 401e98: a run's instance ID once; the rest as compose's go-yaml reads numbers
    "text",
    ["401e98", "1e5", "+1e3", "0o17", "0X1F", "-0o17", "1_0e5", "0_9"],
)
def test_a_string_a_yaml_1_2_reader_would_take_for_a_number_is_quoted(text: str) -> None:
    assert dump_document({"instance": text}) == f"instance: '{text}'\n"
