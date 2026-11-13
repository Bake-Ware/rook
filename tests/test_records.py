"""Records (K1-K7): the brief's eight-step scenario, called directly as plugin code would."""

import uuid

import pytest

from helpers import Clock
from rook_hub import Hub, records
from rook_hub.errors import HubError

PHOTO, MANUAL = b"\x89PNG not really a picture", b"%PDF not really a manual"


def blob_files(state_dir):
    return sorted(p.name for p in (state_dir / "blobs").iterdir())


def test_records(tmp_path):
    clock = Clock()
    state = tmp_path / "hub"
    hub = Hub(state, clock=clock)
    try:
        alice = hub.auth.create_user("Alice", "a password")
        agent = hub.auth.mint("agent", user=alice["guid"])
        agent_guid = hub.auth.verify(agent)["guid"]

        # 1. A record of an undeclared type is refused, saying to declare it. `note`
        #    is declared for plugin `notes`; declaring it for `other` is refused (K2).
        with pytest.raises(HubError) as refused:
            records.create(hub, "note", "Shopping", user=alice["guid"])
        assert refused.value.message["code"] == "unknown_type" and "Declare the type" in refused.value.message["next"]
        assert records.declare_type(hub, "note", "notes") == {"type": "note", "plugin": "notes"}
        assert records.declare_type(hub, "note", "notes") == {"type": "note", "plugin": "notes"}   # again: no change
        with pytest.raises(HubError) as taken:
            records.declare_type(hub, "note", "other")
        assert taken.value.message["code"] == "type_taken" and taken.value.message["next"]
        assert records.list_types(hub) == [{"type": "blob", "plugin": "core"}, {"type": "note", "plugin": "notes"}]

        # 2. Alice directly and an agent holding Alice's token each create a note (K1, G1).
        shopping = records.create(hub, "note", "Shopping", "for the weekend",
                                  {"items": ["milk", "bread"]}, user=alice["guid"])
        clock.advance(1)
        trip = records.create(hub, "note", "Trip", body={"plan": "train to the coast"}, token=agent)
        for note, token in ((shopping, None), (trip, agent_guid)):
            assert str(uuid.UUID(note["guid"])) == note["guid"] and len(note["moniker"].split("-")) == 3
            assert (note["type"], note["owner"], note["creator"], note["creator_token"]) == (
                "note", alice["guid"], alice["guid"], token)
            assert note["created_at"] == note["updated_at"] is not None
            assert records.get(hub, note["guid"]) == records.get(hub, note["moniker"]) == note
        assert shopping["body"] == {"items": ["milk", "bread"]} and trip["description"] == ""

        # 3. An update changes the title and body; the updated time moves, the created time does not.
        clock.advance(5)
        shopping = records.update(hub, shopping["moniker"], title="Groceries", body={"items": ["milk", "eggs"]})
        assert (shopping["title"], shopping["body"], shopping["description"]) == (
            "Groceries", {"items": ["milk", "eggs"]}, "for the weekend")
        assert shopping["updated_at"] == shopping["created_at"] + 6

        # 4. Search by type, by text, and both (K2, K7). "coast" is only in the trip's body;
        #    the old body's words left the index with the update.
        def found(**query):
            return [r["guid"] for r in records.search(hub, **query)]
        assert found(type="note") == [shopping["guid"], trip["guid"]]          # most recently updated first
        assert found(text="coast") == [trip["guid"]]
        assert found(text="bread") == []
        assert found(text="GROCERIES") == [shopping["guid"]]                     # titles, any case
        assert found(type="note", text="milk") == [shopping["guid"]]
        assert found(type="blob", text="milk") == []
        assert found(text="items") == []                                         # keys are not text

        # 5. Bytes stored as a blob: the body holds hash, size and MIME type, and the bytes
        #    read back. The same bytes again give a second record and one file (K3).
        photo = records.store_blob(hub, PHOTO, "image/png", "shelf", user=alice["guid"])
        assert photo["type"] == "blob" and photo["creator"] == alice["guid"]
        assert photo["body"] == {"hash": photo["body"]["hash"], "size": len(PHOTO), "mime": "image/png"}
        assert records.read_blob(hub, photo["moniker"]) == PHOTO
        manual = records.store_blob(hub, MANUAL, "application/pdf", token=agent)
        again = records.store_blob(hub, MANUAL, "application/pdf", token=agent)
        assert again["guid"] != manual["guid"] and again["body"] == manual["body"]
        assert blob_files(state) == sorted({photo["body"]["hash"], manual["body"]["hash"]})
        assert found(type="note") == [shopping["guid"], trip["guid"]]          # no blob among the notes
        with pytest.raises(HubError) as not_blob:
            records.read_blob(hub, trip["guid"])
        assert not_blob.value.message["code"] == "not_a_blob"

        # 6. Unlinked blobs are listed; linking a note to one leaves the others (K4).
        #    The link shows from both ends (K3).
        def unlinked():
            return [r["guid"] for r in records.unlinked_blobs(hub)]
        assert unlinked() == [photo["guid"], manual["guid"], again["guid"]]
        records.link(hub, shopping["guid"], photo["moniker"])
        records.link(hub, shopping["guid"], photo["guid"])                         # again: no change
        assert unlinked() == [manual["guid"], again["guid"]]
        assert [r["guid"] for r in records.links(hub, shopping["guid"])["to"]] == [photo["guid"]]
        assert [r["guid"] for r in records.links(hub, photo["guid"])["from"]] == [shopping["guid"]]
        assert records.links(hub, photo["guid"])["to"] == records.links(hub, shopping["guid"])["from"] == []
        records.link(hub, trip["guid"], manual["guid"])
        records.unlink(hub, trip["guid"], manual["guid"])
        assert records.links(hub, trip["guid"]) == {"to": [], "from": []}

        # 7. The note is deleted: its links go, the blob is unlinked again, nothing else is removed (K4).
        records.delete(hub, shopping["moniker"])
        with pytest.raises(HubError) as gone:
            records.get(hub, shopping["guid"])
        assert gone.value.message["code"] == "unknown_record" and gone.value.message["next"]
        assert records.links(hub, photo["guid"]) == {"to": [], "from": []}
        assert unlinked() == [photo["guid"], manual["guid"], again["guid"]]
        assert found(type="note") == [trip["guid"]] and found(text="milk") == []
        assert records.read_blob(hub, photo["guid"]) == PHOTO
    finally:
        hub.close()

    # 8. The hub opens again on the same directory: records, links, blobs and search still work (K7).
    hub = Hub(state, clock=clock)
    try:
        assert records.get(hub, trip["moniker"]) == trip
        assert records.read_blob(hub, again["guid"]) == MANUAL
        assert [r["guid"] for r in records.search(hub, text="coast")] == [trip["guid"]]
        records.link(hub, trip["guid"], again["guid"])
        assert [r["guid"] for r in records.unlinked_blobs(hub)] == [photo["guid"], manual["guid"]]
        assert records.list_types(hub) == [{"type": "blob", "plugin": "core"}, {"type": "note", "plugin": "notes"}]
    finally:
        hub.close()


def test_record_errors(tmp_path):
    # Each refusal says what to try next (C3), and nothing is written.
    hub = Hub(tmp_path / "hub")
    try:
        alice = hub.auth.create_user("Alice", "a password")["guid"]
        records.declare_type(hub, "note", "notes")
        blob = records.store_blob(hub, b"bytes", "text/plain", "a blob", user=alice)
        for call, code in ((lambda: records.create(hub, "note", None, user=alice), "bad_message"),
                           (lambda: records.create(hub, "note", "x", body={1, 2}, user=alice), "bad_message"),
                           (lambda: records.create(hub, "note", "x"), "no_actor"),
                           (lambda: records.create(hub, "note", "x", owner="nobody", user=alice), "unknown_user"),
                           (lambda: records.store_blob(hub, "text", "text/plain", user=alice), "bad_message"),
                           (lambda: records.update(hub, "no-such-record", title="x"), "unknown_record"),
                           (lambda: records.link(hub, "no-such-record", "nor-this"), "unknown_record"),
                           (lambda: records.declare_type(hub, "", "notes"), "bad_message"),
                           # K3: a blob record always has bytes behind it.
                           (lambda: records.create(hub, "blob", "x", body={"hash": "0" * 64}, user=alice),
                            "blob_needs_bytes"),
                           (lambda: records.update(hub, blob["guid"], body={"hash": "0" * 64}), "blob_body_fixed")):
            with pytest.raises(HubError) as refused:
                call()
            assert refused.value.message["code"] == code and refused.value.message["next"]
        assert [r["guid"] for r in records.search(hub)] == [blob["guid"]]
        assert records.get(hub, blob["guid"]) == blob and records.read_blob(hub, blob["guid"]) == b"bytes"

        # A blob's title and description may still change.
        renamed = records.update(hub, blob["moniker"], title="renamed", description="still the same bytes")
        assert (renamed["title"], renamed["body"]) == ("renamed", blob["body"])

        # The owner is the acting user unless another user is named.
        bob = hub.auth.create_user("Bob", "a password")
        note = records.create(hub, "note", "For Bob", owner=bob["moniker"], user=alice)
        assert (note["owner"], note["creator"]) == (bob["guid"], alice)
    finally:
        hub.close()
