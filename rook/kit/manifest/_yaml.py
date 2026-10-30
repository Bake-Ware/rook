"""The YAML subset manifests are written in: plain YAML that maps 1:1 to JSON.

PyYAML speaks YAML 1.1, where ``on``, ``off``, ``yes`` and ``no`` are
booleans (the pikvm ``enum: [on, off]`` and ``hooks: on:`` would break),
``1:30`` is the integer 90 and ``2026-10-30`` is a date. This loader resolves
plain scalars like YAML 1.2's core schema instead, and refuses what has no
JSON equivalent or makes a hub validating untrusted files do unbounded work:
anchors and aliases (so merge keys too), custom tags, duplicate keys, and
keys that aren't strings.
"""

from __future__ import annotations

import re

import yaml


MAX_DEPTH = 64


class YamlError(ValueError):
    """The text is not in the manifest YAML subset."""


class _Loader(yaml.SafeLoader):
    pass


# Drop every implicit resolver (YAML 1.1 rules) and install YAML 1.2 core.
_Loader.yaml_implicit_resolvers = {}
for _tag, _regexp, _first in (
    ("tag:yaml.org,2002:null", r"^(?:~|null|Null|NULL|)$", list("~nN") + [""]),
    ("tag:yaml.org,2002:bool", r"^(?:true|True|TRUE|false|False|FALSE)$", list("tTfF")),
    ("tag:yaml.org,2002:int", r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$",
     list("-+0123456789")),
    ("tag:yaml.org,2002:float",
     r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
     r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$", list("-+0123456789.")),
):
    _Loader.add_implicit_resolver(_tag, re.compile(_regexp), _first)


def _int(loader, node):
    text = loader.construct_scalar(node)
    if text.startswith("0o"):
        return int(text[2:], 8)
    if text.startswith("0x"):
        return int(text[2:], 16)
    return int(text)


def _float(loader, node):
    text = loader.construct_scalar(node).lower()
    if text.endswith("inf"):
        return float("-inf") if text.startswith("-") else float("inf")
    return float(text)


def _mapping(loader, node):
    out = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            raise YamlError(f"line {key_node.start_mark.line + 1}: key {key!r} must be a string")
        if key in out:
            raise YamlError(f"line {key_node.start_mark.line + 1}: duplicate key {key!r}")
        out[key] = loader.construct_object(value_node, deep=True)
    return out


_Loader.add_constructor("tag:yaml.org,2002:int", _int)
_Loader.add_constructor("tag:yaml.org,2002:float", _float)
_Loader.add_constructor("tag:yaml.org,2002:map", _mapping)

_STANDARD_TAGS = {f"tag:yaml.org,2002:{t}" for t in ("null", "bool", "int", "float", "str", "seq", "map")}


def _check_nodes(node) -> None:
    stack = [node]
    while stack:
        n = stack.pop()
        if n.tag not in _STANDARD_TAGS:
            raise YamlError(f"line {n.start_mark.line + 1}: tag {n.tag} is not allowed")
        if isinstance(n, yaml.SequenceNode):
            stack.extend(n.value)
        elif isinstance(n, yaml.MappingNode):
            for k, v in n.value:
                stack.extend((k, v))


def loads(text: str):
    """Parse one manifest document into plain JSON data."""
    try:
        # Aliases and deep nesting are refused before composing, so an alias
        # bomb never expands and PyYAML's parse (quadratic in depth) stops early.
        depth = 0
        for event in yaml.parse(text, Loader=_Loader):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise YamlError(f"line {event.start_mark.line + 1}: anchors and aliases are not allowed")
            if isinstance(event, yaml.CollectionStartEvent):
                depth += 1
                if depth > MAX_DEPTH:
                    raise YamlError(f"line {event.start_mark.line + 1}: nested deeper than {MAX_DEPTH}")
            elif isinstance(event, yaml.CollectionEndEvent):
                depth -= 1
        loader = _Loader(text)
        try:
            node = loader.get_single_node()
            if node is None:
                return None
            _check_nodes(node)
            return loader.construct_document(node)
        finally:
            loader.dispose()
    except yaml.YAMLError as e:
        raise YamlError(str(e)) from None
