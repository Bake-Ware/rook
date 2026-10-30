"""rook.kit._types: record values checked by the manifest type language."""

import subprocess
import sys

import pytest

from rook.kit._types import check, parse_duration


@pytest.mark.parametrize("spec, value", [
    ({"type": "str", "enum": ["a", "b"]}, "a"),
    ({"type": "int", "min": 1, "max": 5}, 5),
    ({"type": "float"}, 3),
    ({"type": "time"}, 1793390000.5),
    ({"type": "time"}, "2026-10-30T12:00:00Z"),
    ({"type": "duration"}, "90d"),
    ({"type": "duration"}, 30),
    ({"type": "list", "items": {"type": "int"}, "max": 3}, [1, 2]),
    ({"type": "map", "fields": {"x": {"type": "int", "required": True}}}, {"x": 1}),
    ({"type": "map", "items": {"type": "bool"}}, {"a": True}),
    ({"type": "ref", "to": "work.project"}, "p_1"),
    ({"type": "json"}, {"anything": [1, "x"]}),
    ({"schema": {"type": "string", "format": "uri"}}, "https://x"),
    ({}, "plain str"),
])
def test_fits(spec, value):
    check("f", spec, value)


@pytest.mark.parametrize("spec, value, msg", [
    ({"type": "bool"}, "yes", "f: expected bool, got str"),
    ({"type": "int"}, True, "f: expected int, got bool"),       # bools aren't numbers
    ({"type": "int"}, 1.5, "expected int"),
    ({"type": "str", "enum": ["a", "b"]}, "c", "is not one of"),
    ({"type": "str", "max": 2}, "abc", "too long"),
    ({"type": "int", "min": 1}, 0, "less than the minimum"),
    ({"type": "str", "pattern": "^[a-z]+$"}, "A1", "does not match"),
    ({"type": "list", "items": {"type": "int"}}, [1, "x"], "f.1: expected int, got str"),
    ({"type": "map", "fields": {"x": {"type": "int"}}}, {"y": 1}, "Additional properties"),
    ({"type": "map", "fields": {"x": {"type": "int", "required": True}}}, {}, "'x' is a required"),
    ({"type": "duration"}, "soon", "does not match"),
    ({"type": "nope"}, 1, "f: unknown type 'nope'"),
    ({"type": "int", "pattern": "x"}, 1, "pattern does not apply to type int"),
    ({"schema": {"type": "nonsense"}}, 1, "f: bad schema"),
])
def test_refused(spec, value, msg):
    with pytest.raises(ValueError, match=msg):
        check("f", spec, value)


def test_none_passes():
    check("f", {"type": "int", "required": True}, None)


def test_parse_duration():
    assert parse_duration("1h30m") == 5400
    assert parse_duration("250ms") == 0.25
    assert parse_duration(7) == 7.0
    for bad in ("", "5x", True, "m5"):
        with pytest.raises(ValueError):
            parse_duration(bad)


def test_workers_import_hooks_without_jsonschema():
    """Workers don't install jsonschema; hooks only need parse_duration."""
    code = ("import sys; sys.modules['jsonschema'] = None; import rook.kit.hooks; "
            "from rook.kit._types import parse_duration; print(parse_duration('5m'))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "300.0"
