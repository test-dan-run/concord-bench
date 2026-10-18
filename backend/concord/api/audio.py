"""Audio playback (the derived 16 kHz FLAC, with range requests) and waveform peaks."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..core import paths
from ..core.db import get_session
from ..core.models import AudioFile

router = APIRouter(tags=["audio"])


def _file(session: Session, file_id: int) -> AudioFile:
    f = session.get(AudioFile, file_id)
    if f is None:
        raise HTTPException(404, "audio file not found")
    return f


@router.get("/audio/{file_id}")
def audio(file_id: int, original: bool = False, session: Session = Depends(get_session)) -> FileResponse:
    f = _file(session, file_id)
    p = paths.original(f.sha256, f.ext) if original else paths.derived(f.sha256)
    if not p.is_file():
        raise HTTPException(404, "no playable audio for this file (undecodable?)")
    media = "audio/flac" if p.suffix == ".flac" else None
    return FileResponse(p, media_type=media, headers={"Cache-Control": "private, max-age=86400"})


@router.get("/audio/{file_id}/peaks")
def peaks(file_id: int, session: Session = Depends(get_session)) -> FileResponse:
    f = _file(session, file_id)
    p = paths.peaks(f.sha256)
    if not p.is_file():
        raise HTTPException(404, "no peaks for this file")
    return FileResponse(p, media_type="application/json", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/audio/{file_id}/info")
def info(file_id: int, session: Session = Depends(get_session)) -> dict:
    f = _file(session, file_id)
    return {"id": f.id, "path": f.rel_path, "stem": f.stem, "duration": f.duration, "sample_rate": f.orig_sr,
            "channels": f.channels, "status": f.status, "dataset_id": f.dataset_id, "meta": f.meta}
