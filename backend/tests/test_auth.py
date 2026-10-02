from tests.conftest import auth


def test_login_success(client, demo_tokens):
    assert demo_tokens["citizen"]
    assert demo_tokens["cop"]
    assert demo_tokens["volunteer"]
    assert demo_tokens["admin"]


def test_login_wrong_password(client):
    r = client.post("/api/v1/auth/login", json={"email": "citizen@example.com", "password": "wrong"})
    assert r.status_code == 401


def test_login_unknown_user(client):
    r = client.post("/api/v1/auth/login", json={"email": "ghost@example.com", "password": "x"})
    assert r.status_code == 401


def test_register_citizen(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"name": "Test New", "email": "new@example.com", "password": "Strong@123", "role": "CITIZEN"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["user"]["role"] == "CITIZEN"


def test_register_forbidden_role(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"name": "Hacker", "email": "hacker@example.com", "password": "Strong@123", "role": "COP"},
    )
    assert r.status_code == 403


def test_register_duplicate_email(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"name": "Dup", "email": "citizen@example.com", "password": "Strong@123", "role": "CITIZEN"},
    )
    assert r.status_code == 400


def test_me(client, demo_tokens):
    r = client.get("/api/v1/auth/me", headers=auth(demo_tokens["citizen"]))
    assert r.status_code == 200
    assert r.json()["email"] == "citizen@example.com"


def test_me_denied_without_token(client):
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 401