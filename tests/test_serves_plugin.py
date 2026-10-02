"""The serves hub plugin: hand-written hosting metadata per worker."""
import pytest

from rook.band_mcp import roster
from rook.hub.plugins.serves import Serves


@pytest.fixture
def serves(tmp_path):
    p = Serves()
    p.__dict__["_data_root"] = str(tmp_path)
    return p


def test_set_normalises_keeps_omitted_lists_and_persists(serves, tmp_path):
    out = serves.set("Kaiju", sites=["https://strata.example.com/", {"name": "Voice", "url": "https://voice.example.com"},
                                     "https://strata.example.com/"], by="tunnel sync")
    assert out["worker"] == "kaiju" and out["by"] == "tunnel sync"
    assert out["sites"] == [{"name": "strata.example.com", "url": "https://strata.example.com/"},
                            {"name": "Voice", "url": "https://voice.example.com"}]
    serves.set("kaiju", services=[{"name": "llm front door", "url": "http://10.0.0.5:1234", "note": "OpenAI API"}])
    again = Serves()
    again.__dict__["_data_root"] = str(tmp_path)  # a restart reads the same file
    entry = again.list("KAIJU")["serves"]["kaiju"]
    assert len(entry["sites"]) == 2 and entry["services"][0]["note"] == "OpenAI API"
    assert again.lookup("Kaiju") == {"sites": entry["sites"], "services": entry["services"]}
    assert again.lookup("nobody") is None and again.lookup(None) is None


def test_bad_input_is_refused_and_clear_removes(serves):
    with pytest.raises(ValueError, match="sites, services or both"):
        serves.set("kaiju")
    with pytest.raises(ValueError, match="worker is required"):
        serves.set(" ", sites=[])
    with pytest.raises(ValueError, match="must be a list"):
        serves.set("kaiju", sites="https://a.example.com")
    with pytest.raises(ValueError, match="name or a url"):
        serves.set("kaiju", sites=[{}])
    with pytest.raises(ValueError, match="at most"):
        serves.set("kaiju", sites=[f"https://{n}.example.com" for n in range(61)])
    serves.set("kaiju", sites=["https://a.example.com"])
    serves.set("kaiju", sites=[])  # an empty list empties it; the entry stays
    assert serves.lookup("kaiju") is None and "kaiju" in serves.list()["serves"]
    assert serves.clear("kaiju") == {"worker": "kaiju", "removed": True}
    assert serves.clear("kaiju")["removed"] is False and serves.list()["serves"] == {}


def test_roster_rows_carry_serves_only_when_set():
    workers = {"w1": {"worker_id": "w1", "name": "kaiju", "last_seen": 100.0},
               "w2": {"worker_id": "w2", "name": "idle", "last_seen": 100.0}}
    hosting = {"kaiju": {"sites": [{"name": "a", "url": "https://a.example.com"}]}}
    rows = roster.workers_view(workers, now=101.0, serves=hosting.get)
    by = {r["name"]: r for r in rows}
    assert by["kaiju"]["serves"] == hosting["kaiju"] and "serves" not in by["idle"]
    assert roster.workers_view(workers, now=101.0, name="idle", fields="name,serves") == [{"name": "idle", "serves": {}}]
    assert "serves" not in roster.workers_view(workers, now=101.0)[0]  # no plugin: no field
