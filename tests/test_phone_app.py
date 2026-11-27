"""The phone app's public page and APK (brief AP1): GET /app and GET /app/rook.apk with no login,
and the hub's address stamped into the APK it hands out.

The stamp is checked against a small APK made here as a signer leaves one: a zip with an APK
Signing Block before its central directory (a stand-in signature pair and apksigner's padding
pair). It and its stamped copy are kept in android/app/src/test/resources/stamp/, where the app's
own unit test (HubStampTest.kt) reads the address back with the app's reader."""

import asyncio
import io
import pathlib
import zipfile

import aiohttp

from rook_hub import Hub, site
from test_site import browser, login

FIXTURES = pathlib.Path(__file__).parent.parent / "android" / "app" / "src" / "test" / "resources" / "stamp"
ADDRESS = "http://10.0.2.2:8123"            # what the emulator calls the machine it runs on
SIGNATURE_V2 = 0x7109871a


def fake_apk():
    """A zip of one entry, with a signing block holding a stand-in v2 signature, padded to 4096 bytes."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as apk:
        apk.writestr(zipfile.ZipInfo("AndroidManifest.xml", date_time=(2026, 11, 26, 0, 0, 0)), b"<manifest/>")
    data = out.getvalue()
    end = data.rfind(b"PK\x05\x06")
    directory = int.from_bytes(data[end + 16:end + 20], "little")
    pairs = [(SIGNATURE_V2, b"a stand-in for the v2 signature")]
    block = site._signing_block(pairs)
    block = site._signing_block(pairs + [(site.VERITY_PADDING_ID, bytes(-(len(block) + 12) % 4096))])
    return data[:directory] + block + data[directory:end + 16] + (directory + len(block)).to_bytes(4, "little") + data[end + 20:]


def pairs_of(apk):
    """{id: value} of the signing block's pairs, and the block's length."""
    end = apk.rfind(b"PK\x05\x06")
    directory = int.from_bytes(apk[end + 16:end + 20], "little")
    assert apk[directory - 16:directory] == site.APK_SIG_MAGIC
    size = int.from_bytes(apk[directory - 24:directory - 16], "little")
    found, at = {}, directory - size - 8 + 8
    while at < directory - 24:
        length = int.from_bytes(apk[at:at + 8], "little")
        found[int.from_bytes(apk[at + 8:at + 12], "little")] = apk[at + 12:at + 8 + length]
        at += 8 + length
    return found, size + 8


def test_the_stamp_goes_in_the_signing_block_and_the_app_reads_it_back():
    unstamped = fake_apk()
    # The fixtures the app's test reads are these bytes: made again here, they come out the same.
    assert (FIXTURES / "unstamped.apk").read_bytes() == unstamped
    stamped = site.stamp_apk(unstamped, ADDRESS)
    assert (FIXTURES / "stamped.apk").read_bytes() == stamped

    found, length = pairs_of(stamped)
    assert found[site.HUB_ADDRESS_ID] == ADDRESS.encode() and found[SIGNATURE_V2] == b"a stand-in for the v2 signature"
    assert length % 4096 == 0                                          # still padded as apksigner pads it
    # The zip is the same zip: its entries read, and nothing before the signing block moved.
    with zipfile.ZipFile(io.BytesIO(stamped)) as apk:
        assert apk.read("AndroidManifest.xml") == b"<manifest/>"
    assert stamped[:30] == unstamped[:30]

    # Stamped again (another hub's copy of the same file), the new address replaces the old one.
    again, length = pairs_of(site.stamp_apk(stamped, "https://rook.example.com"))
    assert again[site.HUB_ADDRESS_ID] == b"https://rook.example.com" and length % 4096 == 0
    assert set(again) == {SIGNATURE_V2, site.HUB_ADDRESS_ID, site.VERITY_PADDING_ID}


def test_an_apk_with_no_signing_block_is_handed_out_as_it_is():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as apk:
        apk.writestr("AndroidManifest.xml", b"<manifest/>")
    assert site.stamp_apk(out.getvalue(), ADDRESS) == out.getvalue()
    assert site.stamp_apk(b"not a zip", ADDRESS) == b"not a zip"


def test_the_public_page_and_the_apk(tmp_path):
    asyncio.run(public(tmp_path))


async def public(tmp_path):
    hub = Hub(tmp_path / "hub")
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    hub.auth.create_user("Alice", "correct horse", first=True)
    file = tmp_path / "hub" / "app" / "rook.apk"
    try:
        async with aiohttp.ClientSession() as client:        # no session: nobody signed in
            # 1. /app is a page anyone can open: the QR code and button are drawn by its script.
            async with client.get(base + "/app") as response:
                page = await response.text()
                assert response.status == 200 and response.content_type == "text/html"
                assert response.headers["Cache-Control"] == "no-store"         # kept nowhere (AP6)
                assert "/site/phone-app.js" in page and '<script type="importmap">' in page
                assert "/site/style.css?v=" in page                     # its files at their versions (HA5)
            # 2. With no APK at <data>/app/rook.apk, the page and the file say so. Where it goes names
            #    the server's folders, so only a person signed in is told (AP6).
            async with client.get(base + "/app", headers={"Accept": "application/json"}) as response:
                assert response.status == 200 and await response.json() == {"apk": False}
                assert response.headers["Cache-Control"] == "no-store"
            async with client.get(base + "/app/rook.apk") as response:
                assert response.status == 404 and response.headers["Cache-Control"] == "no-store"
                assert await response.json() == {"code": "no_app", "message": "This hub has no app file yet.",
                                                 "next": "Whoever runs this hub can put the app on it; "
                                                         "Settings > Phone app says where."}
                assert str(tmp_path) not in await response.text()
            async with browser() as signed:
                assert (await login(signed, base, "Alice", "correct horse"))[0] == 200
                async with signed.get(base + "/app", headers={"Accept": "application/json"}) as response:
                    assert await response.json() == {"apk": False, "file": str(file)}
                async with signed.get(base + "/app/rook.apk") as response:
                    assert response.status == 404 and (await response.json())["next"] == f"Put the APK at {file}."
            # 3. With one there: it is handed out with this hub's address, as the asker reached it.
            file.parent.mkdir()
            file.write_bytes(fake_apk())
            async with client.get(base + "/app", headers={"Accept": "application/json"}) as response:
                assert await response.json() == {"apk": True}
            async with client.get(base + "/app/rook.apk") as response:
                assert response.status == 200 and response.content_type == "application/vnd.android.package-archive"
                assert response.headers["Cache-Control"] == "no-store"      # Cloudflare keeps .apk files otherwise
                assert response.headers["Content-Disposition"] == 'attachment; filename="rook.apk"'
                assert await response.read() == site.stamp_apk(fake_apk(), base)
            # Behind a proxy: the scheme from X-Forwarded-Proto, the host from Host.
            async with client.get(base + "/app/rook.apk", headers={"Host": "rook.example.com",
                                                                     "X-Forwarded-Proto": "https"}) as response:
                assert pairs_of(await response.read())[0][site.HUB_ADDRESS_ID] == b"https://rook.example.com"
            async with client.get(base + "/app/rook.apk", headers={"Host": "10.0.2.2:9999"}) as response:
                assert pairs_of(await response.read())[0][site.HUB_ADDRESS_ID] == b"http://10.0.2.2:9999"
            assert file.read_bytes() == fake_apk()                       # the file itself is left as it was
    finally:
        await hub.stop_serving()
        hub.close()
