"""Rook protocol v1 against the shared vectors (W1, W2, U8)."""

import json
import pathlib

import pytest

from rook_protocol import PROTOCOL, ProtocolError, decode, encode, validate

VECTORS = json.loads((pathlib.Path(__file__).parent.parent / "protocol" / "vectors.json").read_text("utf-8"))


@pytest.mark.parametrize("vector", VECTORS["valid"], ids=lambda v: v["name"])
def test_valid_vector_encodes_to_its_bytes_and_back(vector):
    assert encode(vector["message"]) == bytes.fromhex(vector["hex"])
    assert decode(bytes.fromhex(vector["hex"])) == vector["message"]


@pytest.mark.parametrize("vector", VECTORS["invalid"], ids=lambda v: v["name"])
def test_invalid_vector_is_rejected(vector):
    with pytest.raises(ProtocolError):
        decode(bytes.fromhex(vector["hex"]))


def test_vectors_are_this_version():
    assert VECTORS["protocol"] == PROTOCOL == 1


def test_every_message_type_has_a_vector():
    from rook_protocol import MESSAGES
    assert {v["message"]["type"] for v in VECTORS["valid"]} == set(MESSAGES)


def test_minimum_worker_needs_no_stream_messages():
    # W2: register, heartbeat, answer calls. The ESP32 vector is a whole
    # heartbeat with only the fields an ESP32 can fill.
    esp32 = next(v["message"] for v in VECTORS["valid"] if v["name"] == "heartbeat, ESP32 worker")
    assert "streams" not in esp32
    assert set(esp32["health"]) == {"heap_free", "uptime"}


def test_unknown_fields_are_kept_not_rejected():
    # U8: a newer worker's extra fields must not stop an older hub decoding it.
    message = {"type": "heartbeat_reply", "protocol": 2, "interval": 15, "later_field": {"x": 1}}
    assert decode(encode(message)) == message
    heartbeat = next(v["message"] for v in VECTORS["valid"] if v["name"] == "heartbeat, Linux worker")
    newer = json.loads(json.dumps(heartbeat))
    newer["health"]["fan_rpm"] = 1200
    newer["facts"]["kernel"] = "6.18"
    assert validate(newer) is newer


@pytest.mark.parametrize("name", ["shell.exec", "wake-on-lan.send", "echo", "gpu.vram-free"])
def test_cap_names_accepted(name):
    validate({"type": "call", "id": "1", "cap": name, "args": {}})


@pytest.mark.parametrize("name", ["Shell.exec", "shell..exec", "shell.exec-", "-shell", "shell_exec", "", "shell.exec."])
def test_cap_names_rejected(name):
    with pytest.raises(ProtocolError, match="cap name"):
        validate({"type": "call", "id": "1", "cap": name, "args": {}})


def test_encode_refuses_what_json_cannot_carry():
    with pytest.raises(ProtocolError):
        encode({"type": "result", "id": "1", "result": float("nan")})
    with pytest.raises(ProtocolError):
        encode({"type": "result", "id": "1", "result": b"bytes"})


def test_stream_data_must_be_base64():
    with pytest.raises(ProtocolError, match="base64"):
        validate({"type": "stream_data", "stream": "s1", "data": "not base64!"})
