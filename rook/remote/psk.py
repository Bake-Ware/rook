"""Human-readable band keys. Existing PSKs remain opaque, case-sensitive text.

Five independent draws from 7,776 words provide about 64.6 bits of entropy.
This improves manual entry; it does not add device authentication or make a
short passphrase equivalent to the old 192-bit random secret. See
psk_words.LICENSE.md for wordlist provenance.
"""

from functools import cache
from importlib.resources import files
import secrets


@cache
def _words() -> tuple[str, ...]:
    words = tuple(files(__package__).joinpath("psk_words.txt").read_text(
        encoding="ascii").splitlines())
    # Fail closed if packaging or a later edit damages the dictionary.
    if (len(words) != 7776 or len(set(words)) != 7776
            or any(not (w.isascii() and w.isalpha() and w.islower()) for w in words)):
        raise ValueError("invalid PSK wordlist")
    return words


def generate_psk() -> str:
    """Generate five lowercase, hyphen-separated words using the OS CSPRNG.

    Sample with replacement: repeated words are valid, and each of the
    7,776**5 phrases has equal probability. No network or saved state needed.
    """
    words = _words()
    return "-".join(secrets.choice(words) for _ in range(5))
