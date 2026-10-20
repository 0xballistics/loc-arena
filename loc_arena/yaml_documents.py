"""YAML for the stack's YAML 1.2 readers: Docker compose, agentgateway and the OpenTelemetry Collector.

PyYAML writes YAML 1.1, so it leaves a string such as ``401e98``, ``1e5`` or ``0o17`` unquoted, and a YAML 1.2
reader takes it for a number. A run's random instance ID can look like one, and compose then refuses the file.
Compose and the Collector read with go-yaml, which also drops ``_`` between digits and takes radix prefixes in
either case and after a sign (``0X1F``, ``-0o17``, ``1_0e5``), so those are quoted too.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import yaml


class _Yaml12SafeDumper(yaml.SafeDumper):
    """A safe dumper that quotes every string a YAML 1.2 reader would take for a number."""


# Numbers as go-yaml reads them: decimals and floats with or without a dot (``1e5``), and radix integers, each
# with ``_`` between digits. Quoting a string that is not one is harmless; YAML 1.1 quotes the rest.
_Yaml12SafeDumper.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"^[-+]?(?:\.[0-9_]+|[0-9][0-9_]*(?:\.[0-9_]*)?)(?:[eE][-+]?[0-9_]+)?$"),
    list("-+0123456789."),
)
_Yaml12SafeDumper.add_implicit_resolver(
    "tag:yaml.org,2002:int",
    re.compile(r"^[-+]?0[xXoObB][0-9a-fA-F_]+$"),
    list("-+0"),
)


def dump_document(document: Mapping[str, Any], *, sort_keys: bool = False) -> str:
    """``document`` as YAML a YAML 1.2 reader reads back with every string still a string."""
    return yaml.dump(dict(document), Dumper=_Yaml12SafeDumper, sort_keys=sort_keys)
