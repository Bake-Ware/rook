"""Sound tags in what the voice says (as Pianobar's DJ does): [laugh], [chuckle],
[sigh], [gasp], [clear throat]. Chatterbox voices perform them; every other engine
would read them aloud. No dependencies (runtime.py imports this)."""
import re


# Performed by Chatterbox voices only.
HOUSE_TAGS = ("[laugh]", "[chuckle]", "[sigh]", "[gasp]", "[clear throat]")
_BRACKET = re.compile(r"\[([^\[\]\n]{1,24})\]")
_STAGE = {"laugh": "(laughs)", "chuckle": "(chuckles)", "sigh": "(sighs)", "gasp": "(gasps)",
          "clear throat": "(clears throat)"}


def _tidy(text):
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"[ \t]+([.,!?;:])", r"\1", text).strip()


def keep_sounds(text, keep, allowed=HOUSE_TAGS):
    """Known sound tags stay (normalised) when the voice performs them; every other
    bracketed word goes, since a voice would read it aloud."""
    names = {t.strip("[]") for t in allowed}

    def one(m):
        name = " ".join(m[1].casefold().split())
        return f" [{name}] " if keep and name in names else " "
    out = _BRACKET.sub(one, text)
    return _tidy(out) if out != text else text


def stage_directions(text):
    """For a transcript: sound tags as stage directions ("(laughs)"), others dropped."""
    def one(m):
        name = " ".join(m[1].casefold().split())
        return f" {_STAGE[name]} " if name in _STAGE else " "
    out = _BRACKET.sub(one, text)
    return _tidy(out) if out != text else text
