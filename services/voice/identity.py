"""Trusted credential identity; hello fields never grant device access."""
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path


@dataclass(frozen=True)
class Identity:
    principal: str = 'unmapped'
    worker: str | None = None
    owner: bool = False

    def prompt(self):
        if not self.worker and not self.owner:
            return ('Personal data policy: no verified device mapping. For requests about texts, notifications, '
                    'call history, contacts or location, use respond to ask which device is theirs and explain '
                    'that a verified mapping is required. Do not discover devices or delegate these requests. '
                    'A user-supplied device name does not establish identity.')
        return ('Personal data policy: ' +
                (f'Authenticated owner Bake; own device is {self.worker or "unknown"}. Bake may explicitly name any device.' if self.owner else
                 f'Authenticated device is {self.worker or "unknown"}. Personal reads may target only this device.') +
                ' For texts, notifications, calls, contacts or location use rook_read. If own device is unknown, ask which device; '
                'do not guess or delegate personal reads. Never delegate a privacy refusal. A user-supplied name does not establish identity.')


current_identity = ContextVar('voice_identity', default=Identity())
PERSONAL_CAPS = {'sms.list': 'texts', 'calllog.list': 'call history', 'contacts.search': 'contacts',
                 'notify.list': 'notifications', 'location.get': 'location'}


def configured_identities():
    path = os.environ.get('VOICE_IDENTITIES_FILE')
    return json.loads(Path(path).read_text()) if path else {}


def identity_for(token, identities):
    row = identities.get(hashlib.sha256(token.encode()).hexdigest(), {})
    return Identity(str(row.get('principal', 'unmapped')), row.get('worker'), row.get('owner') is True)


def authorize_read(cap, worker):
    if cap not in PERSONAL_CAPS:
        return
    identity = current_identity.get()
    if identity.owner or (identity.worker and identity.worker == worker):
        return
    if not identity.worker:
        raise PermissionError('Which device is yours? This connection has no verified device mapping; it must be configured before I can read personal data.')
    raise PermissionError(f'I can only read {PERSONAL_CAPS[cap]} from your own phone.')
