"""Admin-created police accounts.

Police accounts have no self-service path - ``/auth/register`` refuses the COP
role - so this router is the only way one exists. The tests that matter are the
ones pinning who may call it.

Note on the database: the schema is created once per session, so rows created by
one test survive into the next. Tests therefore create their own account with its
own email and assert against the value that was actually created, rather than
sharing a fixed address.

These tests also run alphabetically after the earlier sample in some orders, so
nothing here assumes an empty users table.
"""

from tests.conftest import auth

CREATE = "/api/v1/users/police"

_counter = {"n": 0}


def _email() -> str:
    _counter["n"] += 1
    return f"officer{_counter['n']}@police.gov.in"


def _payload(**overrides) -> dict:
    body = {
        "name": "Inspector New",
        "email": _email(),
        "phone": "9000000001",
        "password": "Station@123",
    }
    body.update(overrides)
    return body


def _create(client, token, **overrides) -> dict:
    """Create an account and return the response body, asserting it worked."""
    r = client.post(CREATE, json=_payload(**overrides), headers=auth(token))
    assert r.status_code == 201, r.text
    return r.json()


def test_admin_creates_a_police_account(client, demo_tokens):
    created = _create(client, demo_tokens["admin"])
    assert created["role"] == "COP"
    assert created["email"].endswith("@police.gov.in")
    assert created["active"] is True
    # The password is never echoed back.
    assert "password" not in created


def test_created_officer_can_sign_in_with_the_set_password(client, demo_tokens):
    created = _create(client, demo_tokens["admin"])
    r = client.post("/api/v1/auth/login", json={"email": created["email"], "password": "Station@123"})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["role"] == "COP"


def test_new_officer_can_use_the_console(client, demo_tokens):
    created = _create(client, demo_tokens["admin"])
    r = client.post("/api/v1/auth/login", json={"email": created["email"], "password": "Station@123"})
    token = r.json()["access_token"]
    # A COP route: the account is genuinely a console account, not just a label.
    assert client.get("/api/v1/hotlist", headers=auth(token)).status_code == 200


def test_cop_cannot_create_a_police_account(client, demo_tokens):
    r = client.post(CREATE, json=_payload(), headers=auth(demo_tokens["cop"]))
    assert r.status_code == 403


def test_citizen_cannot_create_a_police_account(client, demo_tokens):
    r = client.post(CREATE, json=_payload(), headers=auth(demo_tokens["citizen"]))
    assert r.status_code == 403


def test_volunteer_cannot_create_a_police_account(client, demo_tokens):
    r = client.post(CREATE, json=_payload(), headers=auth(demo_tokens["volunteer"]))
    assert r.status_code == 403


def test_creation_requires_a_token(client):
    assert client.post(CREATE, json=_payload()).status_code == 401


def test_duplicate_email_is_refused(client, demo_tokens):
    headers = auth(demo_tokens["admin"])
    created = _create(client, demo_tokens["admin"])
    r = client.post(CREATE, json=_payload(email=created["email"], name="Someone Else"), headers=headers)
    assert r.status_code == 400


def test_cannot_create_an_admin_through_the_police_endpoint(client, demo_tokens):
    # The role is fixed server-side, so smuggling one in changes nothing.
    created = _create(client, demo_tokens["admin"], role="ADMIN")
    assert created["role"] == "COP"


def test_short_password_is_refused(client, demo_tokens):
    r = client.post(CREATE, json=_payload(password="short"), headers=auth(demo_tokens["admin"]))
    assert r.status_code == 422


# ----------------------------------------------------------- deactivation ---


def test_admin_deactivates_an_officer_and_login_stops(client, demo_tokens):
    headers = auth(demo_tokens["admin"])
    created = _create(client, demo_tokens["admin"])

    r = client.post(f"/api/v1/users/{created['id']}/active", json={"active": False}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["active"] is False

    # The signing-in check is the whole point of deactivation.
    denied = client.post("/api/v1/auth/login", json={"email": created["email"], "password": "Station@123"})
    assert denied.status_code == 403


def test_deactivated_officer_token_stops_working(client, demo_tokens):
    headers = auth(demo_tokens["admin"])
    created = _create(client, demo_tokens["admin"])
    token = client.post(
        "/api/v1/auth/login", json={"email": created["email"], "password": "Station@123"}
    ).json()["access_token"]
    assert client.get("/api/v1/hotlist", headers=auth(token)).status_code == 200

    client.post(f"/api/v1/users/{created['id']}/active", json={"active": False}, headers=headers)
    # An already-issued token must stop resolving, not merely stop being issued.
    assert client.get("/api/v1/hotlist", headers=auth(token)).status_code == 401


def test_reactivating_restores_access(client, demo_tokens):
    headers = auth(demo_tokens["admin"])
    created = _create(client, demo_tokens["admin"])
    client.post(f"/api/v1/users/{created['id']}/active", json={"active": False}, headers=headers)
    r = client.post(f"/api/v1/users/{created['id']}/active", json={"active": True}, headers=headers)
    assert r.status_code == 200
    assert r.json()["active"] is True
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": created["email"], "password": "Station@123"}
        ).status_code
        == 200
    )


def test_admin_cannot_deactivate_their_own_account(client, demo_tokens):
    # There is no recovery path from inside the app, so this must be refused.
    me = client.get("/api/v1/auth/me", headers=auth(demo_tokens["admin"])).json()
    r = client.post(f"/api/v1/users/{me['id']}/active", json={"active": False}, headers=auth(demo_tokens["admin"]))
    assert r.status_code == 400


def test_cop_cannot_deactivate_anyone(client, demo_tokens):
    me = client.get("/api/v1/auth/me", headers=auth(demo_tokens["citizen"])).json()
    r = client.post(f"/api/v1/users/{me['id']}/active", json={"active": False}, headers=auth(demo_tokens["cop"]))
    assert r.status_code == 403


def test_deactivate_unknown_user_is_404(client, demo_tokens):
    r = client.post(
        "/api/v1/users/00000000-0000-0000-0000-000000000000/active",
        json={"active": False},
        headers=auth(demo_tokens["admin"]),
    )
    assert r.status_code == 404


# --------------------------------------------------------- password reset ---


def test_admin_resets_an_officer_password(client, demo_tokens):
    headers = auth(demo_tokens["admin"])
    created = _create(client, demo_tokens["admin"])

    r = client.post(f"/api/v1/users/{created['id']}/password", json={"password": "NewPass@456"}, headers=headers)
    assert r.status_code == 200, r.text

    assert (
        client.post(
            "/api/v1/auth/login", json={"email": created["email"], "password": "Station@123"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": created["email"], "password": "NewPass@456"}
        ).status_code
        == 200
    )


def test_password_reset_refuses_a_citizen_account(client, demo_tokens):
    # Resetting a citizen's password from the police console would be an account
    # takeover: nobody here can prove who owns that account.
    me = client.get("/api/v1/auth/me", headers=auth(demo_tokens["citizen"])).json()
    r = client.post(
        f"/api/v1/users/{me['id']}/password", json={"password": "NewPass@456"}, headers=auth(demo_tokens["admin"])
    )
    assert r.status_code == 403


def test_cop_cannot_reset_a_password(client, demo_tokens):
    me = client.get("/api/v1/auth/me", headers=auth(demo_tokens["cop"])).json()
    r = client.post(
        f"/api/v1/users/{me['id']}/password", json={"password": "NewPass@456"}, headers=auth(demo_tokens["cop"])
    )
    assert r.status_code == 403


def test_password_reset_is_never_echoed(client, demo_tokens):
    headers = auth(demo_tokens["admin"])
    created = _create(client, demo_tokens["admin"])
    r = client.post(f"/api/v1/users/{created['id']}/password", json={"password": "NewPass@456"}, headers=headers)
    assert "NewPass@456" not in r.text
    assert "password" not in r.json()


# ----------------------------------------------------------------- listing ---


def test_listing_reports_active_state(client, demo_tokens):
    headers = auth(demo_tokens["admin"])
    created = _create(client, demo_tokens["admin"])
    client.post(f"/api/v1/users/{created['id']}/active", json={"active": False}, headers=headers)

    r = client.get("/api/v1/users", headers=auth(demo_tokens["cop"]))
    assert r.status_code == 200
    rows = {u["email"]: u for u in r.json()}
    assert rows[created["email"]]["active"] is False


def test_created_account_is_audited(client, demo_tokens):
    from app.db.session import SessionLocal
    from app.models import AuditLog

    headers = auth(demo_tokens["admin"])
    created = _create(client, demo_tokens["admin"])

    db = SessionLocal()
    try:
        rows = (
            db.query(AuditLog)
            .filter(AuditLog.action == "user.police_created", AuditLog.target_id == created["id"])
            .all()
        )
        assert len(rows) == 1
    finally:
        db.close()
