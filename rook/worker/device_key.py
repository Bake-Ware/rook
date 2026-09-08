"""Ed25519 device keys using the worker's existing PyNaCl dependency.

Serialization is the fixed RFC 8410 PKCS#8/SPKI format and PKCS#10 CSR format.
The cryptographic operations are provided by libsodium, not this encoder.
This lets native Android enroll without another platform-specific crypto wheel.
"""
import base64
from nacl.signing import SigningKey

_ALGORITHM = bytes.fromhex('300506032b6570')
_PRIVATE_PREFIX = bytes.fromhex('302e020100300506032b657004220420')


def _der(tag, content):
    size = len(content)
    length = bytes([size]) if size < 128 else bytes([0x81, size])
    if size > 255:
        raise ValueError('Unexpected device-key encoding size.')
    return bytes([tag]) + length + content


def _pem(label, data):
    encoded = base64.b64encode(data).decode()
    return f'-----BEGIN {label}-----\n' + '\n'.join(encoded[i:i+64] for i in range(0,len(encoded),64)) + f'\n-----END {label}-----\n'


def private_pem(key):
    return _pem('PRIVATE KEY', _PRIVATE_PREFIX + bytes(key))


def load_private(pem):
    raw = base64.b64decode(''.join(line for line in pem.splitlines() if not line.startswith('---')), validate=True)
    if len(raw) != len(_PRIVATE_PREFIX)+32 or not raw.startswith(_PRIVATE_PREFIX):
        raise ValueError('Not a Rook Ed25519 private key.')
    return SigningKey(raw[len(_PRIVATE_PREFIX):])


def certificate_request():
    key = SigningKey.generate()
    subject = _der(0x30, _der(0x31, _der(0x30, bytes.fromhex('0603550403') + _der(0x0c,b'rook worker'))))
    spki = _der(0x30, _ALGORITHM + _der(0x03,b'\x00'+bytes(key.verify_key)))
    info = _der(0x30,b'\x02\x01\x00'+subject+spki+b'\xa0\x00')
    csr = _der(0x30,info+_ALGORITHM+_der(0x03,b'\x00'+key.sign(info).signature))
    return key, _pem('CERTIFICATE REQUEST',csr)


def sign(pem, message):
    try:
        key = load_private(pem)
    except ValueError:
        # Compatibility with P-256 identities issued by the first release.
        from cryptography.hazmat.primitives import serialization,hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        key = serialization.load_pem_private_key(pem.encode(),password=None)
        return key.sign(message,ec.ECDSA(hashes.SHA256()))
    return key.sign(message).signature
