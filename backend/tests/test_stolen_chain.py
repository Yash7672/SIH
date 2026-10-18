"""The whole chain, from a citizen's phone to a cop's dashboard.

Every other suite here tests one piece: the normaliser, the reader, the verdict,
the socket, the privacy rules. This one walks the path a reported car actually
takes -

    a citizen reports DL 1 ZA 9092
      -> a cop verifies it, which puts DL1ZA9092 on the hot-list
        -> a volunteer's camera frames the car
          -> frames go out over the socket
            -> STOLEN within three seconds, one alert, one sighting

and then the two branches that matter just as much:

    - the read comes out as `DL12AG092` (Z read as 2, 9 read as G), and the
      chain still must NOT raise a police alert;
    - the cop rejects the complaint, and the same frames must produce nothing at
      all.

The reported bug was a red STOLEN banner over a card reading "not on the
hotlist", for a plate that was read at 93% and was not the listed car. That is a
failure of the *decision*, so it is tested at the only level where the decision
is visible: the whole chain, with real complaints, a real database and the real
socket in between.
"""

from __future__ import annotations

import time

import pytest

import app.api.v1.live_scan as ls
from tests.conftest import auth
from tests.test_live_scan_ws import (
    FRAME_H,
    FRAME_W,
    _StubDetector,
    _auth,
    _collect,
    _drain,
    _frame,
    register_device,  # noqa: F401 - used as a pytest fixture in some of these tests
)

# The plate in the report: the citizen writes it with spaces, the database holds
# it without.
REPORTED = "DL1ZA9092"
CITIZEN_TEXT = "DL 1 ZA 9092"
# The same car, reported the other way: the district block with its leading zero,
# as the complaint form and the RC spell it. A plate pair no other test touches, so
# the hot-list row this test creates cannot be confused with a leftover one - the
# test harness builds the schema once per session and never truncates between
# tests, so a shared plate would make the assertions below meaningless.
PADDED = "DL05ZQ1234"
UNPADDED = "DL5ZQ1234"
# What the camera actually managed to read on the day: two characters wrong, both
# classic OCR confusions, and a string that is itself a perfectly valid plate.
MISREAD = "DL12AG092"


def _complaint(client, token, plate, complaint_type="Stolen vehicle"):
    r = client.post(
        "/api/v1/complaints",
        headers=auth(token),
        data={"plate": plate, "complaint_type": complaint_type, "description": "chain test"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _verify(client, token, complaint_id):
    r = client.post(
        f"/api/v1/complaints/{complaint_id}/verify",
        headers=auth(token),
        json={"fir_reference": f"FIR/2026/{complaint_id[:8]}"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _clear_hotlist(client, token, hotlist_id):
    """Take a verified plate off the list, the way an operator does.

    Not `POST /complaints/{id}/reject`: that endpoint only applies to a complaint
    still under review and 409s once it is HOTLISTED - which is the correct
    behaviour, and is why the withdrawal of a *verified* report is this PATCH.
    """
    r = client.patch(
        f"/api/v1/hotlist/{hotlist_id}",
        headers=auth(token),
        json={"status": "CLOSED"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _reject(client, token, complaint_id):
    r = client.post(f"/api/v1/complaints/{complaint_id}/reject", headers=auth(token))
    assert r.status_code == 200, r.text
    return r.json()


def _count_sightings(plate: str) -> int:
    """Sightings recorded for one plate.

    Joined through the hot-list, because `sightings` deliberately has no `plate`
    column of its own - the plate belongs to the hot-list entry, and
    denormalising it onto every sighting row would have made a plate copyable out
    of the sightings table, which is the whole reason it is not there.
    """
    from app.db.session import SessionLocal
    from app.models import Hotlist, Sighting

    db = SessionLocal()
    try:
        return (
            db.query(Sighting)
            .join(Hotlist, Sighting.hotlist_id == Hotlist.id)
            .filter(Hotlist.plate == plate)
            .count()
        )
    finally:
        db.close()


def _active_hotlist() -> list:
    """The plates the live path would actually match against.

    Read through the same query `live_scan._active_hotlist_plates` uses, and
    through the enum rather than a string literal: the enum values are
    ACTIVE / FIR_CONFIRMED, so a hard-coded "HOTLISTED" is a row that does not
    exist rather than a plate that does.
    """
    from app.db.session import SessionLocal
    from app.models import Hotlist, HotlistStatus

    db = SessionLocal()
    try:
        rows = db.query(Hotlist).filter(
            Hotlist.status.in_([HotlistStatus.ACTIVE, HotlistStatus.FIR_CONFIRMED])
        )
        return [h.plate for h in rows]
    finally:
        db.close()


@pytest.fixture
def one_car(monkeypatch):
    """One car with one plate, 153 px wide - comfortably readable."""

    def px(nx1, ny1, nx2, ny2):
        return [nx1 * FRAME_W, ny1 * FRAME_H, nx2 * FRAME_W, ny2 * FRAME_H]

    vehicles = _StubDetector(px(0.30, 0.30, 0.70, 0.70), cls=2, conf=0.91)
    plates = _StubDetector(px(0.44, 0.60, 0.56, 0.645), cls=0, conf=0.88)
    monkeypatch.setattr(ls, "get_vehicle_model", lambda: vehicles)
    monkeypatch.setattr(ls, "_load_plate_detector", lambda *a, **k: plates)
    return {"vehicles": vehicles, "plates": plates}


@pytest.fixture
def alerts(monkeypatch):
    """Every police alert the server tries to broadcast, captured in order."""
    seen = []

    async def _fake_broadcast(event, payload):
        seen.append((event, payload))

    monkeypatch.setattr(ls.alert_manager, "broadcast", _fake_broadcast)
    return seen


def _stub_read(text, conf, char_confs=None, seq=None):
    """A stubbed OCR read. Optionally a different read per call.

    ``verified: True`` - the stub stands in for the real ``run_ocr``, which
    cross-checks every glyph when the model is present. Tests that care about
    what happens when nobody checked have their own cases in
    ``test_char_verifier.py``.
    """
    call = {"n": 0}

    def _read(crop):
        call["n"] += 1
        if seq:
            plate, this_conf = seq[min(call["n"] - 1, len(seq) - 1)]
        else:
            plate, this_conf = text, conf
        out = {
            "text": plate,
            "norm": plate,
            "conf": this_conf,
            "valid": True,
            "verified": True,
        }
        if char_confs:
            out["char_confs"] = tuple(char_confs)
        return out

    return _read


# --------------------------------------------------------------------------- #
# the reported bug: the whole chain, end to end
# --------------------------------------------------------------------------- #
def test_a_reported_plate_is_flagged_within_three_seconds_and_alerted_once(
    client, demo_tokens, register_device, one_car, alerts, monkeypatch
):
    """The chain that has to work.

    `DL 1 ZA 9092` is reported and verified. Then the camera reads it twice, in
    two different frames, and the second frame has to produce STOLEN - with one
    police alert and one sighting row, not one per frame.
    """
    from app.services.cache import cache_service

    # ---- the citizen reports it ------------------------------------------
    complaint = _complaint(client, demo_tokens["citizen"], CITIZEN_TEXT)
    assert complaint["plate"] == REPORTED, "the spaces were normalised away at intake"
    assert complaint["status"] == "PENDING"

    # Nothing is on the hot-list yet. This is the state the reported bug was seen
    # in: a red banner over a car nobody had reported.
    assert REPORTED not in _active_hotlist()

    # ---- the cop verifies it ---------------------------------------------
    verified = _verify(client, demo_tokens["cop"], complaint["id"])
    assert verified["complaint"]["status"] == "HOTLISTED"
    assert verified["hotlist_id"]
    assert REPORTED in _active_hotlist(), "verify must put the plate on the hot-list"

    # The cache is what the live path reads first. `verify` refreshes it; if it
    # did not, this test would still pass through the DB fallback and the chain
    # would be one slow request slower in production for no reason.
    cached = cache_service.get_active_plates()
    assert cached is None or REPORTED in cached

    # ---- the camera reads it ---------------------------------------------
    # 0.78 with the `Z` at 0.41: below the single-read bar, below the 0.8 at
    # which a track is considered settled, and one doubtful glyph. That last part
    # matters - a track read at 0.8 or above is settled after one frame and never
    # re-read, so a consensus cannot be observed on it at all. This is therefore
    # the hardest path to STOLEN, and the one the reported failure took.
    monkeypatch.setattr(
        ls,
        "run_ocr",
        _stub_read(REPORTED, 0.78, char_confs=[0.99, 0.99, 0.97, 0.41, 0.98, 0.96, 0.97, 0.98, 0.99, 0.99]),
    )
    device_id = register_device("ChainCam")

    before = _count_sightings(REPORTED)
    started = time.monotonic()

    with client.websocket_connect("/api/v1/ws/scan") as ws:
        assert _auth(ws, demo_tokens["volunteer"], device_id)["type"] == "ready"
        first = None
        stolen = None
        for seq in range(1, 6):
            ws.send_json(_frame(seq))
            msg, seen = _drain(ws, "plate", limit=6, timeout=6.0)
            if msg is None:
                continue
            if first is None:
                first = msg
            if msg["stolen"]:
                stolen = msg
                break
            # Under the 15 fps limiter, or the frame is discarded before the
            # plate pass ever sees it.
            time.sleep(0.08)

    elapsed = time.monotonic() - started

    assert first is not None, "the camera never produced a read"
    assert first["stolen"] is False, (
        "one read must not be enough; the first frame is corroboration, not proof"
    )
    assert first["verdict"]["state"] == "POSSIBLE", first["verdict"]
    assert first["verdict"]["matches"] == REPORTED, (
        "an exact match that is not yet corroborated is POSSIBLE, not CLEAR"
    )
    assert stolen is not None, f"no frame produced STOLEN; last was {first['verdict']}"
    assert elapsed < 3.0, f"the verdict took {elapsed:.1f}s across a handful of frames"

    # ---- the verdict says what it means ----------------------------------
    vd = stolen["verdict"]
    assert vd["state"] == "STOLEN"
    assert vd["plate"] == REPORTED
    assert vd["matches"] == REPORTED, "an exact match, not a fuzzy one"
    assert vd["reads"] >= 2, "consensus is the reason, and it must be visible"
    assert vd["reason"], "a verdict without a reason cannot be argued with"
    assert stolen["police_alerted"] is True

    # ---- and exactly once, in two honest steps ----------------------------
    # A 0.78 read is above the review floor, so the portal is first woken with
    # `needs_review` while the verdict is still POSSIBLE, and woken again -
    # without the flag - the moment the second read corroborates STOLEN. Two
    # broadcasts, but only one *confirmed* alert and one sighting row: the
    # review step writes nothing, so it can never eat the cooldown the
    # confirmed step needs.
    assert len(alerts) == 2, f"{len(alerts)} police alerts for one car"
    for event, _payload in alerts:
        assert event == "hotlist_detection", event
    review_event, review_payload = alerts[0]
    stolen_event, payload = alerts[1]
    assert review_payload["needs_review"] is True, (
        "the 0.78 read must reach the portal flagged for verification"
    )
    assert payload["needs_review"] is False, (
        "the corroborated alert must not still say 'merely verify'"
    )
    assert payload["plate"] == REPORTED
    # The payload shape is what the police dashboard reads; this is the contract,
    # and the verdict work must not have changed it.
    for key in ("plate", "latitude", "longitude", "confidence", "hotlist_id"):
        assert key in payload, f"the alert lost `{key}`"
        assert key in review_payload, f"the review alert lost `{key}`"

    assert _count_sightings(REPORTED) == before + 1


def test_every_read_of_a_parked_stolen_car_alerts_again_but_files_one_row(
    client, demo_tokens, register_device, one_car, alerts, monkeypatch
):
    """A parked stolen car must alert on every read - and file one row.

    Every detection at or above ``ALERT_MIN_CONFIDENCE`` wakes the dashboards:
    the first read (POSSIBLE, flagged `needs_review`), and every read after the
    verdict turns STOLEN. What the 60 s cooldown still throttles is the *row*:
    one sighting per plate+device per minute, so the timeline and heat layer
    stay clean while the portal hears the car every time it is seen.

    The read is deliberately a *marginal* 0.79: below `PLATE_CONF_DONE`, so the
    track never settles and keeps being re-read for its full three attempts.
    That is the only way to observe several verdicts on one parked car - a
    confident read settles the track after one, which is correct but makes the
    test vacuous.
    """
    complaint = _complaint(client, demo_tokens["citizen"], CITIZEN_TEXT)
    _verify(client, demo_tokens["cop"], complaint["id"])
    # Two reads within the 5 s consensus window are enough, and every character
    # is read confidently so nothing about the string is in doubt.
    monkeypatch.setattr(
        ls, "run_ocr", _stub_read(REPORTED, 0.79, char_confs=[0.99] * 10)
    )
    device_id = register_device("RepeatCam")
    before = _count_sightings(REPORTED)

    # Collected from the start rather than drained per frame, because the
    # assertion is about reads *after* the alert - and draining would have
    # thrown exactly those away.
    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"], device_id)
        for seq in range(1, 6):
            ws.send_json(_frame(seq))
            # Under the 15 fps limiter, or the frame is discarded before the
            # plate pass ever sees it.
            time.sleep(0.08)
        seen = [m for m in _collect(ws, 6.0) if m.get("type") == "plate"]

    assert len(seen) >= 2, f"only {len(seen)} plate messages across five frames"
    # One alert per read: the first 0.79 read is above the review floor and is
    # flagged `needs_review`; every read after the verdict is STOLEN alerts
    # without the flag. Repeats carry no sighting_id because no new row was
    # written for them.
    assert len(alerts) == len(seen), (
        f"{len(alerts)} alerts for {len(seen)} reads of one parked car"
    )
    assert alerts[0][1]["needs_review"] is True, "the first read must say 'verify'"
    assert all(p["needs_review"] is False for _, p in alerts[1:]), (
        "a corroborated read must not still say 'merely verify'"
    )
    for _event, payload in alerts:
        assert payload["plate"] == REPORTED
    states = [m["verdict"]["state"] for m in seen]
    # The first read is not corroborated, so it is POSSIBLE; from the second
    # onwards the phone keeps being told the car is stolen. The banner must not
    # vanish, and the state must not fall back on the strength of "we already
    # told someone".
    assert states[-1] == "STOLEN", states
    assert "STOLEN" in states[1:], states
    # Every read that woke the portal claims it did.
    flagged = [m for m in seen if m["police_alerted"]]
    assert len(flagged) == len(seen), (
        f"{len(flagged)} of {len(seen)} reads claim to have filed an alert"
    )
    # The row is still throttled: evidence, not one row per frame.
    assert _count_sightings(REPORTED) == before + 1, "the 60 s cooldown let rows through"


def test_a_camera_with_no_device_says_it_cannot_alert_and_does_not_claim_to(
    client, demo_tokens, register_device, one_car, alerts, monkeypatch
):
    """A session that authenticates without a device must not claim it alerted.

    `scripts/stream_camera.py` - the fixed-camera path the README walks through
    - used to open the socket with `device_id: None`. The server accepted that
    (`_device_allowed` allows an unidentified scanner), and the verdict was
    right: the operator saw STOLEN on a listed car. But a sighting row is only
    ever written for a device (`sightings.device_id` is not nullable), so there
    was nothing to record, nothing to broadcast, and `police_alerted` came back
    True regardless. The console printed POLICE-ALERTED while the police
    dashboard stayed dark - the one claim the flag exists to make honestly.

    The banner must survive: the car really is on the list, and the volunteer
    looking at a stolen car is the person who most needs to know it.
    """
    complaint = _complaint(client, demo_tokens["citizen"], CITIZEN_TEXT)
    _verify(client, demo_tokens["cop"], complaint["id"])
    monkeypatch.setattr(
        ls,
        "run_ocr",
        _stub_read(REPORTED, 0.79, char_confs=[0.99] * 10),
    )

    before = _count_sightings(REPORTED)
    with client.websocket_connect("/api/v1/ws/scan") as ws:
        assert _auth(ws, demo_tokens["volunteer"], None)["type"] == "ready"
        seen = []
        for seq in range(1, 6):
            ws.send_json(_frame(seq))
            time.sleep(0.08)
        seen = [m for m in _collect(ws, 6.0) if m.get("type") == "plate"]

    assert seen, "the camera never produced a read"
    # The verdict is unaffected: the plate is genuinely on the list.
    assert [m["verdict"]["state"] for m in seen][-1] == "STOLEN", seen[-1]["verdict"]
    # What must not happen is the claim.
    flagged = [m for m in seen if m["police_alerted"]]
    assert not flagged, f"{len(flagged)} frames claimed to have paged the police without a device"
    assert alerts == [], "a device-less session broadcast a police alert"
    assert _count_sightings(REPORTED) == before, "a sighting was written without a device"


# --------------------------------------------------------------------------- #
# the district block: the same car, reported with the leading zero
# --------------------------------------------------------------------------- #
def test_a_zero_padded_report_still_pages_the_police(
    client, demo_tokens, register_device, one_car, alerts, monkeypatch
):
    """`DL01ZA9092` on the complaint, `DL1ZA9092` under the camera: one car.

    The district code is officially two digits, and the complaint form, the RC
    and the police record all write it that way - while the plate on the car, and
    the person typing, usually leave the zero out. Those were two different
    strings: no exact hot-list match, only a 0.90 fuzzy one, which is amber
    POSSIBLE, writes no sighting and pages nobody. A stolen car drives on with the
    phone reading "might be".

    This is the chain from the top of the file with exactly one thing changed -
    the citizen's plate is filed the way the paperwork spells it - and it has to
    reach the same STOLEN, one alert and one sighting.
    """
    from app.services.cache import cache_service

    # Guard: the harness does not reset the database between tests, so if some
    # other test ever starts using this car the assertions below would pass off
    # its leftovers instead of this test's own work.
    assert not any(p in _active_hotlist() for p in (PADDED, UNPADDED)), "test state leaked"

    # ---- the citizen reports it, zero and all ----------------------------
    complaint = _complaint(client, demo_tokens["citizen"], PADDED)
    assert complaint["plate"] == PADDED, "the complaint keeps the spelling the citizen used"
    assert complaint["status"] == "PENDING"

    # ---- the cop verifies it ---------------------------------------------
    verified = _verify(client, demo_tokens["cop"], complaint["id"])
    assert verified["hotlist_id"]
    assert PADDED in _active_hotlist()

    # The cache is what the live path reads first, and it holds the canonical
    # form - neither spelling verbatim unless it happens to be the canonical one.
    # A lookup that is not spelling-aware cannot find the entry.
    cached = cache_service.get_active_plates()
    assert cached is None or PADDED in cached or UNPADDED in cached

    # ---- the camera reads the car, without the zero ----------------------
    # 0.79 with every glyph solid: below the single-read bar, so the track is
    # corroborated across two frames - the realistic case, and the one that also
    # proves consensus still works when the two reads and the listing disagree on
    # the spelling.
    monkeypatch.setattr(ls, "run_ocr", _stub_read(UNPADDED, 0.79, char_confs=[0.99] * 9))
    device_id = register_device("PaddedCam")

    before = _count_sightings(PADDED)
    with client.websocket_connect("/api/v1/ws/scan") as ws:
        assert _auth(ws, demo_tokens["volunteer"], device_id)["type"] == "ready"
        stolen = None
        for seq in range(1, 6):
            ws.send_json(_frame(seq))
            msg, _seen = _drain(ws, "plate", limit=6, timeout=6.0)
            if msg is None:
                continue
            if msg["stolen"]:
                stolen = msg
                break
            time.sleep(0.08)

    assert stolen is not None, "a report filed with a leading zero never reached the police"
    vd = stolen["verdict"]
    assert vd["state"] == "STOLEN", vd
    assert vd["plate"] == UNPADDED, "the verdict reports the plate the camera read"
    assert vd["matches"] == UNPADDED
    assert stolen["police_alerted"] is True

    # The 0.79 read is above the review floor, so the portal hears once while
    # the verdict is still POSSIBLE (flagged `needs_review`) and once when the
    # second frame confirms it - one confirmed alert, one sighting, either way.
    assert len(alerts) == 2, f"{len(alerts)} police alerts for one car"
    assert alerts[0][0] == "hotlist_detection"
    assert alerts[0][1]["needs_review"] is True
    assert alerts[1][1]["needs_review"] is False
    assert _count_sightings(PADDED) == before + 1, "the sighting was not filed against the entry"


# --------------------------------------------------------------------------- #
# the reported bug: the read that is not the listed car
# --------------------------------------------------------------------------- #
def test_the_misread_of_a_reported_plate_never_reaches_the_police(
    client, demo_tokens, register_device, one_car, alerts, monkeypatch
):
    """`DL12AG092` is not `DL1ZA9092`, and must not be treated as it.

    This is the exact failure that was reported: a real plate, a real hot-list
    entry for a near-identical car, and a read that differs in two characters -
    both of them classic confusions. `DL12AG092` is itself a syntactically valid
    Delhi plate, so no validator can catch it; only the consensus and the
    character confidence can, and they must.

    The chain before this point is identical to the passing case above: same
    citizen report, same cop verification, same camera. Only the read differs.
    That is the point - the whole chain runs, and the answer at the end is
    different.
    """
    complaint = _complaint(client, demo_tokens["citizen"], CITIZEN_TEXT)
    _verify(client, demo_tokens["cop"], complaint["id"])
    assert REPORTED in _active_hotlist()

    # Read at 93%: high enough that the old "confidence >= 0.6" line would have
    # accepted it without a second look.
    monkeypatch.setattr(ls, "run_ocr", _stub_read(MISREAD, 0.93))
    device_id = register_device("MisreadCam")

    before = _count_sightings(MISREAD)
    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"], device_id)
        plates = []
        for seq in range(1, 6):
            ws.send_json(_frame(seq))
            msg, _seen = _drain(ws, "plate", limit=6, timeout=6.0)
            if msg:
                plates.append(msg)
            time.sleep(0.08)

    assert plates, "the camera produced no read at all"
    for msg in plates:
        assert msg["stolen"] is False, "a misread raised a police alert"
        assert msg["verdict"]["state"] != "STOLEN"
        assert msg["police_alerted"] is False

    assert alerts == [], f"the police were alerted from a misread: {alerts}"
    assert _count_sightings(MISREAD) == before, "a misread was written to the database"


def test_a_misread_that_looks_close_shows_amber_and_not_red(
    client, demo_tokens, register_device, one_car, monkeypatch
):
    """The same read must still *tell the volunteer something*.

    Refusing to alert is not the same as refusing to say anything. `DL12AG092`
    and `DL1ZA9092` are one substitution and one deletion apart inside the same
    state code, so the verdict is POSSIBLE - "this might be that listed car, look
    again" - which sends no alert and writes no row, and is a genuinely useful
    thing to put on the screen.
    """
    complaint = _complaint(client, demo_tokens["citizen"], CITIZEN_TEXT)
    _verify(client, demo_tokens["cop"], complaint["id"])
    monkeypatch.setattr(ls, "run_ocr", _stub_read(MISREAD, 0.93))
    device_id = register_device("AmberCam")

    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"], device_id)
        ws.send_json(_frame(1))
        msg, seen = _drain(ws, "plate", limit=6, timeout=6.0)

    assert msg is not None, f"no read; saw {[m['type'] for m in seen]}"
    vd = msg["verdict"]
    assert vd["state"] == "POSSIBLE", vd
    # POSSIBLE is explicitly not an alert, and the phone is told so it cannot
    # invent its own opinion later.
    assert msg["stolen"] is False
    assert msg["police_alerted"] is False
    assert vd["reads"] == 1


def test_the_misread_is_reported_as_unsure_rather_than_as_an_answer(
    client, demo_tokens, register_device, one_car, monkeypatch
):
    """Where the doubt is gets said out loud.

    The volunteer walking up to the car is the last line of defence, and they can
    only do their job if they are told *which* character to look at. `G` and `2`
    are both in the ambiguity table, so both are flagged with their index.
    """
    monkeypatch.setattr(
        ls,
        "run_ocr",
        _stub_read(MISREAD, 0.93, char_confs=[0.99, 0.99, 0.99, 0.62, 0.99, 0.99, 0.41, 0.99, 0.99, 0.99]),
    )
    device_id = register_device("DoubtCam")

    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"], device_id)
        ws.send_json(_frame(1))
        msg, seen = _drain(ws, "plate", limit=6, timeout=6.0)

    assert msg is not None, f"no read; saw {[m['type'] for m in seen]}"
    weak = msg["verdict"]["weak"]
    assert weak, "the read was doubtful but did not say where"
    for i in weak:
        assert 0 <= i < len(MISREAD), f"index {i} is not a character of {MISREAD}"
    # Both doubtful characters are flagged: the Z-for-2 style confusion at index
    # 3 and the 9-for-G one at index 6.
    assert 3 in weak and 6 in weak, weak
    assert msg["verdict"]["ambiguous"] is True


# --------------------------------------------------------------------------- #
# the other branch: the complaint is rejected
# --------------------------------------------------------------------------- #
def test_rejecting_the_complaint_stops_every_alert(
    client, demo_tokens, register_device, one_car, alerts, monkeypatch
):
    """A withdrawn report must leave no trace on the live path.

    The chain runs, the car is framed, and the plates read perfectly - and
    because there is no hot-list entry any more, the answer is CLEAR. Not
    POSSIBLE, not UNREAD: a plate that was read and is not listed is CLEAR, and
    the volunteer is told so rather than being left to guess.

    The report is withdrawn by closing its hot-list entry, which is the only
    route that exists: `/complaints/{id}/reject` applies to a complaint still
    under review and correctly refuses a verified one.
    """
    complaint = _complaint(client, demo_tokens["citizen"], CITIZEN_TEXT)
    verified = _verify(client, demo_tokens["cop"], complaint["id"])
    assert REPORTED in _active_hotlist()

    _clear_hotlist(client, demo_tokens["cop"], verified["hotlist_id"])
    assert REPORTED not in _active_hotlist(), "closing must take it off the hot-list"

    monkeypatch.setattr(ls, "run_ocr", _stub_read(REPORTED, 0.97))
    device_id = register_device("RejectedCam")

    before = _count_sightings(REPORTED)
    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"], device_id)
        plates = []
        for seq in range(1, 5):
            ws.send_json(_frame(seq))
            msg, _seen = _drain(ws, "plate", limit=6, timeout=6.0)
            if msg:
                plates.append(msg)
            time.sleep(0.08)

    assert plates, "the camera produced no read at all"
    for msg in plates:
        assert msg["verdict"]["state"] == "CLEAR", msg["verdict"]
        assert msg["stolen"] is False
        assert msg["police_alerted"] is False
    assert alerts == [], f"a rejected complaint still alerted: {alerts}"
    assert _count_sightings(REPORTED) == before, "a rejected plate was written to the database"


def test_an_unrelated_plate_is_clear_all_the_way_through(
    client, demo_tokens, register_device, one_car, alerts, monkeypatch
):
    """The plain privacy case, with the whole chain in front of it.

    A car that was never reported must produce CLEAR, no sighting row and no
    alert - the same guarantee `test_privacy.py` checks, but reached through a
    real complaint and a real verified hot-list, so "not on the hot-list" is
    tested against an actual hot-list rather than an empty one.
    """
    complaint = _complaint(client, demo_tokens["citizen"], CITIZEN_TEXT)
    _verify(client, demo_tokens["cop"], complaint["id"])

    other = "MH12JK4567"
    monkeypatch.setattr(ls, "run_ocr", _stub_read(other, 0.97))
    device_id = register_device("OtherCam")

    before = _count_sightings(other)
    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"], device_id)
        ws.send_json(_frame(1))
        msg, seen = _drain(ws, "plate", limit=6, timeout=6.0)

    assert msg is not None, f"no read; saw {[m['type'] for m in seen]}"
    assert msg["verdict"]["state"] == "CLEAR", msg["verdict"]
    assert msg["verdict"]["plate"] == other
    assert alerts == []
    assert _count_sightings(other) == before


# --------------------------------------------------------------------------- #
# the phone's view of all of it
# --------------------------------------------------------------------------- #
def test_the_phone_receives_one_verdict_and_nothing_to_reinterpret(
    client, demo_tokens, register_device, one_car, monkeypatch
):
    """Everything the phone is given, in one place.

    The reported contradiction - a red banner above a card saying "not on the
    hotlist" - was two client-side decisions disagreeing with the server's. This
    pins the shape that removes the second decision: the verdict, and only the
    verdict.
    """
    complaint = _complaint(client, demo_tokens["citizen"], CITIZEN_TEXT)
    _verify(client, demo_tokens["cop"], complaint["id"])
    monkeypatch.setattr(ls, "run_ocr", _stub_read(REPORTED, 0.96))
    device_id = register_device("ShapeCam")

    with client.websocket_connect("/api/v1/ws/scan") as ws:
        _auth(ws, demo_tokens["volunteer"], device_id)
        ws.send_json(_frame(1))
        msg, _seen = _drain(ws, "plate", limit=6, timeout=6.0)

    assert msg is not None
    verdict = msg["verdict"]
    for key in ("state", "plate", "confidence", "reads", "ambiguous", "repaired", "reason", "weak"):
        assert key in verdict, f"the verdict is missing `{key}`"
    assert verdict["state"] in {"STOLEN", "POSSIBLE", "CLEAR", "UNREAD"}
    # `stolen` must agree with the verdict, exactly, on every frame. The phone
    # reads both, and any disagreement between them is the reported bug.
    assert msg["stolen"] == (verdict["state"] == "STOLEN")
    # And the plate on the verdict is the plate on the message.
    assert verdict["plate"] == msg["norm"]