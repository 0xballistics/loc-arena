"""A tool call's arguments as pi reads the text the model wrote, ported from the runners' pi.

pi-ai 0.99.2 (``utils/json-parse.js``, ``parseStreamingJson``) parses the text as JSON, then as JSON with
its string literals repaired, then completes it as partial JSON (partial-json 0.1.7), then completes the
repaired text, and takes ``{}`` when all four fail. Each step is ported as written, JavaScript's
``substring``, ``lastIndexOf``, out-of-range indexing and ``JSON.parse`` included, so a span's arguments can
be checked against what pi held. Values compare as JavaScript serializes them (``as_js_value``): every number
a double, and one that is not finite ``null``.
"""

from __future__ import annotations

import json
import math
import re

from pydantic import JsonValue

JS_WHITESPACE = (  # what JavaScript's trim removes
    " \t\n\v\f\r\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)
VALID_JSON_ESCAPES = frozenset('"\\/bfnrtu')
CONTROL_CHARACTER_ESCAPES = {"\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


class _UnparsableError(ValueError):
    """What JavaScript's parse would throw: partial-json's PartialJSON and MalformedJSON, or a SyntaxError."""


def _reject_constant(constant: str) -> float:
    raise _UnparsableError(f"JSON.parse takes no {constant}")


def _parse_json(text: str) -> JsonValue:
    """JavaScript's ``JSON.parse``, which takes no ``NaN`` or ``Infinity``."""
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as error:
        raise _UnparsableError(str(error)) from error


def _substring(text: str, start: int, end: int) -> str:
    """JavaScript's ``substring``: each end clamped into the text, and the ends swapped when out of order."""
    start, end = (min(max(position, 0), len(text)) for position in (start, end))
    return text[min(start, end) : max(start, end)]


def repair_json(text: str) -> str:
    """``repairJson`` of pi: inside string literals, escape raw control characters and stray backslashes."""
    repaired = ""
    in_string = False
    index = 0
    while index < len(text):
        character = text[index]
        if not in_string:
            repaired += character
            in_string = character == '"'
        elif character == '"':
            repaired += character
            in_string = False
        elif character == "\\":
            following = text[index + 1 : index + 2]
            if following == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", text[index + 2 : index + 6]):
                repaired += f"\\u{text[index + 2 : index + 6]}"
                index += 5
            elif following and following in VALID_JSON_ESCAPES:
                repaired += f"\\{following}"
                index += 1
            else:
                repaired += "\\\\"
        elif ord(character) <= 0x1F:
            repaired += CONTROL_CHARACTER_ESCAPES.get(character, f"\\u{ord(character):04x}")
        else:
            repaired += character
        index += 1
    return repaired


def _parse_json_with_repair(text: str) -> JsonValue:
    """``parseJsonWithRepair`` of pi: ``JSON.parse``, then ``JSON.parse`` of the repaired text."""
    try:
        return _parse_json(text)
    except _UnparsableError:
        return _parse_json(repair_json(text))


class _PartialJsonParser:
    """partial-json 0.1.7's ``parse`` with every kind allowed to be partial, as pi calls it."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.index = 0

    def _at(self, index: int) -> str:
        return self.text[index : index + 1]  # "" past either end, where JavaScript reads undefined

    def _skip_blank(self) -> None:
        while self.index < len(self.text) and self.text[self.index] in " \n\r\t":
            self.index += 1

    def _rest_is_prefix_of(self, literal: str, shorter_than: int) -> bool:
        return len(self.text) - self.index < shorter_than and literal.startswith(self.text[self.index :])

    def parse_any(self) -> JsonValue:  # noqa: C901, PLR0911
        self._skip_blank()
        if self.index >= len(self.text):
            raise _UnparsableError("Unexpected end of input")
        character = self.text[self.index]
        if character == '"':
            return self._parse_string()
        if character == "{":
            return self._parse_object()
        if character == "[":
            return self._parse_array()
        for literal, value in (("null", None), ("true", True), ("false", False), ("Infinity", math.inf)):
            if self.text[self.index : self.index + len(literal)] == literal or self._rest_is_prefix_of(
                literal,
                len(literal),
            ):
                self.index += len(literal)
                return value
        rest = len(self.text) - self.index
        if self.text[self.index : self.index + 9] == "-Infinity" or (
            1 < rest < 9 and "-Infinity".startswith(self.text[self.index :])  # noqa: PLR2004
        ):
            self.index += 9
            return -math.inf
        if self.text[self.index : self.index + 3] == "NaN" or self._rest_is_prefix_of("NaN", 3):
            self.index += 3
            return math.nan
        return self._parse_number()

    def _parse_string(self) -> JsonValue:
        start = self.index
        escape = False
        self.index += 1
        while self.index < len(self.text) and (
            self.text[self.index] != '"' or (escape and self.text[self.index - 1] == "\\")
        ):
            escape = not escape if self.text[self.index] == "\\" else False
            self.index += 1
        if self._at(self.index) == '"':
            self.index += 1
            return _parse_json(_substring(self.text, start, self.index - escape))
        try:
            return _parse_json(_substring(self.text, start, self.index - escape) + '"')
        except _UnparsableError:
            return _parse_json(_substring(self.text, start, self.text.rfind("\\")) + '"')

    def _parse_object(self) -> dict[str, JsonValue]:
        self.index += 1
        self._skip_blank()
        parsed: dict[str, JsonValue] = {}
        try:
            while self._at(self.index) != "}":
                self._skip_blank()
                if self.index >= len(self.text):
                    return parsed
                key = self._parse_string()
                self._skip_blank()
                self.index += 1
                try:
                    parsed[str(key)] = self.parse_any()
                except _UnparsableError:
                    return parsed
                self._skip_blank()
                if self._at(self.index) == ",":
                    self.index += 1
        except _UnparsableError:
            return parsed
        self.index += 1
        return parsed

    def _parse_array(self) -> list[JsonValue]:
        self.index += 1
        parsed: list[JsonValue] = []
        try:
            while self._at(self.index) != "]":
                parsed.append(self.parse_any())
                self._skip_blank()
                if self._at(self.index) == ",":
                    self.index += 1
        except _UnparsableError:
            return parsed
        self.index += 1
        return parsed

    def _parse_number(self) -> JsonValue:
        if self.index == 0:
            if self.text == "-":
                raise _UnparsableError("Not sure what '-' is")
            try:
                return _parse_json(self.text)
            except _UnparsableError:
                return _parse_json(_substring(self.text, 0, self.text.rfind("e")))
        start = self.index
        if self._at(self.index) == "-":
            self.index += 1
        while self.index < len(self.text) and self.text[self.index] not in ",]}":
            self.index += 1
        try:
            return _parse_json(_substring(self.text, start, self.index))
        except _UnparsableError:
            if _substring(self.text, start, self.index) == "-":
                raise
            return _parse_json(_substring(self.text, start, self.text.rfind("e")))


def _parse_partial_json(text: str) -> JsonValue:
    """partial-json's ``parse``, which throws on a text that is blank once trimmed."""
    trimmed = text.strip(JS_WHITESPACE)
    if not trimmed:
        raise _UnparsableError("empty")
    return _PartialJsonParser(trimmed).parse_any()


def read_tool_arguments(written: str) -> JsonValue:
    """``parseStreamingJson`` of pi on the arguments the model wrote: what pi's tool call holds."""
    if not written.strip(JS_WHITESPACE):
        return {}
    try:
        return _parse_json_with_repair(written)
    except _UnparsableError:
        pass
    for completing in (written, repair_json(written)):
        try:
            completed = _parse_partial_json(completing)
        except _UnparsableError:
            continue
        return {} if completed is None else completed
    return {}


def as_js_value(value: JsonValue) -> JsonValue:
    """``value`` as JavaScript serializes it and reads it back: each number a double, one not finite null."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, list):
        return [as_js_value(item) for item in value]
    return {key: as_js_value(item) for key, item in value.items()}
