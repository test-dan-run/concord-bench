"""Audio playback (the derived 16 kHz FLAC, with range requests) and waveform peaks."""

from __future__ import annotations

import mimetypes

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..core import paths
from ..core.db import get_session
from ..core.models import AudioFile, EnhancedFile

router = APIRouter(tags=["audio"])


def _file(session: Session, file_id: int) -> AudioFile:
    f = session.get(AudioFile, file_id)
    if f is None:
        raise HTTPException(404, "audio file not found")
    return f


def _sha(session: Session, f: AudioFile, copy_id: int | None) -> str:
    """The file's own audio, or its enhanced version in a copy."""
    if copy_id is None:
        return f.sha256
    from sqlalchemy import select

    e = session.scalar(select(EnhancedFile).where(EnhancedFile.copy_id == copy_id, EnhancedFile.audio_file_id == f.id))
    if e is None:
        raise HTTPException(404, "this enhanced copy has no audio for the file (deleted, or not built yet)")
    return e.sha256


@router.get("/audio/{file_id}")
def audio(file_id: int, original: bool = False, copy_id: int | None = None,
          session: Session = Depends(get_session)) -> FileResponse:
    f = _file(session, file_id)
    p = paths.original(f.sha256, f.ext) if original else paths.derived(_sha(session, f, copy_id))
    if not p.is_file():
        raise HTTPException(404, "no playable audio for this file (undecodable?)")
    # An original in the Dataset Catalog's store has no extension: its type comes from the file's own.
    media = "audio/flac" if p.suffix == ".flac" else mimetypes.guess_type(f"x{f.ext}")[0] if original else None
    return FileResponse(p, media_type=media, headers={"Cache-Control": "private, max-age=86400"})


@router.get("/audio/{file_id}/peaks")
def peaks(file_id: int, copy_id: int | None = None, session: Session = Depends(get_session)) -> FileResponse:
    f = _file(session, file_id)
    p = paths.peaks(_sha(session, f, copy_id))
    if not p.is_file():
        raise HTTPException(404, "no peaks for this file")
    return FileResponse(p, media_type="application/json", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/audio/{file_id}/info")
def info(file_id: int, session: Session = Depends(get_session)) -> dict:
    f = _file(session, file_id)
    return {"id": f.id, "path": f.rel_path, "stem": f.stem, "duration": f.duration, "sample_rate": f.orig_sr,
            "channels": f.channels, "status": f.status, "dataset_id": f.dataset_id, "meta": f.meta}
