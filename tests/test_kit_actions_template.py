"""rook.kit.actions templating, parse and returns (no I/O)."""

import base64

import pytest

from rook.kit.actions.errors import ActionError, Secret
from rook.kit.actions.parse import parse, pick
from rook.kit.actions.template import (Missing, Scope, render, render_argv, render_map,
                                       render_tree, secret_names)


def scope(**kw):
    base = dict(args={}, settings={}, appliance=None, worker={"os": "linux", "name": "w1"},
                secrets={}, tmp="/tmp/call", plugin_dir="/p", plugin_data="/d")
    base.update(kw)
    return Scope(**base)


# --- variables ----------------------------------------------------------------

def test_variables_resolve():
    s = scope(args={"x": "1", "o": {"k": [5, 6]}}, settings={"c": "deluge"},
              appliance={"host": "tv.lan"}, secrets={"tok": "s3cret"})
    assert render("{{args.x}}-{{ settings.c }}-{{worker.name}}-{{worker.os}}", s) == "1-deluge-w1-linux"
    assert render("{{tmp}}|{{plugin.dir}}|{{plugin.data}}|{{appliance.host}}", s) == "/tmp/call|/p|/d|tv.lan"
    assert render("{{secret:tok}}", s) == "s3cret"
    assert render("{{args.o.k.1}}", s) == "6"


def test_lone_template_keeps_type_only_when_native():
    s = scope(args={"n": 3, "on": True, "l": [1, 2]})
    assert render("{{args.n}}", s, native=True) == 3
    assert render("{{args.n}}", s) == "3"
    assert render("x{{args.on}}", s) == "xtrue"
    assert render("{{args.l}}!", s) == "[1,2]!"


def test_unknown_variable_is_an_error_even_in_a_group():
    s = scope()
    for bad in ("{{nope}}", "{{args}}", "{{plugin.x}}", "{{os.environ}}"):
        with pytest.raises(ActionError, match="unknown template variable"):
            render(bad, s)
    with pytest.raises(ActionError):
        render_argv(["a", ["-x", "{{env.HOME}}"]], s)


def test_unset_values_raise_missing():
    s = scope(args={"none": None})
    with pytest.raises(Missing):
        render("{{args.gone}}", s)
    with pytest.raises(Missing):
        render("{{args.none}}", s)
    with pytest.raises(ActionError, match="not in the vault"):
        render("{{secret:absent}}", s)


def test_secret_names_found_anywhere():
    spec = {"url": "https://h/{{secret:a}}", "headers": {"x": "{{ secret:b }}"},
            "argv": ["x", ["{{secret:c}}"]], "n": 3}
    assert secret_names(spec) == {"a", "b", "c"}


def test_redact_hides_secrets_and_secret_settings():
    s = scope(settings={"pw": Secret("hunter22"), "plain": "visible"}, secrets={"t": "tok-abc"})
    assert s.redact("pw=hunter22 t=tok-abc plain=visible") == "pw=*** t=*** plain=visible"


# --- argv ---------------------------------------------------------------------

def test_argv_groups_drop_unless_every_value_set():
    s = scope(args={"state": "Seeding", "n": 2}, settings={"console": "deluge-console"})
    argv = ["{{settings.console}}", "info", ["-s", "{{args.state}}"], ["-n", "{{args.n}}", "{{args.m}}"],
            ["--a", ["--b", "{{args.zz}}"]]]
    assert render_argv(argv, s) == ["deluge-console", "info", "-s", "Seeding", "--a"]


def test_argv_slots_are_literal_and_lists_splice():
    s = scope(args={"evil": "a; rm -rf / $(id) `x` | y", "files": ["a b", "c"]})
    assert render_argv(["echo", "{{args.evil}}"], s) == ["echo", "a; rm -rf / $(id) `x` | y"]
    assert render_argv(["rm", "--", "{{args.files}}"], s) == ["rm", "--", "a b", "c"]


def test_argv_top_level_unset_is_missing_and_nul_refused():
    with pytest.raises(Missing):
        render_argv(["x", "{{args.a}}"], scope())
    with pytest.raises(ActionError, match="control character"):
        render_argv(["x", "{{args.a}}"], scope(args={"a": "a\0b"}))


# --- url ----------------------------------------------------------------------

def test_url_parts_escaped():
    s = scope(args={"q": "a&b=c d", "p": "../x/y?z", "dots": ".."}, appliance={"host": "10.0.0.5:8443"})
    assert render("https://{{appliance.host}}/api/{{args.p}}?v={{args.q}}", s, "url") == \
        "https://10.0.0.5:8443/api/..%2Fx%2Fy%3Fz?v=a%26b%3Dc%20d"
    assert render("http://h/{{args.dots}}/k", s, "url") == "http://h/%2E%2E/k"


def test_url_host_slot_rejects_injection():
    for bad in ("evil.com/x", "a@b", "h#f", "h?q", "h p"):
        with pytest.raises(ActionError, match="not a host"):
            render("https://{{appliance.host}}/x", scope(appliance={"host": bad}), "url")
    assert render("https://h:{{args.port}}/", scope(args={"port": 81}), "url") == "https://h:81/"
    assert render("https://[::1]:{{args.port}}/", scope(args={"port": 81}), "url") == "https://[::1]:81/"


def test_url_leading_value_must_be_absolute():
    s = scope(settings={"base": "http://hass.lan:8123"}, args={"u": "/etc/passwd"})
    assert render("{{settings.base}}/api/{{args.u}}", s, "url") == "http://hass.lan:8123/api/%2Fetc%2Fpasswd"
    with pytest.raises(ActionError, match="absolute URL"):
        render("{{args.u}}", s, "url")


# --- headers, lines, paths -------------------------------------------------------

def test_headers_and_lines_reject_newlines():
    s = scope(args={"v": "ok\r\nX-Evil: 1"})
    with pytest.raises(ActionError, match="control character"):
        render("Bearer {{args.v}}", s, "header")
    with pytest.raises(ActionError, match="control character"):
        render("{{args.v}}", s, "line", native=True)
    assert render("say {{args.v}}", s, "raw") == "say ok\r\nX-Evil: 1"


def test_path_values_fill_one_segment():
    s = scope(args={"name": "notes", "bad": "../../etc/passwd", "win": "a\\b", "dot": ".."})
    assert render("{{plugin.data}}/{{args.name}}.json", s, "path") == "/d/notes.json"
    for bad in ("bad", "win", "dot"):
        with pytest.raises(ActionError, match="single file name"):
            render("{{plugin.data}}/{{args.%s}}" % bad, s, "path")
    # a lone value hands the whole path to the caller (a cap that reads any file)
    assert render("{{args.bad}}", s, "path") == "../../etc/passwd"


# --- maps and trees ------------------------------------------------------------------

def test_maps_drop_unset_keys_and_stringify():
    s = scope(args={"a": 1, "b": False})
    assert render_map({"a": "{{args.a}}", "b": "{{args.b}}", "c": "{{args.c}}", "d": "lit"}, s, "raw") == \
        {"a": "1", "b": "false", "d": "lit"}


def test_tree_keeps_types_and_drops_unset():
    s = scope(args={"n": 2, "tags": ["x"], "name": "o\"k"})
    tree = {"n": "{{args.n}}", "tags": "{{args.tags}}", "gone": "{{args.gone}}",
            "list": ["{{args.name}}", "{{args.gone}}", 7], "nested": {"m": "pre-{{args.n}}"}}
    assert render_tree(tree, s) == {"n": 2, "tags": ["x"], "list": ["o\"k", 7], "nested": {"m": "pre-2"}}


# --- parse ------------------------------------------------------------------------

def test_parse_kinds():
    assert parse(b"hello\n", "text") == "hello"
    assert parse(b"a\n\n b \r\nc\n", "lines") == ["a", " b", "c"]
    assert parse(b'{"a": [1]}', "json") == {"a": [1]}
    assert parse(b"  \n", "json") is None
    assert parse(b"Name: box\nstate = up\njunk\nurl: http://x:1\n", "kv") == \
        {"Name": "box", "state": "up", "url": "http://x:1"}
    assert parse(b"a,b\n1,2\n3,4\n", "csv") == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]
    f = parse(b"\x89PNG", "bytes", name="s.png")
    assert f == {"name": "s.png", "mime": "image/png", "bytes": 4,
                 "data": base64.b64encode(b"\x89PNG").decode()}
    with pytest.raises(ActionError, match="not JSON"):
        parse(b"nope", "json")
    with pytest.raises(ActionError, match="unknown parse"):
        parse(b"x", "yaml")


def test_parse_regex():
    out = "t1 Seeding 50%\nt2 Paused 10%\n"
    rx = r"^(?P<id>\w+) (?P<state>\w+) (?P<pct>\d+)%$"
    assert parse(out, {"regex": rx}) == {"id": "t1", "state": "Seeding", "pct": "50"}
    assert parse(out, {"regex": rx, "each": True})[1] == {"id": "t2", "state": "Paused", "pct": "10"}
    assert parse("zzz", {"regex": rx}) is None
    with pytest.raises(ActionError, match="regex is invalid"):
        parse("x", {"regex": "(?P<a"})


# --- returns ------------------------------------------------------------------------

def test_returns_pick_and_rename():
    row = {"name": "a", "state": "up", "progress": {"percent": 40}, "items": [{"id": 9}]}
    assert pick(row, ["name", "progress.percent"]) == {"name": "a", "percent": 40}
    assert pick(row, {"pct": "progress.percent", "first": "items.0.id", "none": "x.y"}) == \
        {"pct": 40, "first": 9, "none": None}
    assert pick([row, row], ["state"]) == [{"state": "up"}, {"state": "up"}]
    assert pick("scalar", None) == "scalar"
    with pytest.raises(ActionError, match="needs an object"):
        pick("scalar", ["a"])
