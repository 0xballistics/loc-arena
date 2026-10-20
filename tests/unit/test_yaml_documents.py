"""The YAML the stack writes for compose, agentgateway and the Collector keeps every string a string."""

from __future__ import annotations

import pytest
from loc_arena.yaml_documents import dump_document


@pytest.mark.parametrize("text", ["401e98", "1e5", "+1e3", "0o17"])  # 401e98: a run's instance ID once
def test_a_string_a_yaml_1_2_reader_would_take_for_a_number_is_quoted(text: str) -> None:
    assert dump_document({"instance": text}) == f"instance: '{text}'\n"
