"""A file's reference labels, viewed the way each task needs them.

Utterance unit (PLAN §4): if the references have segment-level text, an utterance is a reference
segment; otherwise it is the whole file.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.models import AudioFile, Segment, SegmentStatus
from ..metrics.der import Turn

# Reference labels: confirmed segments (the only status Bench writes; Concord Label sends only confirmed ones).
REFERENCE_STATUSES = (SegmentStatus.confirmed,)


@dataclass
class Unit:
    """One ASR utterance: a span of a file and its reference text."""

    key: str
    start: float
    end: float
    text: str
    speaker: str | None = None
    segment_id: int | None = None
    whole_file: bool = False


@dataclass
class FileRefs:
    file: AudioFile
    segments: list[Segment] = field(default_factory=list)
    # The audio models hear, when it is not the file's own: an enhanced copy's file (same length).
    audio_sha: str | None = None

    @property
    def sha256(self) -> str:
        """The audio a model call is about (and its cache key): the enhanced file when there is one."""
        return self.audio_sha or self.file.sha256

    @property
    def duration(self) -> float:
        return self.file.duration or 0.0

    def _ref(self) -> list[Segment]:
        return sorted((s for s in self.segments if s.status in REFERENCE_STATUSES), key=lambda s: (s.start, s.end))

    @property
    def text_segments(self) -> list[Segment]:
        return [s for s in self._ref() if s.text is not None]

    @property
    def speaker_turns(self) -> list[Turn]:
        return [Turn(s.start, s.end, s.speaker) for s in self._ref() if s.speaker is not None]

    @property
    def spk_text(self) -> list[Segment]:
        return [s for s in self._ref() if s.speaker is not None and s.text is not None]

    def _is_whole(self, s: Segment) -> bool:
        return s.speaker is None and s.start <= 0.01 and s.end >= self.duration - 0.05

    @property
    def segment_level(self) -> bool:
        """True when the file has sub-file text segments (speaker-attributed or timed)."""
        return any(not self._is_whole(s) for s in self.text_segments)

    @property
    def has_text(self) -> bool:
        return bool(self.text_segments)

    @property
    def has_speakers(self) -> bool:
        return bool(self.speaker_turns)

    @property
    def has_spk_text(self) -> bool:
        return bool(self.spk_text)

    def full_text(self) -> str | None:
        """Whole-file reference transcript: the file-level text if there is one, otherwise the
        segment texts in start-time order."""
        whole = [s for s in self.text_segments if self._is_whole(s)]
        if whole:
            return " ".join(s.text or "" for s in whole).strip()
        segs = self.text_segments
        return " ".join((s.text or "").strip() for s in segs).strip() if segs else None

    def asr_units(self) -> list[Unit]:
        stem = self.file.stem
        if self.segment_level:
            segs = [s for s in self.text_segments if not self._is_whole(s)]
            width = len(str(len(segs)))
            return [Unit(f"{stem}#{k + 1:0{width}d}", s.start, s.end, s.text or "", s.speaker, s.id)
                    for k, s in enumerate(segs)]
        text = self.full_text()
        return [Unit(stem, 0.0, self.duration, text or "", None,
                     self.text_segments[0].id if self.text_segments else None, whole_file=True)]


def has_segment_level_text(refs: list[FileRefs]) -> bool:
    return any(r.segment_level for r in refs)
