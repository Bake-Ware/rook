"""rook.kit.manifest: schema, type language, semantic checks, hashing, diff,
and the conformance vectors in conformance/manifest/."""

import json
from pathlib import Path

import pytest

pytest.importorskip("jsonschema")

from rook.kit.manifest import (ManifestError, canonical_json, check_bump, compile_type, diff,
                               file_hashes, glob_match, hold_effects, load, schema,
                               trust_class, validate)
from rook.kit.manifest import _yaml
from rook.kit.manifest.__main__ import main
from rook.kit.manifest.load import check_text
from rook.kit.manifest.types import TypeSpecError

ROOT = Path(__file__).resolve().parents[1] / "conformance" / "manifest"
EXPECTED = json.loads((ROOT / "expected.json").read_text())
PIKVM = (ROOT / "valid" / "pikvm.yaml").read_text()


def _codes(issues):
    return [{"level": i.level, "path": i.path, "code": i.code} for i in issues]


# -- conformance vectors -------------------------------------------------------

def test_every_vector_has_expectations():
    on_disk = {p.relative_to(ROOT).as_posix() for p in ROOT.glob("*/*.yaml")}
    assert on_disk == set(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_vector(name):
    _, issues = check_text((ROOT / name).read_text())
    assert _codes(issues) == EXPECTED[name]
    errors = [i for i in issues if i.level == "error"]
    assert bool(errors) == name.startswith("invalid/")


def test_schema_is_valid_json_schema():
    from jsonschema import Draft202012Validator
    Draft202012Validator.check_schema(schema())


# -- YAML subset ---------------------------------------------------------------

def test_yaml_12_scalars():
    data = _yaml.loads("a: [on, off, yes, no, true, False]\nb: 2026-10-30\nc: 1:30\nd: 0x1f\ne: 1e3\n"
                       "f: ~\ng: .inf\non: 1\n")
    assert data == {"a": ["on", "off", "yes", "no", True, False], "b": "2026-10-30",
                    "c": "1:30", "d": 31, "e": 1000.0, "f": None, "g": float("inf"), "on": 1}


@pytest.mark.parametrize("text", ["a: " + "[" * 5000 + "]" * 5000, "a: &x 1\nb: *x\n", "a: 1\na: 2\n", "1: x\n",
                                  "a: !!python/object:os.system x\n", "a: !custom x\n", "a: [\n"])
def test_yaml_refused(text):
    with pytest.raises(_yaml.YamlError):
        _yaml.loads(text)


def test_pikvm_enum_survives_yaml():
    m = _yaml.loads(PIKVM)
    assert m["caps"]["power.set"]["args"]["action"]["enum"] == ["on", "off", "off_hard", "reset_hard"]


# -- type language -------------------------------------------------------------

def test_compile_types():
    assert compile_type({}) == {"type": "string", "x-rook-type": "str"}
    assert compile_type({"type": "int", "min": 1, "max": 5, "default": 2, "doc": "n"}) == {
        "type": "integer", "x-rook-type": "int", "minimum": 1, "maximum": 5, "default": 2,
        "description": "n"}
    assert compile_type({"type": "str", "min": 1, "pattern": "^a", "enum": ["a", "ab"]}) == {
        "type": "string", "x-rook-type": "str", "minLength": 1, "pattern": "^a", "enum": ["a", "ab"]}
    lst = compile_type({"type": "list", "items": {"type": "ref", "to": "workbench.project"}, "max": 3})
    assert lst["items"]["x-rook-ref"] == "workbench.project" and lst["maxItems"] == 3
    m = compile_type({"type": "map", "fields": {"a": {"type": "bool", "required": True}, "b": {}}})
    assert m["required"] == ["a"] and m["additionalProperties"] is False
    assert compile_type({"type": "map", "items": {"type": "int"}})["additionalProperties"]["type"] == "integer"
    assert compile_type({"schema": {"type": "null"}, "doc": "raw"}) == {"type": "null", "description": "raw"}
    assert compile_type({"type": "secret"})["writeOnly"] is True


@pytest.mark.parametrize("spec", [{"type": "string"}, {"type": "bool", "max": 1},
                                  {"type": "ref"}, {"type": "int", "fields": {}},
                                  {"type": "list", "items": {"type": "nope"}}])
def test_compile_type_refuses(spec):
    with pytest.raises(TypeSpecError):
        compile_type(spec)


def test_compiled_types_validate_values():
    from jsonschema import Draft202012Validator
    ok = Draft202012Validator(compile_type({"type": "duration"}))
    assert ok.is_valid("30s") and ok.is_valid(1.5) and not ok.is_valid("30 sec")
    t = Draft202012Validator(compile_type({"type": "time"}))
    assert t.is_valid("2026-10-30T12:00:00Z") and t.is_valid(1793382011.9)


# -- load ----------------------------------------------------------------------

def _folder(tmp_path, text, files=()):
    (tmp_path / "plugin.yaml").write_text(text)
    for f in files:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("x")
    return tmp_path


def test_load_normalizes(tmp_path):
    m = load(_folder(tmp_path, (ROOT / "valid" / "deluge.yaml").read_text(), ["scripts/stats.py"]))
    assert m.plugin == "deluge" and m.version == "1.2.0"
    assert set(m.caps) == {"deluge.torrents.list", "deluge.torrents.remove", "deluge.stats.custom"}
    cap = m.caps["deluge.torrents.remove"]
    assert cap["on"] == "worker" and cap["elevation"] == "never" and cap["tags"] == []
    assert cap["args"]["id"] == {"type": "str", "required": True}
    assert m.data["settings"]["password"] == {"where": "hub", "secret": True, "type": "secret",
                                              "required": False}
    assert m.data["settings"]["console"]["where"] == "worker"
    assert m.warnings == []


def test_load_generates_record_caps(tmp_path):
    m = load(_folder(tmp_path, (ROOT / "valid" / "homework.yaml").read_text(), ["pages/bulletin.html"]))
    assert sorted(m.caps) == [f"homework.item.{v}" for v in
                              ("archive", "create", "get", "list", "search", "update")]
    assert m.caps["homework.item.list"]["risk"] == "read"
    assert m.caps["homework.item.create"]["risk"] == "write"
    assert m.caps["homework.item.list"]["on"] == "hub"


def test_record_delete_is_destructive():
    data = _yaml.loads("manifest: 1\nplugin: n\nversion: 1.0.0\n"
                       "records: { note: { fields: { t: {} }, caps: [delete], risk: { delete: admin } } }\n")
    from rook.kit.manifest import normalize
    cap = normalize(data)["caps"]["n.note.delete"]
    assert cap["risk"] == "admin" and cap["effects"] == ["destructive"]


def test_load_marks_unknown_action_unavailable(tmp_path):
    m = load(_folder(tmp_path, (ROOT / "valid" / "warnings.yaml").read_text(), ["scripts/open.py"]))
    assert m.caps["gizmo.serial.send"]["unavailable"] == "unknown action type: serial"
    assert "unavailable" not in m.caps["gizmo.zap"]
    assert {w.code for w in m.warnings} >= {"unknown-key", "unknown-action", "unknown-effect"}
    assert hold_effects(m.caps["gizmo.zap"]) == {"physical", "levitates", "destructive"}


def test_load_raises_with_all_issues(tmp_path):
    with pytest.raises(ManifestError) as e:
        load(_folder(tmp_path, (ROOT / "invalid" / "templates.yaml").read_text()))
    assert len([i for i in e.value.issues if i.level == "error"]) == 5


def test_missing_files_are_errors_with_a_folder(tmp_path):
    folder = _folder(tmp_path, (ROOT / "valid" / "homework.yaml").read_text())
    issues = validate(_yaml.loads((folder / "plugin.yaml").read_text()), folder)
    assert _codes(issues) == [{"level": "error", "path": "/pages/bulletin/file", "code": "missing-file"}]


def test_contracts_from_other_plugins():
    data = _yaml.loads("manifest: 1\nplugin: tg\nversion: 1.0.0\n"
                       "caps: { send: { doc: d, risk: write, scope: external, run: s.py:f,"
                       " args: { text: { type: str, required: true } } } }\n"
                       "provides: { notify.send: tg.send }\n")
    assert [i.code for i in validate(data)] == ["unknown-contract"]
    core = {"notify.send": {"args": {"text": {"type": "str", "required": True},
                                     "to": {"type": "str"}}}}
    assert [i.code for i in validate(data, contracts=core)] == ["contract-mismatch"]
    data["caps"]["send"]["args"]["to"] = {"type": "str"}
    assert validate(data, contracts=core) == []


def test_provides_by_a_record_cap_is_not_checked_against_args():
    data = _yaml.loads("manifest: 1\nplugin: t\nversion: 1.0.0\nrecords: { item: { fields: { x: {} } } }\n"
                       "contracts: { c.list: { args: { limit: { type: int } } } }\n"
                       "provides: { c.list: t.item.list }\n")
    assert validate(data) == []


def test_glob_match():
    assert glob_match("*", "a.b") and glob_match("phone.*", "phone.motion.started")
    assert glob_match("homework.item.*", "homework.item.list")
    assert not glob_match("phone.*", "phone") and not glob_match("phone.*", "phonebook.x")


def test_not_a_mapping():
    assert [i.code for i in validate([1, 2])] == ["not-a-mapping"]


# -- hashing and trust class ---------------------------------------------------

def test_canonical_json():
    assert canonical_json({"b": [1, "é"], "a": {"d": 1, "c": None}}) == \
        b'{"a":{"c":null,"d":1},"b":[1,"\\u00e9"]}'
    assert canonical_json(_yaml.loads(PIKVM)) == canonical_json(json.loads(json.dumps(_yaml.loads(PIKVM))))


def test_file_hashes_and_trust_class(tmp_path):
    _folder(tmp_path, PIKVM)
    (tmp_path / "plugin.sig").write_text("sig")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "manual.md").write_text("hi")
    hashes = file_hashes(tmp_path)
    assert list(hashes) == ["docs/manual.md", "plugin.yaml"]
    assert hashes["docs/manual.md"] == "8f434346648f6b96df89dda901c5176b10a6d83961dd3c1ac88b59b2dc327aa4"
    assert trust_class(tmp_path) == "config"
    (tmp_path / "pages").mkdir()
    (tmp_path / "pages" / "a.html").write_text("<p>")
    assert trust_class(tmp_path) == "page"
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "x.py").write_text("")
    assert trust_class(tmp_path) == "code"


def test_unknown_file_types_are_code(tmp_path):
    (tmp_path / "run.sh").write_text("")
    assert trust_class(tmp_path) == "code"


def test_symlinks_refused(tmp_path):
    (tmp_path / "a.md").write_text("")
    (tmp_path / "b.md").symlink_to(tmp_path / "a.md")
    with pytest.raises(ValueError):
        file_hashes(tmp_path)


# -- diff and semver -----------------------------------------------------------

BASE = """manifest: 1
plugin: d
version: {version}
caps:
  list:
    doc: List.
    risk: read
    scope: local
    args: {args}
    result: {result}
    run: s.py:f
{extra}"""


def _m(version="1.0.0", args="{ state: { type: str, enum: [a, b] } }",
       result="{ type: map, fields: { id: {}, name: {} } }", extra=""):
    return _yaml.loads(BASE.format(version=version, args=args, result=result, extra=extra))


def _kinds(old, new):
    return [c.kind for c in diff(old, new)]


def test_diff_compatible_changes():
    assert _kinds(_m(), _m()) == []
    # New optional arg, enum widened, result widened, a new cap.
    new = _m(args="{ state: { type: str, enum: [a, b, c] }, limit: { type: int } }",
             result="{ type: map, fields: { id: {}, name: {}, size: {} } }",
             extra="  more: { doc: M., risk: read, scope: local, run: s.py:g }\n")
    assert _kinds(_m(), new) == []


def test_diff_breaking_changes():
    assert _kinds(_m(), _m(args="{}")) == ["arg-removed"]
    assert _kinds(_m(), _m(args="{ state: { type: int } }")) == ["type-changed"]
    assert _kinds(_m(), _m(args="{ state: { type: str, enum: [a] } }")) == ["type-changed"]
    assert _kinds(_m(), _m(args="{ state: { type: str, enum: [a, b], required: true } }")) == ["arg-required"]
    assert _kinds(_m(), _m(args="{ state: { type: str, enum: [a, b] }, x: { required: true } }")) == ["arg-required"]
    assert _kinds(_m(), _m(result="{ type: map, fields: { id: {} } }")) == ["result-narrowed"]
    assert _kinds(_m(), _m(result="{ type: list }")) == ["type-changed"]
    gone = _yaml.loads("manifest: 1\nplugin: d\nversion: 2.0.0\n")
    assert _kinds(_m(), gone) == ["cap-removed"]


def test_diff_rename_with_alias_is_not_removal():
    renamed = _yaml.loads(BASE.format(version="1.1.0", args="{ state: { type: str, enum: [a, b] } }",
                                      result="{ type: map, fields: { id: {}, name: {} } }",
                                      extra="").replace("  list:", "  items:\n    aliases: [d.list]"))
    assert _kinds(_m(), renamed) == []


def test_check_bump():
    assert check_bump(_m(), _m(version="1.0.1")) == []
    assert [i.code for i in check_bump(_m(), _m(version="1.0.0"))] == ["version-not-increased"]
    assert [i.code for i in check_bump(_m(), _m(version="1.0.0-rc.1"))] == ["version-not-increased"]
    broken = _m(version="1.1.0", args="{}")
    assert [(i.code, i.path) for i in check_bump(_m(), broken)] == [("breaking-change", "/caps/d.list/args/state")]
    assert check_bump(_m(), _m(version="2.0.0", args="{}")) == []
    # 0.x: a minor bump may break, a patch may not.
    assert check_bump(_m(version="0.1.0"), _m(version="0.2.0", args="{}")) == []
    assert check_bump(_m(version="0.1.0"), _m(version="0.1.1", args="{}")) != []


def test_prerelease_ordering():
    from rook.kit.manifest.diff import semver_key
    order = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta.2", "1.0.0-beta.11",
             "1.0.0-rc.1", "1.0.0", "1.0.1", "1.10.0"]
    assert sorted(order, key=semver_key) == order


# -- CLI -----------------------------------------------------------------------

def test_cli_validate(tmp_path, capsys):
    (tmp_path / "good").mkdir()
    good = _folder(tmp_path / "good", PIKVM)
    assert main(["validate", str(good)]) == 0
    assert capsys.readouterr().out.strip() == "ok: pikvm 1.0.0, 2 cap(s), 1 file(s), trust class config"
    (tmp_path / "bad").mkdir()
    bad = _folder(tmp_path / "bad", (ROOT / "invalid" / "missing-version.yaml").read_text())
    assert main(["validate", str(bad)]) == 1
    assert "'version' is a required property [schema-required]" in capsys.readouterr().out


def test_cli_validate_takes_the_yaml_path(tmp_path, capsys):
    _folder(tmp_path, PIKVM)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "x.md").write_text("")
    assert main(["validate", str(tmp_path / "plugin.yaml")]) == 0
    assert "2 file(s)" in capsys.readouterr().out


def test_cli_diff(tmp_path, capsys):
    for name, version, args in (("a", "1.0.0", "{ state: {} }"), ("b", "1.1.0", "{}")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "plugin.yaml").write_text(
            BASE.format(version=version, args=args, result="{ type: str }", extra=""))
        (tmp_path / name / "s.py").write_text("")
    assert main(["diff", str(tmp_path / "a"), str(tmp_path / "b")]) == 1
    out = capsys.readouterr().out
    assert "arg-removed: /caps/d.list/args/state" in out and "[breaking-change]" in out
