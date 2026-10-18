"""Phase 2 Label on recorded responses: pseudo-labelling an audio-only copy of the fixture set, the
console, annotation edits, live evaluation and promotion (HTTP API, no GPU)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from concord.core.models import Run, Score, Segment
from label_helpers import _ok, label


def test_preview_reuses_pipeline_outputs(label_flow, client):
    flow = label_flow
    ids = flow["ids"]
    asr = f"{ids['whisper-large-v3']},{ids['faster-whisper-large-v3']}"
    pv = _ok(client.get(f"/api/label/datasets/{flow['unlabelled']}/preview?seg_model_id={ids['pyannote-diarization']}"
                        f"&asr_model_ids={asr}"))
    # the acceptance flow ran pyannote -> both ASR pipelines on the same audio: everything is saved
    assert pv["seg"]["state"] == "saved" and pv["files"] == 6
    assert [m["state"] for m in pv["members"]] == ["saved", "saved"]
    assert pv["saved_members"] == 2 and pv["new_members"] == 0


def test_label_run_needs_no_services_when_saved(labelled):
    assert labelled["started"]["job"]["required_services"] == []


def test_label_run_writes_triaged_segments(labelled, session):
    res = labelled["results"]
    s = res["summary"]
    assert s["segments"] > 0 and s["files"] == 6 and s["kept_reviewed"] == 0
    assert sum(s["triage"].values()) == s["segments"]
    assert {c["member"] for c in s["chosen"]} <= {"whisper-large-v3", "faster-whisper-large-v3"}
    assert len(s["agreement"]["cells"]) == 2
    segs = list(session.scalars(select(Segment).where(Segment.source_run_id == res["run"]["id"])))
    assert len(segs) == s["segments"]
    assert all(x.speaker and x.text is not None and x.edited_at is None for x in segs)
    for x in segs:
        expect = "auto" if x.confidence >= 0.85 else ("pseudo" if x.confidence >= 0.5 else "rejected")
        assert x.status == expect
        assert set(x.meta["members"]) == {"whisper-large-v3", "faster-whisper-large-v3"}
    q = res["queue"]
    assert all(r["status"] in ("pseudo", "rejected") for r in q)
    assert [r["confidence"] for r in q] == sorted(r["confidence"] for r in q)


def test_console_progress_adds_up(labelled, client):
    rows = {r["id"]: r for r in _ok(client.get("/api/label/console"))}
    r = rows[labelled["unlabelled"]]
    assert r["label_source"] == "pseudo-labels" and r["tasks"] == ["asr", "diar"]
    bar = r["progress"]["bar"]
    assert bar["unlabelled"] == 0 and bar["running"] == 0
    assert sum(bar.values()) == pytest.approx(r["hours"] * 3600, rel=1e-6)
    assert r["next"]["kind"] == "review" or r["next"]["label"].startswith("Confirm")
    ref = rows[labelled["dataset"]["id"]]
    assert ref["label_source"] == "uploaded references" and ref["progress"]["bar"]["confirmed"] > 0


def test_rerun_keeps_reviewed_work(labelled, client, session):
    ds = labelled["unlabelled"]
    files = _ok(client.get(f"/api/label/datasets/{ds}/files"))
    view = _ok(client.get(f"/api/label/files/{files[0]['id']}"))
    first = view["segments"][0]
    _ok(client.patch(f"/api/label/segments/{first['id']}", json={"text": "Edited by hand.", "status": "confirmed"}))
    # re-fuse with ROVER: no inference, the edited segment survives, the rest is replaced
    _, res = label(client, labelled, strategy="rover")
    s = res["summary"]
    assert s["kept_reviewed"] >= 1 and s["replaced"] > 0
    assert s["strategy_applied"] == {"best_aligned": s["segments"]}  # only one word-timed member
    kept = session.get(Segment, first["id"])
    assert kept.text == "Edited by hand." and kept.status == "confirmed" and kept.reviewed


def test_annotate_edits(labelled, client):
    ds = labelled["unlabelled"]
    fid = _ok(client.get(f"/api/label/datasets/{ds}/files"))[0]["id"]
    v = _ok(client.get(f"/api/label/files/{fid}"))
    assert v["position"]["index"] == 1 and v["next_file_id"] and v["keywords"] == []
    segs = v["segments"]
    assert len(segs) >= 3 and all(len(x["alternatives"]) == 2 for x in segs)
    a, b, c = segs[0], segs[1], segs[2]
    # bulk: assign speaker + confirm
    out = _ok(client.post("/api/label/segments/bulk", json={"ids": [a["id"], b["id"]], "action": "assign_speaker",
                                                             "speaker": "agent"}))
    assert {x["speaker"] for x in out["segments"]} == {"agent"} and all(x["reviewed"] for x in out["segments"])
    _ok(client.post("/api/label/segments/bulk", json={"ids": [a["id"]], "action": "confirm"}))
    # merge a + b
    m = _ok(client.post("/api/label/segments/merge", json={"ids": [a["id"], b["id"]]}))
    assert m["id"] == a["id"] and m["end"] == b["end"] and m["status"] == "pseudo"
    assert m["text"].startswith(a["text"].strip()[:10])
    # split c at its middle: both halves to review, text kept in order
    mid = round((c["start"] + c["end"]) / 2, 3)
    h1, h2 = _ok(client.post(f"/api/label/segments/{c['id']}/split", json={"t": mid}))
    assert h1["end"] == h2["start"] == mid and h1["status"] == h2["status"] == "pseudo"
    assert f"{h1['text']} {h2['text']}".split() == c["text"].split()
    assert _ok(client.post(f"/api/label/segments/{c['id']}/split", json={"t": c["start"]}), 422)
    # bad span, create, delete
    _ok(client.patch(f"/api/label/segments/{h1['id']}", json={"end": 9999}), 422)
    new = _ok(client.post(f"/api/label/files/{fid}/segments", json={"start": 0.0, "end": 0.4, "speaker": "agent"}), 201)
    assert new["status"] == "pseudo" and new["reviewed"]
    _ok(client.delete(f"/api/label/segments/{new['id']}"), 204)
    # confirm all high-confidence
    n = _ok(client.post(f"/api/label/files/{fid}/confirm-auto"))["confirmed"]
    v2 = _ok(client.get(f"/api/label/files/{fid}"))
    assert n == sum(1 for x in segs[3:] if x["status"] == "auto")
    assert not any(x["status"] == "auto" for x in v2["segments"])


def test_live_eval_and_promote(labelled, client, session):
    ds = labelled["unlabelled"]
    lv = _ok(client.get(f"/api/label/datasets/{ds}/live"))
    assert lv["asr"]["confirmed"] == 0 and lv["asr"]["models"] == [] and lv["kws"] == {"available": False}
    _ok(client.post(f"/api/label/datasets/{ds}/promote", json={"name": "x"}), 422)  # nothing confirmed yet
    # confirm every segment of every file
    for f in _ok(client.get(f"/api/label/datasets/{ds}/files")):
        ids = [x["id"] for x in _ok(client.get(f"/api/label/files/{f['id']}"))["segments"]]
        if ids:
            _ok(client.post("/api/label/segments/bulk", json={"ids": ids, "action": "confirm"}))
    lv = _ok(client.get(f"/api/label/datasets/{ds}/live"))
    a = lv["asr"]
    assert a["confirmed"] == labelled["results"]["summary"]["segments"]
    names = {m["service"]: m for m in a["models"]}
    assert set(names) == {"whisper-large-v3", "faster-whisper-large-v3"}
    for m in a["models"]:
        assert m["coverage"] == a["confirmed"] and m["curve"][-1]["confirmed"] == a["confirmed"]
        assert m["curve"][-1]["wer"] == pytest.approx(m["wer"])
        if a["ranked"]:
            lo, hi = m["ci"]
            assert lo <= m["wer"] <= hi
    # confirmed text is each segment's chosen member output, so at least one model has a low WER
    assert min(m["wer"] for m in a["models"]) < 0.5
    assert lv["der"]["files"] == 6 and {m["name"] for m in lv["der"]["models"]} >= {"pyannote-diarization"}
    # the references are pyannote's own turns after cut merging, so it is best and nearly perfect
    assert lv["der"]["models"][0]["name"] == "pyannote-diarization" and lv["der"]["models"][0]["der"] < 0.1
    assert lv["feed"] and lv["feed"][0]["confirmed_at"]
    new = _ok(client.post(f"/api/label/datasets/{ds}/promote", json={"name": "Promoted calls"}), 201)
    assert new["label_types"] == ["asr", "diar"] and new["mode"] == "annotated" and new["files"] == 6
    assert new["has_speaker_text"]
    # the promoted dataset's benchmark score becomes the locked benchmark. (Benchmarking it needs new
    # inference: references are the unpadded turns, which have no recorded responses, so seed the score.)
    asr_id = labelled["ids"]["faster-whisper-large-v3"]
    run = Run(task="asr", model_id=asr_id, dataset_id=new["id"], status="done")
    session.add(run)
    session.flush()
    session.add(Score(run_id=run.id, normaliser="basic@1", collar=0.0, aggregates={"wer": 0.123}))
    session.commit()
    lv = _ok(client.get(f"/api/label/datasets/{ds}/live"))
    fw = next(m for m in lv["asr"]["models"] if m["model_id"] == asr_id)
    assert fw["locked"]["promoted"] and fw["locked"]["dataset_id"] == new["id"] and fw["locked"]["wer"] == 0.123


def test_label_run_validation(label_flow, client):
    flow = label_flow
    ids, ds = flow["ids"], flow["unlabelled"]
    url = f"/api/label/datasets/{ds}/runs"
    _ok(client.post(url, json={"seg_model_id": ids["silero-vad"], "asr_model_ids": [ids["whisper-large-v3"]]}), 422)
    _ok(client.post(url, json={"seg_model_id": ids["pyannote-diarization"], "asr_model_ids": []}), 422)
    _ok(client.post(url, json={"seg_model_id": ids["pyannote-diarization"], "tasks": ["kws"]}), 422)
    _ok(client.post(url, json={"seg_model_id": ids["pyannote-diarization"], "asr_model_ids": [ids["whisper-large-v3"]],
                               "strategy": "single"}), 422)
    # transcripts only, with a speech-only segmenter, is fine
    _ok(client.post(url, json={"tasks": ["asr"], "seg_model_id": ids["silero-vad"],
                               "asr_model_ids": [ids["whisper-large-v3"]]}), 202)
    _ok(client.post(url, json={"tasks": ["asr"], "seg_model_id": ids["silero-vad"],
                               "asr_model_ids": [ids["whisper-large-v3"]]}), 409)  # one active run per dataset
