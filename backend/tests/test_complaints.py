from tests.conftest import auth


def create_complaint(client, token, plate="TS09AB1234", ctype="Stolen vehicle", desc="Test desc"):
    return client.post(
        "/api/v1/complaints",
        headers=auth(token),
        data={"plate": plate, "complaint_type": ctype, "description": desc},
    )


def test_create_complaint(client, demo_tokens):
    r = create_complaint(client, demo_tokens["citizen"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plate"] == "TS09AB1234"
    assert body["status"] == "PENDING"


def test_create_complaint_normalizes_plate(client, demo_tokens):
    r = create_complaint(client, demo_tokens["citizen"], plate="ts-09 ab 1234")
    assert r.status_code == 200
    assert r.json()["plate"] == "TS09AB1234"


def test_create_complaint_invalid_plate(client, demo_tokens):
    r = create_complaint(client, demo_tokens["citizen"], plate="AB12")
    assert r.status_code == 422


def test_create_complaint_requires_citizen(client, demo_tokens):
    r = create_complaint(client, demo_tokens["cop"])
    assert r.status_code == 403


def test_my_complaints(client, demo_tokens):
    create_complaint(client, demo_tokens["citizen"])
    r = client.get("/api/v1/complaints/mine", headers=auth(demo_tokens["citizen"]))
    assert r.status_code == 200
    assert len(r.json()) >= 1


def test_list_complaints_cop_only(client, demo_tokens):
    create_complaint(client, demo_tokens["citizen"])
    denied = client.get("/api/v1/complaints", headers=auth(demo_tokens["citizen"]))
    assert denied.status_code == 403
    ok = client.get("/api/v1/complaints", headers=auth(demo_tokens["cop"]))
    assert ok.status_code == 200
    assert len(ok.json()) >= 1


def test_verify_creates_hotlist(client, demo_tokens):
    c = create_complaint(client, demo_tokens["citizen"]).json()
    r = client.post(
        f"/api/v1/complaints/{c['id']}/verify",
        headers=auth(demo_tokens["cop"]),
        json={"fir_reference": "FIR/2026/0001"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["complaint"]["status"] == "HOTLISTED"
    assert body["hotlist_id"]


def test_verify_not_citizen(client, demo_tokens):
    c = create_complaint(client, demo_tokens["citizen"]).json()
    r = client.post(f"/api/v1/complaints/{c['id']}/verify", headers=auth(demo_tokens["volunteer"]))
    assert r.status_code == 403


def test_verify_twice_conflict(client, demo_tokens):
    c = create_complaint(client, demo_tokens["citizen"]).json()
    t = demo_tokens["cop"]
    assert client.post(f"/api/v1/complaints/{c['id']}/verify", headers=auth(t)).status_code == 200
    r = client.post(f"/api/v1/complaints/{c['id']}/verify", headers=auth(t))
    assert r.status_code == 409


def test_reject_complaint(client, demo_tokens):
    c = create_complaint(client, demo_tokens["citizen"]).json()
    r = client.post(f"/api/v1/complaints/{c['id']}/reject", headers=auth(demo_tokens["cop"]))
    assert r.status_code == 200
    assert r.json()["status"] == "REJECTED"


def test_proof_file_saved(client, demo_tokens):
    r = client.post(
        "/api/v1/complaints",
        headers=auth(demo_tokens["citizen"]),
        data={"plate": "KA01AB1234", "complaint_type": "Stolen", "description": "with proof"},
        files={"proof": ("proof.jpg", b"\xff\xd8\xff\xe0fakejpeg", "image/jpeg")},
    )
    assert r.status_code == 200, r.text


def test_proof_reject_video(client, demo_tokens):
    r = client.post(
        "/api/v1/complaints",
        headers=auth(demo_tokens["citizen"]),
        data={"plate": "KA01AB1234", "complaint_type": "Stolen", "description": "video proof"},
        files={"proof": ("clip.mp4", b"\x00\x00\x00\x18ftypmp42", "video/mp4")},
    )
    assert r.status_code == 415