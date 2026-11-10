"""One public base URL decides http vs https for everything the hub hands out:
a LAN quickstart hub (plain HTTP on <ip>:7005) gets http:// installers, a
public hub behind a TLS proxy keeps https:// exactly as before."""

import base64

import pytest
from aiohttp.test_utils import TestClient, TestServer

from rook.worker import enroll

LAN = "192.0.2.10"   # TEST-NET-1: stands in for a LAN address in rendered scripts
PRIVATE = "10.0.0.5"
AUTH = "Basic " + base64.b64encode(b"owner:password").decode()


@pytest.mark.parametrize("server,ok", [
    ("https://rook.example.com", True),
    (f"http://{PRIVATE}:7005", True),
    ("http://127.0.0.1:7005", True),
    ("http://localhost:7005", True),
    ("http://rook.example.com", False),     # a public name never gets plain HTTP
    ("http://100.64.0.1:7005", True),       # CGNAT / tailnet
    ("http://11.0.0.1:7005", False),        # nor a public address
    ("http://0.0.0.0:7005", False),
    ("ftp://10.0.0.5", False),
    ("https://rook.example.com/path", False),
])
def test_worker_enrollment_origin(server, ok):
    if ok:
        enroll.check_origin(server)
    else:
        with pytest.raises(ValueError):
            enroll.check_origin(server)


# -- cookies and the dashboard -> account bridge ------------------------------

def _set_cookies(response):
    return {c.split("=", 1)[0]: c for c in response.headers.getall("Set-Cookie", [])}


async def _dashboard_login(client):
    response = await client.post("/login", data={"user": "owner", "pass": "password"},
                                 allow_redirects=False)
    assert response.status == 302
    return response


