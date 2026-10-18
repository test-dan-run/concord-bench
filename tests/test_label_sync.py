"""Label packages: export, staged import, merge rules, conflicts, snapshot + undo, fresh installs."""

from __future__ import annotations

import json
import zipfile

import pytest

from concord.core import db
from concord.core.settings import reset_settings
from label_helpers import _ok, audio_only_dataset, label


def segments_of(client, ds):
    out = []
    for f in _ok(client.get(f"/api/label/datasets/{ds}/files")):
        out += _ok(client.get(f"/api/label/files/{f['id']}"))["segments"]
    return out


def export(client, ds, **kw):
    pkg = _ok(client.post("/api/sync/exports", json={"dataset_id": ds, "scope": "all", **kw}), 201)
    r = client.get(f"/api/sync/exports/{pkg['id']}/download")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    return pkg, r.content


def stage(client, tmp_path, blob, name="pkg.zip"):
    p = tmp_path / name
    p.write_bytes(blob)
    return _ok(client.post("/api/sync/imports", json={"local_path": str(p)}), 201)


def test_package_layout(labelled, client, tmp_path):
    ds = labelled["unlabelled"]
    pkg, blob = export(client, ds, include_outputs=True)
    (tmp_path / "p.zip").write_bytes(blob)
    with zipfile.ZipFile(tmp_path / "p.zip") as z:
        names = set(z.namelist())
        assert {"manifest.json", "files.csv", "segments.jsonl", "keywords.jsonl", "runs/model_outputs.jsonl",
                "runs/label_runs.jsonl"} <= names
        assert not any(n.startswith("audio/") for n in names)
        m = json.loads(z.read("manifest.json"))
        assert m["format"] == "concord-label-package" and m["counts"]["files"] == 7
        seg = json.loads(z.read("segments.jsonl").decode().splitlines()[0])
        assert {"file_sha256", "start", "end", "speaker", "text", "status", "confidence", "source",
                "updated_at", "uid"} <= set(seg)
        assert z.read("files.csv").decode().splitlines()[0] == "path,sha256,duration,sample_rate"
        assert z.read("keywords.jsonl") == b""
    assert pkg["counts"]["segments"] == labelled["results"]["summary"]["segments"]
    # nothing changed since: a "changed" export is empty; reviewed-only drops pseudo-labels
    nxt = _ok(client.post("/api/sync/exports", json={"dataset_id": ds, "scope": "changed"}), 201)
    assert nxt["counts"]["files"] == 0
    rev = _ok(client.post("/api/sync/exports", json={"dataset_id": ds, "include_pseudo": False}), 201)
    assert rev["counts"]["segments"] == 0


def test_conflict_resolve_and_undo(labelled, client, tmp_path):
    ds = labelled["unlabelled"]
    a = segments_of(client, ds)[0]
    _ok(client.patch(f"/api/label/segments/{a['id']}", json={"text": "Fixed on laptop.", "status": "confirmed"}))
    _, blob = export(client, ds)
    # meanwhile this install reviews the same segment differently
    _ok(client.patch(f"/api/label/segments/{a['id']}", json={"text": "Fixed on desktop."}))
    staged = stage(client, tmp_path, blob)
    assert staged["suggestions"][0]["dataset_id"] == ds and staged["suggestions"][0]["matched_files"] == 7
    pl = _ok(client.get(f"/api/sync/imports/{staged['id']}/plan?target_dataset_id={ds}"))
    assert pl["file_counts"] == {"matched": 7}
    assert pl["counts"]["conflict"] == 1 and pl["counts"]["unchanged"] == len(segments_of(client, ds)) - 1
    c = pl["conflicts"][0]
    assert c["local"]["text"] == "Fixed on desktop." and c["incoming"]["text"] == "Fixed on laptop."
    # apply without resolving: local kept, the conflict stays open
    applied = _ok(client.post(f"/api/sync/imports/{staged['id']}/apply", json={"target_dataset_id": ds}))
    assert applied["status"] == "applied" and applied["conflicts_open"] == 1
    assert segments_of(client, ds)[0]["text"] == "Fixed on desktop."
    out = _ok(client.post(f"/api/sync/imports/{staged['id']}/resolve", json={"key": c["key"], "choice": "incoming"}))
    assert out["package"]["conflicts_open"] == 0
    assert segments_of(client, ds)[0]["text"] == "Fixed on laptop."
    # undo restores the snapshot taken before the merge
    _ok(client.post(f"/api/sync/imports/{staged['id']}/undo"))
    assert segments_of(client, ds)[0]["text"] == "Fixed on desktop."
    _ok(client.post(f"/api/sync/imports/{staged['id']}/undo"), 422)
    hist = _ok(client.get(f"/api/sync/packages?dataset_id={ds}"))
    assert [h["direction"] for h in hist][:2] == ["import", "export"] and hist[0]["status"] == "undone"


def test_merge_rules_by_overlap(labelled, client, tmp_path):
    ds = labelled["unlabelled"]
    segs = segments_of(client, ds)
    a = segs[0]
    _ok(client.patch(f"/api/label/segments/{a['id']}", json={"text": "Reviewed elsewhere.", "status": "confirmed"}))
    _, blob = export(client, ds)
    # a second copy of the same audio, pseudo-labelled here later: different uids, same spans
    other = audio_only_dataset(client, tmp_path, "Second copy")
    labelled["unlabelled"] = other
    label(client, labelled)
    local = segments_of(client, other)
    b = local[1]
    _ok(client.post("/api/label/segments/bulk", json={"ids": [b["id"]], "action": "confirm"}))
    staged = stage(client, tmp_path, blob)
    pl = _ok(client.get(f"/api/sync/imports/{staged['id']}/plan?target_dataset_id={other}"))
    by = {(r["local"] or {}).get("id"): r["outcome"] for r in pl["rows"] if r.get("local")}
    assert by[local[0]["id"]] == "take_incoming"  # incoming reviewed, local not
    assert by[b["id"]] == "keep_local"  # local reviewed, incoming is a pseudo-label
    # both unreviewed: the newer (local, pseudo-labelled after the export) wins
    rest = {o for k, o in by.items() if k not in (local[0]["id"], b["id"])}
    assert rest <= {"keep_local", "unchanged"}, rest
    _ok(client.post(f"/api/sync/imports/{staged['id']}/apply", json={"target_dataset_id": other}))
    after = segments_of(client, other)
    assert after[0]["text"] == "Reviewed elsewhere." and after[0]["status"] == "confirmed" and after[0]["reviewed"]
    assert after[0]["uid"] == a["uid"]  # matched segments take the incoming uid, so the next sync matches by uid


def test_import_as_new_dataset_reuses_known_audio(labelled, client, tmp_path):
    _, blob = export(client, labelled["unlabelled"])
    staged = stage(client, tmp_path, blob)
    pl = _ok(client.get(f"/api/sync/imports/{staged['id']}/plan"))
    assert pl["file_counts"] == {"add": 7} and set(pl["counts"]) == {"add"}
    out = _ok(client.post(f"/api/sync/imports/{staged['id']}/apply", json={"new_name": "From laptop"}))
    d = _ok(client.get(f"/api/datasets/{out['dataset_id']}"))
    assert d["name"] == "From laptop" and d["usable_files"] == 6
    assert len(segments_of(client, d["id"])) == labelled["results"]["summary"]["segments"]


def test_fresh_install_from_package_with_audio(labelled, client, tmp_path, monkeypatch):
    _, blob = export(client, labelled["unlabelled"], include_audio=True, include_outputs=True)
    with zipfile.ZipFile(__import__("io").BytesIO(blob)) as z:
        assert sum(1 for n in z.namelist() if n.startswith("audio/")) == 7
    # a second install: empty data folder
    monkeypatch.setenv("CONCORD_DATA", str(tmp_path / "other-install"))
    reset_settings()
    db.reset_engines()
    db.migrate()
    assert _ok(client.get("/api/datasets")) == []
    staged = stage(client, tmp_path, blob)
    assert staged["suggestions"] == [] and staged["counts"]["audio_files"] == 7
    out = _ok(client.post(f"/api/sync/imports/{staged['id']}/apply", json={}))
    assert out["counts"]["model_outputs_added"] > 0 and len(out["counts"]["added_file_ids"]) == 7
    d = _ok(client.get(f"/api/datasets/{out['dataset_id']}"))
    assert d["usable_files"] == 6 and d["status"] == "ready"
    f = _ok(client.get(f"/api/label/datasets/{d['id']}/files"))[0]
    assert client.get(f"/api/audio/{f['id']}").status_code == 200
    assert client.get(f"/api/audio/{f['id']}/peaks").status_code == 200
    # undo removes the files the import added
    _ok(client.post(f"/api/sync/imports/{staged['id']}/undo"))
    assert _ok(client.get(f"/api/label/datasets/{d['id']}/files")) == []


def test_upload_without_zip_extension(labelled, client, tmp_path):
    """Browsers can hand over a download under another name; the package is found by its content."""
    _, blob = export(client, labelled["unlabelled"])
    uid = client.post("/api/uploads").json()["upload_id"]
    _ok(client.post(f"/api/uploads/{uid}/files", files=[("files", ("3f2a9c-download", blob))]))
    staged = _ok(client.post("/api/sync/imports", json={"upload_id": uid}), 201)
    assert staged["counts"]["segments"] == labelled["results"]["summary"]["segments"]


@pytest.mark.parametrize("payload", [b"not a zip", None])
def test_bad_packages(client, tmp_path, payload):
    if payload is None:
        p = tmp_path / "evil.zip"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("../escape.txt", "x")
        payload = p.read_bytes()
    p = tmp_path / "bad.zip"
    p.write_bytes(payload)
    r = client.post("/api/sync/imports", json={"local_path": str(p)})
    assert r.status_code == 422
