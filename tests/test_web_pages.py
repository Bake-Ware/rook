"""Server-rendered pages come from static files in rook/web and keep their
forms, CSRF fields and escaping."""
import re

import pytest
from aiohttp.test_utils import TestClient, TestServer

from rook.remote import setup_store
from rook.remote.web_pages import render, WEB_DIR
from test_setup_auth import AUTH, SETUP, env  # noqa: F401  (fixture)

HTML = {"Accept": "text/html"}


def test_render_escapes_once_and_requires_every_slot():
    out = render("account-page.html", title='<b>"x"</b>', body="<p>{{title}}</p>")
    assert "&lt;b&gt;&quot;x&quot;&lt;/b&gt; · Rook" in out
    assert "<p>{{title}}</p>" in out          # a value is never expanded again
    with pytest.raises(KeyError):
        render("account-page.html", title="x")


@pytest.mark.parametrize("name", ["login.html", "setup.html", "permissions.html", "account-page.html"])
def test_pages_use_the_shared_theme_and_have_no_inline_palette(name):
    text = (WEB_DIR / name).read_text(encoding="utf-8")
    assert "/account/bands/assets/theme.css" in text
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", text)   # colours come from theme.css


@pytest.mark.asyncio
async def test_login_page_form_and_error(env):  # noqa: F811
    setup_store.save(SETUP)
    server = env(web_user="owner", web_pass="test-password-1")
    async with TestClient(TestServer(server._app)) as client:
        page = await (await client.get("/login")).text()
        assert '<form method="POST" action="/login">' in page
        assert 'name="user"' in page and 'name="pass" type="password"' in page
        assert 'href="/account/login"' in page and "error" not in page
        response = await client.post("/login", data={"user": "owner", "pass": "wrong"})
        assert response.status == 200 and "username and password are not right" in await response.text()
        # The stylesheet the page links is public, or a signed-out browser
        # would get an unstyled login form.
        css = await client.get("/account/bands/assets/theme.css")
        assert css.status == 200 and "--accent" in await css.text()


@pytest.mark.asyncio
async def test_password_only_hub_asks_for_just_the_password(env):  # noqa: F811
    """The installers set a password and no username (one operator): the
    first sign-in is the password alone, the username field hidden."""
    setup_store.save(SETUP)
    server = env(web_user="", web_pass="test-password-1")
    async with TestClient(TestServer(server._app)) as client:
        page = await (await client.get("/login")).text()
        assert "Username" not in page
        assert '<input type="hidden" name="user"' in page
        assert 'name="pass" type="password" required autocomplete="current-password" autofocus' in page
        assert "Enter the dashboard password" in page and "{{" not in page
        response = await client.post("/login", data={"pass": "wrong"})
        assert "That password is not right." in await response.text()
        response = await client.post("/login", data={"user": "admin", "pass": "test-password-1"},
                                     allow_redirects=False)
        assert response.status == 302


@pytest.mark.asyncio
async def test_setup_page_keeps_csrf_and_escapes_values(env):  # noqa: F811
    server = env(bind="127.0.0.1", band_name='"><script>x</script>')
    async with TestClient(TestServer(server._app)) as client:
        page = await (await client.get("/setup")).text()
        assert f'<input type="hidden" name="csrf" value="{server._setup_csrf}">' in page
        assert "<script>x</script>" not in page and "&quot;&gt;&lt;script&gt;" in page
        for field in ("band_name", "hub_public", "pyz_domain", "band_psk"):
            assert f'name="{field}"' in page
        response = await client.post("/setup", data={**SETUP, "csrf": "stale"})
        assert response.status == 400 and "The form expired" in await response.text()
        response = await client.post("/setup", data={**SETUP, "hub_public": "",
                                                     "csrf": server._setup_csrf})
        assert response.status == 200 and "are all required" in await response.text()
        response = await client.post("/setup", data={**SETUP, "csrf": server._setup_csrf},
                                     allow_redirects=False)
        assert response.status == 302 and response.headers["Location"] == "/"


@pytest.mark.asyncio
async def test_permissions_page_escapes_policy_values(env, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ROOK_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    server = env(web_user="owner", web_pass="test-password-1")
    setup_store.save({k: SETUP[k] for k in ("band_name", "band_psk", "hub_public")})
    async with TestClient(TestServer(server._app)) as client:
        from rook.hub.policy import DEFAULT_POLICY
        doc = {**DEFAULT_POLICY, "note": "</textarea><script>bad()</script>"}
        assert (await client.post("/api/policy", headers=AUTH, json={"policy": doc})).status == 200
        page = await (await client.get("/permissions", headers=AUTH)).text()
        assert "<script>bad()</script>" not in page
        assert "&lt;/textarea&gt;&lt;script&gt;bad()" in page
        assert 'id="mode">audit<' in page and 'id="rev">1<' in page
        assert "{{" not in page


@pytest.mark.asyncio
async def test_account_document_shell(env):  # noqa: F811
    setup_store.save(SETUP)
    server = env(web_user="owner", web_pass="test-password-1")
    async with TestClient(TestServer(server._app)) as client:
        response = await client.get("/account/login")
        page = await response.text()
        assert response.status == 200 and "<title>Sign in · Rook</title>" in page
        assert '<form method="post" action="/account/login">' in page
        assert 'name="form_token"' in page and 'class="standalone-account"' in page
        assert response.headers["Cache-Control"] == "no-store"
