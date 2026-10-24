"""WER text normalisers. Every score stores `<id>@<version>`, so changing a normaliser's behaviour
means bumping its version and old scores stay explainable.

- `none`: whitespace only.
- `basic` (default, Q9): lowercase, dashes and slashes become spaces, other punctuation is removed,
  whitespace collapsed. "Don't" -> "dont", "state-of-the-art" -> "state of the art".
- `whisper_en`: OpenAI Whisper's English normaliser (spelling, contractions, numbers).
- `platform:<service>`: any platform `text.transform` service, applied before `basic`, called through
  the model catalog's gateway (aicore with AICORE_CATALOG), which starts it when it is stopped. Offered as an
  extra, never the default: the platform's `text-normalizer` adds casing and punctuation, which is
  the opposite of WER normalisation, so `basic` runs after it.
"""

from __future__ import annotations

import re
import unicodedata
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache

DEFAULT = "basic"


@dataclass(frozen=True)
class Normaliser:
    id: str
    version: int
    label: str
    description: str
    fn: Callable[[list[str]], list[str]]  # batch: list in, list out
    service: str | None = None  # platform service the normaliser calls, if any

    @property
    def key(self) -> str:
        return f"{self.id}@{self.version}"

    def __call__(self, text: str) -> str:
        return self.fn([text])[0]

    def many(self, texts: list[str]) -> list[str]:
        return self.fn(texts) if texts else []


_WS = re.compile(r"\s+")
_SPLITTERS = re.compile(r"[\-‐‑‒–—―/\\_]+")


def _strip_punct(text: str) -> str:
    return "".join(ch for ch in text if not unicodedata.category(ch).startswith(("P", "S")))


def none_fn(texts: list[str]) -> list[str]:
    return [_WS.sub(" ", t).strip() for t in texts]


def basic_one(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = _SPLITTERS.sub(" ", text)
    text = _strip_punct(text)
    return _WS.sub(" ", text).strip()


def basic_fn(texts: list[str]) -> list[str]:
    return [basic_one(t) for t in texts]


@cache
def _whisper():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        from whisper_normalizer.english import EnglishTextNormalizer
    return EnglishTextNormalizer()


def whisper_fn(texts: list[str]) -> list[str]:
    n = _whisper()
    return [_WS.sub(" ", n(t)).strip() for t in texts]


BUILTIN: dict[str, Normaliser] = {
    n.id: n
    for n in (
        Normaliser("basic", 1, "Lowercase + strip punct.", "Lowercase, remove punctuation, collapse whitespace.",
                   basic_fn),
        Normaliser("whisper_en", 1, "Whisper EN", "OpenAI Whisper English normaliser: spelling, contractions, "
                   "numbers.", whisper_fn),
        Normaliser("none", 1, "None (raw)", "Whitespace only; case and punctuation count as errors.", none_fn),
    )
}


# text.transform names no batch limit, but services cap it (monkey-custom 1000, the ITN services 256)
PLATFORM_BATCH = 256


def platform_normaliser(service: str) -> Normaliser:
    def fn(texts: list[str]) -> list[str]:
        import aicore

        c = aicore.client(service)
        out = [o for i in range(0, len(texts), PLATFORM_BATCH) for o in c.transform(texts[i:i + PLATFORM_BATCH])]
        return basic_fn(out)

    return Normaliser(f"platform:{service}", 1, f"{service} + basic", f"Platform text.transform service "
                      f"{service!r}, then basic.", fn, service=service)


def get(key: str) -> Normaliser:
    """`basic`, `basic@1` or `platform:<service>[@1]`."""
    nid = key.split("@", 1)[0]
    if nid.startswith("platform:"):
        return platform_normaliser(nid.removeprefix("platform:"))
    try:
        return BUILTIN[nid]
    except KeyError:
        raise KeyError(f"unknown normaliser {key!r}; use one of {', '.join(BUILTIN)} or platform:<service>") from None


def tokens(text: str) -> list[str]:
    return text.split()
