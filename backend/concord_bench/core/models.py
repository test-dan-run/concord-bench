"""SQLite schema (PLAN §4). Inference outputs (`model_output`) are kept apart from scores (`score`,
`result`), so a new normaliser or collar rescores without re-running any model.

KWS hooks (Q6): `kws` is in every task enum and `keyword_event` exists, but nothing writes them yet.

Labelling lives in Concord Label (SPLIT_PLAN.md). Bench's segments are references: confirmed, from an
annotated upload.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def new_uid() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[str]: JSON, list[Any]: JSON}


class LabelType(enum.StrEnum):
    asr = "asr"
    diar = "diar"
    kws = "kws"


class Capability(enum.StrEnum):
    asr = "asr"
    vad = "vad"
    diar = "diar"
    kws = "kws"
    enh = "enhance"  # speech enhancement: audio in, cleaned audio out (the catalog's "enhance" task)


class TimingLevel(enum.StrEnum):
    word = "word"
    segment = "segment"
    text = "text"


class RunTask(enum.StrEnum):
    asr = "asr"
    seg = "seg"
    pipeline = "pipeline"
    kws = "kws"


class FileStatus(enum.StrEnum):
    ok = "ok"
    clipped = "clipped"
    undecodable = "undecodable"


class SegmentStatus(enum.StrEnum):
    confirmed = "confirmed"


class JobStatus(enum.StrEnum):
    queued = "queued"
    waiting = "waiting"  # its lease is queued in the catalog: waiting for compute
    running = "running"
    done = "done"
    failed = "failed"
    blocked = "blocked"  # cannot run: unknown or unverified model, a failed lease, or a failed earlier stage


class Dataset(Base):
    __tablename__ = "dataset"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    language: Mapped[str | None] = mapped_column(String(32))
    description: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(default=list)
    label_types: Mapped[list[str]] = mapped_column(default=list)
    status: Mapped[str] = mapped_column(String(16), default="ingesting")  # ingesting | ready | failed
    # Ingest report: counts, hours, sample-rate histogram, flagged files, annotation matches.
    report: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Where the references came from, for a dataset imported from a reference package (CONTRACTS.md §2):
    # the manifest's source, export_version, scope, review, models, normaliser, and the import id.
    # Empty for an annotated upload or a package without a manifest.
    provenance: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # "<install_id>/<dataset_uid>" of the source: datasets sharing it are versions of one dataset (§4.3).
    source_key: Mapped[str | None] = mapped_column(String(300), index=True)
    export_version: Mapped[int | None] = mapped_column(Integer)
    previous_version_id: Mapped[int | None] = mapped_column(ForeignKey("dataset.id", ondelete="SET NULL"))

    files: Mapped[list[AudioFile]] = relationship(back_populates="dataset", cascade="all, delete-orphan")


class AudioFile(Base):
    __tablename__ = "audio_file"
    __table_args__ = (UniqueConstraint("dataset_id", "rel_path"), Index("ix_audio_sha", "sha256"))
    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("dataset.id", ondelete="CASCADE"))
    rel_path: Mapped[str] = mapped_column(String(1000))
    sha256: Mapped[str] = mapped_column(String(64))
    ext: Mapped[str] = mapped_column(String(16))
    duration: Mapped[float | None] = mapped_column(Float)
    orig_sr: Mapped[int | None] = mapped_column(Integer)
    channels: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default=FileStatus.ok)
    # Free-form metadata used by "WER by slice" (e.g. condition, noise) from annotation files.
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)

    dataset: Mapped[Dataset] = relationship(back_populates="files")
    segments: Mapped[list[Segment]] = relationship(back_populates="audio_file", cascade="all, delete-orphan")

    @property
    def stem(self) -> str:
        name = self.rel_path.rsplit("/", 1)[-1]
        return name.rsplit(".", 1)[0] if "." in name else name

    @property
    def usable(self) -> bool:
        return self.status in (FileStatus.ok, FileStatus.clipped)


class Segment(Base):
    """A reference label. Text-only file-level transcripts are one segment spanning the file with no
    speaker; RTTM turns have a speaker and no text; speaker-attributed transcripts have both.

    `uid` identifies the segment across apps, so "reference is wrong" flags can go back to Concord Label
    (CONTRACTS.md §3)."""

    __tablename__ = "segment"
    __table_args__ = (Index("ix_segment_file", "audio_file_id", "start"), Index("ix_segment_uid", "uid"))
    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(32), default=new_uid)
    audio_file_id: Mapped[int] = mapped_column(ForeignKey("audio_file.id", ondelete="CASCADE"))
    start: Mapped[float] = mapped_column(Float)
    end: Mapped[float] = mapped_column(Float)
    speaker: Mapped[str | None] = mapped_column(String(100))
    text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default=SegmentStatus.confirmed)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    flag_note: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    audio_file: Mapped[AudioFile] = relationship(back_populates="segments")


class KeywordEvent(Base):
    """KWS hook (Q6): created now so KWS needs no migration later. Unused in Phase 1."""

    __tablename__ = "keyword_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    audio_file_id: Mapped[int] = mapped_column(ForeignKey("audio_file.id", ondelete="CASCADE"))
    keyword: Mapped[str] = mapped_column(String(200))
    start: Mapped[float] = mapped_column(Float)
    end: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default=SegmentStatus.confirmed)
    confidence: Mapped[float | None] = mapped_column(Float)
    source_run_id: Mapped[int | None] = mapped_column(ForeignKey("run.id", ondelete="SET NULL"))


class Model(Base):
    """A catalog model (platform/catalog/README.md) Bench has used: runs and pipelines point here. Rows are
    created on first use from the catalog's record; registration, verification and capabilities belong to
    the catalog. `catalog` keeps the last record seen (contract, api, capabilities of the current build),
    so pages and rescoring of old runs work while the catalog is unreachable."""

    __tablename__ = "model"
    id: Mapped[int] = mapped_column(primary_key=True)
    catalog_id: Mapped[str] = mapped_column(String(200), unique=True)  # the platform service name
    display_name: Mapped[str] = mapped_column(String(200))
    tags: Mapped[list[str]] = mapped_column(default=list)
    parent_model_id: Mapped[int | None] = mapped_column(ForeignKey("model.id", ondelete="SET NULL"))
    catalog: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def caps(self) -> dict[str, Any]:
        """The catalog's capability record for the model's current build ({} before the first verification)."""
        return (self.catalog or {}).get("capabilities") or {}

    @property
    def capabilities(self) -> list[str]:
        """Tasks the catalog's verifier observed: asr, vad, diar (kws later)."""
        return [t for t in self.caps.get("tasks") or [] if t in set(Capability)]

    @property
    def timing_level(self) -> str | None:
        return self.caps.get("timing_level") if self.is_asr else None

    @property
    def is_asr(self) -> bool:
        return Capability.asr in self.capabilities

    @property
    def is_seg(self) -> bool:
        return Capability.vad in self.capabilities or Capability.diar in self.capabilities

    @property
    def has_speakers(self) -> bool:
        return Capability.diar in self.capabilities

    @property
    def is_enh(self) -> bool:
        return Capability.enh in self.capabilities

    @property
    def generative(self) -> bool:
        """The catalog marks the model as able to shift timing or invent speech (platform P1; absent = no)."""
        return bool((self.catalog or {}).get("generative"))


class Pipeline(Base):
    __tablename__ = "pipeline"
    __table_args__ = (UniqueConstraint("seg_model_id", "asr_model_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    seg_model_id: Mapped[int | None] = mapped_column(ForeignKey("model.id", ondelete="CASCADE"))  # None = oracle
    asr_model_id: Mapped[int] = mapped_column(ForeignKey("model.id", ondelete="CASCADE"))

    seg_model: Mapped[Model | None] = relationship(foreign_keys=[seg_model_id])
    asr_model: Mapped[Model] = relationship(foreign_keys=[asr_model_id])


class Run(Base):
    """One inference job: a model (asr / seg) or a pipeline on a dataset."""

    __tablename__ = "run"
    id: Mapped[int] = mapped_column(primary_key=True)
    task: Mapped[str] = mapped_column(String(16))
    model_id: Mapped[int | None] = mapped_column(ForeignKey("model.id", ondelete="CASCADE"))
    pipeline_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline.id", ondelete="CASCADE"))
    dataset_id: Mapped[int] = mapped_column(ForeignKey("dataset.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued | waiting | running | done | failed | blocked
    error: Mapped[str | None] = mapped_column(Text)
    # The builds that produced the run's outputs: catalog model id -> {build_key, image}. Runs from before
    # the catalog have {image, labels} (the pinned tag) and no build key.
    builds: Mapped[dict[str, Any]] = mapped_column(default=dict)
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # Per-file inference outputs gathered by the run, consumed by scoring (keys are audio_file ids).
    outputs: Mapped[dict[str, Any]] = mapped_column(default=dict)
    rtf_by_stage: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # What each model ran on, per catalog model id, as the model catalog reported it (platform.catalog
    # `hardware`): device (gpu | cpu | hosted), declared VRAM, engine, the host's GPU and CPU, runtime
    # precision and batch size, peak VRAM. Fields the catalog does not report are absent.
    hardware: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # Seconds from start to finish, including the stage jobs of an assembled pipeline (not cold starts).
    wall_s: Mapped[float | None] = mapped_column(Float)
    # Speech enhancement (EVALUATE_PLAN.md §5): the run read the enhanced copy of the dataset that this
    # enhancement model made. None = original audio. Pipelines also place it before or after diarisation.
    enh_model_id: Mapped[int | None] = mapped_column(ForeignKey("model.id", ondelete="CASCADE"))
    enh_copy_id: Mapped[int | None] = mapped_column(ForeignKey("enhanced_copy.id", ondelete="SET NULL"))
    enh_placement: Mapped[str | None] = mapped_column(String(8))  # before | after (pipelines only)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    model: Mapped[Model | None] = relationship(foreign_keys=[model_id])
    enh_model: Mapped[Model | None] = relationship(foreign_keys=[enh_model_id])
    pipeline: Mapped[Pipeline | None] = relationship()
    dataset: Mapped[Dataset] = relationship()


class EnhancedCopy(Base):
    """A cleaned copy of a dataset's audio made by one enhancement model's build (EVALUATE_PLAN.md §5.2).

    Same files and lengths as the originals, which are never changed. Each file is stored as derived audio
    under its own sha256 (`enhanced_file`), so runs on the copy are cached like any other audio. Deleting a
    copy removes its audio and keeps this row (`deleted`), so the runs that read it keep their scores."""

    __tablename__ = "enhanced_copy"
    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("dataset.id", ondelete="CASCADE"))
    model_id: Mapped[int] = mapped_column(ForeignKey("model.id", ondelete="CASCADE"))
    build_key: Mapped[str | None] = mapped_column(String(32))
    params_hash: Mapped[str] = mapped_column(String(16))
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(16), default="building")  # building | ready | failed | cancelled | deleted
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    files: Mapped[int | None] = mapped_column(Integer)
    # ok: every file came back the right length; trimmed: some were off by up to 10 ms and were padded or
    # trimmed to the original length; failed: a file was further off.
    length_check: Mapped[str | None] = mapped_column(String(16))
    max_shift_ms: Mapped[float | None] = mapped_column(Float)
    audio_s: Mapped[float | None] = mapped_column(Float)
    processing_s: Mapped[float | None] = mapped_column(Float)
    hardware: Mapped[dict[str, Any]] = mapped_column(default=dict)
    image: Mapped[str | None] = mapped_column(String(300))
    job_id: Mapped[int | None] = mapped_column(ForeignKey("job.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    model: Mapped[Model] = relationship()
    dataset: Mapped[Dataset] = relationship()

    @property
    def rtf(self) -> float | None:
        return self.processing_s / self.audio_s if self.audio_s and self.processing_s is not None else None


class EnhancedFile(Base):
    __tablename__ = "enhanced_file"
    __table_args__ = (UniqueConstraint("copy_id", "audio_file_id", name="uq_enhanced_file"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    copy_id: Mapped[int] = mapped_column(ForeignKey("enhanced_copy.id", ondelete="CASCADE"))
    audio_file_id: Mapped[int] = mapped_column(ForeignKey("audio_file.id", ondelete="CASCADE"))
    sha256: Mapped[str] = mapped_column(String(64))  # of the enhanced 16 kHz FLAC (paths.derived)
    duration: Mapped[float] = mapped_column(Float)
    shift_ms: Mapped[float] = mapped_column(Float, default=0.0)  # how far its length was off before the fix
    latency_s: Mapped[float] = mapped_column(Float, default=0.0)


class ModelOutput(Base):
    """The inference cache, keyed on (model, build, audio, span, params). Shared by runs and pipelines, and
    with other apps through the catalog's output index: a row read from another app keeps its owner and
    the owner's output id. Rows from before the catalog have no build key: still readable by id (old runs
    score), never matched by a lookup."""

    __tablename__ = "model_output"
    __table_args__ = (UniqueConstraint("model_name", "build_key", "audio_sha256", "span_start", "span_end",
                                       "params_hash", name="uq_model_output_key"),
                      Index("ix_model_output_audio", "audio_sha256"))
    id: Mapped[int] = mapped_column(primary_key=True)
    model_name: Mapped[str] = mapped_column(String(200))  # catalog model id (platform service name)
    build_key: Mapped[str | None] = mapped_column(String(32))
    audio_sha256: Mapped[str] = mapped_column(String(64))
    span_start: Mapped[float] = mapped_column(Float, default=0.0)
    span_end: Mapped[float] = mapped_column(Float, default=-1.0)  # -1 = whole file
    params_hash: Mapped[str] = mapped_column(String(16))
    params: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # the adapter params params_hash was taken of
    output: Mapped[dict[str, Any]] = mapped_column()
    latency_s: Mapped[float] = mapped_column(Float)
    image: Mapped[str | None] = mapped_column(String(300))  # image tag, for display
    # Copied from another app through the catalog's index: that app's name and its output id.
    source_app: Mapped[str | None] = mapped_column(String(64))
    source_output_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Score(Base):
    __tablename__ = "score"
    __table_args__ = (UniqueConstraint("run_id", "normaliser", "collar", "skip_overlap", name="uq_score_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("run.id", ondelete="CASCADE"))
    normaliser: Mapped[str] = mapped_column(String(100))  # id@version
    collar: Mapped[float] = mapped_column(Float)
    # Overlapped speech left out of DER and JER (seg runs and pipelines' seg stage).
    skip_overlap: Mapped[bool] = mapped_column(Boolean, default=False)
    aggregates: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    run: Mapped[Run] = relationship()


class Result(Base):
    """Per-utterance (ASR) or per-file (seg, pipeline) rows; every drill-down sorts and filters these."""

    __tablename__ = "result"
    __table_args__ = (Index("ix_result_score", "score_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    score_id: Mapped[int] = mapped_column(ForeignKey("score.id", ondelete="CASCADE"))
    audio_file_id: Mapped[int] = mapped_column(ForeignKey("audio_file.id", ondelete="CASCADE"))
    utterance_key: Mapped[str] = mapped_column(String(200))
    segment_id: Mapped[int | None] = mapped_column(ForeignKey("segment.id", ondelete="SET NULL"))
    start: Mapped[float] = mapped_column(Float, default=0.0)
    end: Mapped[float] = mapped_column(Float, default=0.0)
    speaker: Mapped[str | None] = mapped_column(String(100))
    ref_text: Mapped[str | None] = mapped_column(Text)
    hyp_text: Mapped[str | None] = mapped_column(Text)
    # Sort keys pulled out of `metrics` so SQL can order on them.
    wer: Mapped[float | None] = mapped_column(Float)
    errors: Mapped[int | None] = mapped_column(Integer)
    metrics: Mapped[dict[str, Any]] = mapped_column(default=dict)
    alignment: Mapped[dict[str, Any]] = mapped_column(default=dict)

    audio_file: Mapped[AudioFile] = relationship()


class Job(Base):
    __tablename__ = "job"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(32))  # ingest | run | seg_stage | asr_stage | score | enhance
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.queued)
    required_services: Mapped[list[str]] = mapped_column(default=list)  # catalog model ids it leases
    after: Mapped[list[Any]] = mapped_column(default=list)  # job ids that must be done first
    # The catalog lease while it waits for compute: {id, state, position, waiting_for}.
    lease: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str | None] = mapped_column(String(300))
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class ReferenceImport(Base):
    """A reference package received by Bench (CONTRACTS.md §2, SPLIT_PLAN.md §4.3): staged with a preview
    until a person accepts it (a new dataset through the ingest job) or rejects it."""

    __tablename__ = "reference_import"
    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="staged")  # staged | accepting | accepted | rejected | failed
    name: Mapped[str] = mapped_column(String(200))
    upload_id: Mapped[str | None] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(String(1000))  # the package folder (inside the upload)
    manifest: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # None: a package without a manifest
    preview: Mapped[dict[str, Any]] = mapped_column(default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)  # uids matched, outputs imported / skipped
    error: Mapped[str | None] = mapped_column(Text)
    dataset_id: Mapped[int | None] = mapped_column(ForeignKey("dataset.id", ondelete="SET NULL"))
    job_id: Mapped[int | None] = mapped_column(ForeignKey("job.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
