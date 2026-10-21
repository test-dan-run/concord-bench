"""The contract samples (CONTRACTS.md) are valid, and today's ingest reads the sample packages."""

import importlib.util
from pathlib import Path

from concord_bench.datasets.ingest import inspect_source

REPO = Path(__file__).resolve().parents[1]
SAMPLES = REPO / "contracts" / "samples"


def _checker():
    spec = importlib.util.spec_from_file_location("contracts_check", REPO / "contracts" / "check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_samples_follow_the_contract():
    assert _checker().check_samples() == []


def test_checker_catches_a_bad_package(tmp_path):
    check = _checker()
    (tmp_path / "manifest.json").write_text('{"format": "concord-reference-package", "format_version": 2}')
    problems = check.check_package(tmp_path)
    assert any("format_version 2" in p for p in problems)
    assert any("files.csv missing" in p for p in problems)


def test_ingest_reads_the_sample_packages():
    for name in ("reference-package-complete", "reference-package-handmade"):
        rep = inspect_source(SAMPLES / name)
        assert rep["audio_files"] == 1 and not rep["unmatched"], name
        assert rep["tasks"]["spk_asr"]["records"] == 3, name
