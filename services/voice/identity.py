"""Trusted credential identity; hello fields never grant device access."""
from contextvars import ContextVar
from dataclasses import dataclass
import dataclasses
import re
import hashlib
import json
import os
from pathlib import Path


@dataclass(frozen=True)
class Identity:
    principal: str = 'unmapped'
    worker: str | None = None
    owner: bool = False

    def tools(self):
        """Tool names this credential may use; None means all (owner only)."""
        if self.owner:
            return None
        return GUEST_TOOLS | ({'rook_read'} if self.worker else set())

    def prompt(self):
        if not self.worker and not self.owner:
            return ('Personal data policy: no verified device mapping. For requests about texts, notifications, '
                    'call history, contacts or location, use respond to ask which device is theirs and explain '
                    'that a verified mapping is required. Do not discover devices or delegate these requests. '
                    'A user-supplied device name does not establish identity.')
        return ('Personal data policy: ' +
                (f'Authenticated owner {self.principal}; own device is {self.worker or "unknown"}. The owner may explicitly name any device.' if self.owner else
                 f'Authenticated device is {self.worker or "unknown"}. Personal reads may target only this device.') +
                ' For texts, notifications, calls, contacts or location use rook_read. If own device is unknown, ask which device; '
                'do not guess or delegate personal reads. Never delegate a privacy refusal. A user-supplied name does not establish identity.')


# Keyless guests (VOICE_ALLOW_ANONYMOUS) and unprivileged keys never reach band
# devices: no device listing, no reads, no agent. A key mapped to a device may
# read only that device.
GUEST_TOOLS = frozenset({'web_search', 'end_session', 'cancel_job', 'job_status'})


def allowed_tools(identity):
    """Tool names this credential may be offered; None means all (owner only)."""
    return identity.tools()


current_identity = ContextVar('voice_identity', default=Identity())
PERSONAL_CAPS = {'sms.list': 'texts', 'calllog.list': 'call history', 'contacts.search': 'contacts',
                 'notify.list': 'notifications', 'location.get': 'location', 'calendar.list': 'calendar', 'ui.text': 'screen'}


def configured_identities():
    path = os.environ.get('VOICE_IDENTITIES_FILE')
    return json.loads(Path(path).read_text()) if path else {}


def identity_for(token, identities):
    row = identities.get(hashlib.sha256(token.encode()).hexdigest(), {})
    return Identity(str(row.get('principal', 'unmapped')), row.get('worker'), row.get('owner') is True)


class PolicyRefusal(PermissionError):
    """A fixed, user-facing identity-policy refusal. Unlike raw tool errors its
    text is safe to speak to any key."""


def authorize_read(cap, worker):
    """Every device read is scoped, not only personal data: files, env, logs and
    agent memory are just as private. Owners may read any device; a mapped key
    only its own device; a guest none."""
    identity = current_identity.get()
    if identity.owner or (identity.worker and identity.worker == worker):
        return
    if not identity.worker:
        raise PolicyRefusal('Which device is yours? This connection has no verified device mapping; it must be configured before I can read device data.')
    raise PolicyRefusal(f'I can only read {PERSONAL_CAPS.get(cap, "device data")} from your own device.')


def authorize_devices():
    if not current_identity.get().owner:
        raise PolicyRefusal('Listing band devices requires an owner voice key.')


_DEVICE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._ -]{0,63}')


def with_hello_device(identity, device):
    """An owner key may name the device it talks from (hello ``device``), which
    becomes its default device for battery/location/calendar/mail. Any other key
    keeps only its server-side mapping; hello fields never grant device access."""
    if identity.owner and not identity.worker and isinstance(device, str) and _DEVICE_NAME.fullmatch(device):
        return dataclasses.replace(identity, worker=device)
    return identity


# On the owner's own phone the speaker is the owner unless speaker ID is confident it is
# someone else (Bake, 2026-10-27: "default to Bake. Only treat the speaker as someone else
# when it's confidently 'definitely not Bake' ... Unsure means Bake."). Measured on kaiju
# 2026-10-27: Bake's own turns scored 0.17-0.67 against his print (a third of them under
# 0.45, so they were heard as "guest" and refused Bake-only requests mid-conversation);
# Logan's scored at most 0.13. "Definitely not" is under NOT_OWNER_BELOW on at least
# NOT_OWNER_MIN_S of speech, or a match to another enrolled voice.
NOT_OWNER_BELOW = float(os.environ.get('VOICE_SPEAKER_NOT_OWNER', '0.15') or 0.15)
NOT_OWNER_MIN_S = float(os.environ.get('VOICE_SPEAKER_NOT_OWNER_MIN_S', '1.0') or 1.0)


def on_owner_phone(heard, owner, speech_s=None):
    """Who is talking on the owner's own phone: ``{'label', 'score', 'sure', 'basis'}``.

    The owner, unless ``heard`` (:meth:`speaker.Speakers.identify`'s result, or None when no
    voiceprint could be made) is confident it is someone else: another enrolled voice
    matched, or the owner's print scored under :data:`NOT_OWNER_BELOW` on at least
    :data:`NOT_OWNER_MIN_S` of speech (``speech_s``). Then the label is that person or
    'guest'. A weak or missing match never moves a turn away from the owner, so the
    speaker cannot flip mid-conversation on one short or noisy utterance. ``basis`` is
    'voice' when the voice decided, 'owner phone' when the owner stands by default.
    None for the assistant's own voice (the echo guard's label stays as heard)."""
    heard = heard or {}
    if heard.get('label') == 'assistant':
        return None
    principal, score = heard.get('principal'), heard.get('score')
    if principal and principal != owner:
        return {'label': principal, 'score': score, 'sure': True, 'basis': 'voice'}
    owner_score = (heard.get('scores') or {}).get(owner)
    if owner_score is None and heard.get('candidate', principal) == owner:
        owner_score = score
    if (owner_score is not None and owner_score < NOT_OWNER_BELOW
            and isinstance(speech_s, (int, float)) and speech_s >= NOT_OWNER_MIN_S):
        return {'label': 'guest', 'score': owner_score, 'sure': False, 'basis': 'voice'}
    matched = principal == owner and owner_score is not None
    return {'label': owner, 'score': owner_score, 'sure': True, 'basis': 'voice' if matched else 'owner phone'}
