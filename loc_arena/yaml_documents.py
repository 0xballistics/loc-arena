"""YAML for the stack's YAML 1.2 readers: Docker compose, agentgateway and the OpenTelemetry Collector.

PyYAML writes YAML 1.1, so it leaves a string such as ``401e98``, ``1e5`` or ``0o17`` unquoted, and a YAML 1.2
reader takes it for a number. A run's random instance ID can look like one, and compose then refuses the file.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import yaml


class _Yaml12SafeDumper(yaml.SafeDumper):
    """A safe dumper that quotes every string a YAML 1.2 reader would take for a number."""


# YAML 1.2's core schema: floats with or without a dot (``1e5``), and ``0o`` octals; YAML 1.1 quotes the rest.
_Yaml12SafeDumper.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"^[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?$"),
    list("-+0123456789."),
)
_Yaml12SafeDumper.add_implicit_resolver("tag:yaml.org,2002:int", re.compile(r"^0o[0-7]+$"), ["0"])


def dump_document(document: Mapping[str, Any], *, sort_keys: bool = False) -> str:
    """``document`` as YAML a YAML 1.2 reader reads back with every string still a string."""
    return yaml.dump(dict(document), Dumper=_Yaml12SafeDumper, sort_keys=sort_keys)
