"""The controller denylist is written to disk on unban.

The handler referenced ``setup_store`` without importing it, so the save
raised NameError inside its own ``try`` and the ban list silently never
persisted across a dashboard restart.
"""

import json
from types import SimpleNamespace

import pytest

from rook.remote import setup_store
from rook.remote.bootstrap import CombinedServer


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.mark.asyncio
async def test_unban_persists_the_ban_list(monkeypatch):
    saved = []
    monkeypatch.setattr(setup_store, "save_bans", lambda bans: saved.append(list(bans)))
    srv = SimpleNamespace(_bans=[{"name": "a", "worker_id": "w1"},
                                 {"name": "b", "worker_id": "w2"}])
    resp = await CombinedServer._api_unban(srv, _Req({"worker_id": "w1"}))
    assert json.loads(resp.text)["removed"] == 1
    assert saved == [[{"name": "b", "worker_id": "w2"}]]
