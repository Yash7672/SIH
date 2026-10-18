"""OCR analytics: hand-computed metric checks + /analytics/ocr contract.

The metric expectations below are computed by hand from a 4-plate example so
the maths is pinned to real numbers, not to whatever the implementation says:

  1. MH12AB1234 -> MH12AB1234   exact, 0 edits
  2. DL1CA5678  -> DL1CA5670    one substitution (8 read as 0)
  3. KA01XY2233 -> KA01XY233    one deletion (a 2 dropped)
  4. TS09AB1111 -> ""           no read: 10 deletions, still an error

plate_accuracy = 1/4, CER = 12 edits / 39 true chars.
"""

import json
from pathlib import Path

import pytest

from app.services.ocr_metrics import (
    LABEL_INDEX,
    MISSING,
    _REPORT_CACHE,
    align_chars,
    character_totals,
    confusion_from_pairs,
    levenshtein_distance,
    wilson_interval,
)

CASES = [
    ("MH12AB1234", "MH12AB1234"),
    ("DL1CA5678", "DL1CA5670"),
    ("KA01XY2233", "KA01XY233"),
    ("TS09AB1111", ""),
]


def pairs():
    return [align_chars(t, p) for t, p in CASES]


# ---------------------------------------------------------------- metrics ---


def test_plate_accuracy_and_error_rate():
    rows = [(t, p) for t, p in CASES]
    exact = sum(1 for t, p in rows if t == p)
    accuracy = exact / len(rows)
    assert accuracy == 0.25
    assert 1 - accuracy == 0.75


def test_missed_plate_counts_as_an_error():
    # The no-read sample is a full error AND contributes every character as a
    # deletion - it must not vanish from either metric.
    only_missed = [align_chars("TS09AB1111", "")]
    assert len(only_missed[0]) == 10
    assert all(t != MISSING for t, _ in only_missed[0])
    totals = character_totals(only_missed)
    assert totals["cer"] == 1.0
    assert totals["character_accuracy"] == 0.0


def test_cer_and_character_accuracy():
    totals = character_totals(pairs())
    assert totals["total_true_chars"] == 39
    assert totals["total_edits"] == 12  # 0 + 1 substitution + 1 deletion + 10 deletions
    assert totals["cer"] == pytest.approx(12 / 39)
    assert totals["character_accuracy"] == pytest.approx(27 / 39)


def test_alignment_substitution_and_deletion():
    sub = align_chars("DL1CA5678", "DL1CA5670")
    assert sub.count(("8", "0")) == 1
    assert levenshtein_distance("DL1CA5678", "DL1CA5670") == 1

    deleted = align_chars("KA01XY2233", "KA01XY233")
    assert deleted.count(("2", MISSING)) == 1
    assert levenshtein_distance("KA01XY2233", "KA01XY233") == 1


def test_alignment_insertion():
    # One hallucinated trailing character: an (MISSING, pred) pair, one edit.
    inserted = align_chars("DL1CA5678", "DL1CA56788")
    assert inserted.count((MISSING, "8")) == 1
    assert sum(1 for t, p in inserted if t != p) == 1
    assert levenshtein_distance("DL1CA5678", "DL1CA56788") == 1


def test_confusion_counts():
    matrix = confusion_from_pairs(pairs())
    counts = matrix["counts"]
    assert sum(sum(row) for row in counts) == 39  # all aligned pairs
    assert counts[LABEL_INDEX["8"]][LABEL_INDEX["0"]] == 1  # 8 read as 0
    assert counts[LABEL_INDEX["2"]][LABEL_INDEX[MISSING]] == 1  # dropped 2
    assert counts[LABEL_INDEX["1"]][LABEL_INDEX[MISSING]] == 4  # the four 1s of the missed plate
    assert counts[LABEL_INDEX["M"]][LABEL_INDEX["M"]] == 1
    assert len(matrix["labels"]) == 37
    # Row-normalised: the 8-row is entirely the single 8->0 error.
    assert matrix["normalized"][LABEL_INDEX["8"]][LABEL_INDEX["0"]] == pytest.approx(1.0)
    top = matrix["top"]
    assert top[0]["true"] == "1" and top[0]["pred"] == MISSING and top[0]["count"] == 4


def test_wilson_interval_known_values():
    assert wilson_interval(1, 4) == pytest.approx((0.04559, 0.69936), abs=1e-4)
    assert wilson_interval(3, 4) == pytest.approx((0.30064, 0.95441), abs=1e-4)
    assert wilson_interval(6, 6) == pytest.approx((0.60966, 1.0), abs=1e-4)
    assert wilson_interval(0, 4) == pytest.approx((0.0, 0.48990), abs=1e-4)


# --------------------------------------------------------------- endpoint ---


def _write_report(path: Path, **overrides) -> Path:
    payload = {
        "status": "ok",
        "target": {
            "value": 0.90,
            "met_point": True,
            "met_proven": False,
            "headline_set": "combined",
            "flag": "real sample too small: 0 real plates (< 30); headline is the combined set",
            "accuracy": 1.0,
            "ci_low": 0.6096,
            "ci_high": 1.0,
        },
        "kpis": {"plate_accuracy": 1.0, "error_rate": 0.0, "wrong_confident_count": 0},
        "sets": [{"name": "combined", "n": 6, "exact_matches": 6, "plate_accuracy": 1.0}],
        "confusion": {"labels": [], "counts": [], "normalized": [], "per_char": [], "top": []},
        "confidence_histogram": {"bins": [], "correct": [], "wrong": []},
        "metadata": {"generated_at": "2026-10-18T00:00:00+00:00", "commit": "abc1234"},
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture()
def report_path(tmp_path, monkeypatch):
    import app.api.v1.ocr_metrics as ocr_api

    _REPORT_CACHE.clear()
    path = tmp_path / "ocr_report.json"
    monkeypatch.setattr(ocr_api, "REPORT_PATH", path)
    yield path
    _REPORT_CACHE.clear()


def test_endpoint_serves_report(client, demo_tokens, report_path):
    from tests.conftest import auth

    _write_report(report_path)
    r = client.get("/api/v1/analytics/ocr", headers=auth(demo_tokens["cop"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["target"]["met_point"] is True
    assert body["metadata"]["commit"] == "abc1234"

    # Cached by mtime: editing the file is picked up, no restart needed.
    report_path.write_text(
        json.dumps({"status": "ok", "metadata": {"commit": "def5678"}}), encoding="utf-8"
    )
    r2 = client.get("/api/v1/analytics/ocr", headers=auth(demo_tokens["admin"]))
    assert r2.status_code == 200
    assert r2.json()["metadata"]["commit"] == "def5678"


def test_endpoint_no_report(client, demo_tokens, report_path):
    from tests.conftest import auth

    assert not report_path.exists()
    r = client.get("/api/v1/analytics/ocr", headers=auth(demo_tokens["cop"]))
    assert r.status_code == 200
    assert r.json() == {"status": "no_report", "hint": "Run: python scripts/eval_ocr.py"}


def test_endpoint_rbac(client, demo_tokens, report_path):
    from tests.conftest import auth

    _write_report(report_path)
    assert client.get("/api/v1/analytics/ocr").status_code == 401
    assert client.get(
        "/api/v1/analytics/ocr", headers=auth(demo_tokens["citizen"])
    ).status_code == 403
    assert client.get(
        "/api/v1/analytics/ocr", headers=auth(demo_tokens["volunteer"])
    ).status_code == 403
