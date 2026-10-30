"""Enrollment (spec 12 of docs/spec/core-v2.md): a device signs in as a
person and the hub tells it how to configure itself; that hub becomes the
device's home hub (9.1) and the person its owner.

- :mod:`.hub`: the hub's side, behind its HTTP door. One-time codes (the QR
  a person shows a device), device codes (a device shows a short code, the
  person approves it), the config bundle, the owner's recovery key, and
  adding an enrolled worker to another hub's band (invites, memberships).
- :mod:`.device`: the device's side, ``python -m rook.core2.worker enroll``:
  find the hub, claim or wait for approval, write the anchor and config.
- :mod:`.mdns`: local network discovery (``_rook-hub._tcp``), a minimal
  mDNS responder and query.
"""
