"""Routing lines in the voice's replies (rook/agents/routing.py): never spoken,
however the stream is split; ops parsed."""

import pytest

from rook.agents.routing import MAX_OPS, RoutingFilter, strip

CASES = [
    ('Let me check.\n<<flow {"op":"queue","request":"weather in x >> y"}>>', "Let me check.",
     [{"op": "queue", "request": "weather in x >> y"}]),
    ('On it. ``<<flow {"op":"status"}>>``', "On it.", [{"op": "status"}]),
    ('Sure <<flow {"op":"mode","mode":"dictate"}>> switching now.', "Sure switching now.",
     [{"op": "mode", "mode": "dictate"}]),
    ("a < b and c << d", "a < b and c << d", []),
    ("use `code` here", "use `code` here", []),
    ('Ok.\n<<flow {"op":"queue","request":"x"}', "Ok.", [{"op": "queue", "request": "x"}]),
    ('Ok.\n<<flow {bad json}>>', "Ok.", []),
    ("Ok <<fl", "Ok", []),
    ('Two. <<flow {"op":"mode","mode":"listen"}>>\n<<flow {"op":"queue","request":"r"}>>',
     "Two.", [{"op": "mode", "mode": "listen"}, {"op": "queue", "request": "r"}]),
    ('<<flow {"op":"quiet"}>>', "", [{"op": "quiet"}]),
]


def norm(text):
    return " ".join(text.split())


@pytest.mark.parametrize("text,spoken,ops", CASES)
def test_every_split_of_the_stream(text, spoken, ops):
    for n in range(1, len(text) + 1):
        f = RoutingFilter()
        out = "".join(f.feed(text[i:i + n]) for i in range(0, len(text), n)) + f.finish()
        assert norm(out) == norm(spoken), (n, out)
        assert "flow" not in out or "flow" in spoken
        assert f.ops == ops
    for k in range(len(text) + 1):
        f = RoutingFilter()
        out = f.feed(text[:k]) + f.feed(text[k:]) + f.finish()
        assert norm(out) == norm(spoken) and f.ops == ops
    assert strip(text) == (spoken, ops)


def test_bad_lines_are_counted_and_ops_capped():
    f = RoutingFilter()
    f.feed("x " + "".join(f'<<flow {{"op":"status","n":{i}}}>>' for i in range(MAX_OPS + 2)))
    f.feed('<<flow {"no_op": 1}>> y')
    assert norm(f.feed("") + f.finish()) == "" and len(f.ops) == MAX_OPS
    assert f.dropped == 2 and f.bad == 1


def test_an_unterminated_line_never_leaks():
    f = RoutingFilter()
    out = f.feed("Okay. <<flow {\"op\": \"queue\", \"request\": \"a very long ")
    out += f.feed("request that never ends") + f.finish()
    assert norm(out) == "Okay." and f.ops == [] and f.bad == 1
