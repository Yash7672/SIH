import os

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg2://postgres:postgres@localhost:5432/rakshak_test"
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["REDIS_URL"] = TEST_REDIS_URL
os.environ["DEMO_MODE"] = "true"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db.session import Base, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services.cache import cache_service  # noqa: E402
from app.services.seed import seed_demo_users  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _db_schema():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    seed_demo_users()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _flush_caches():
    try:
        cache_service._client.flushdb()
    except Exception:
        pass
    yield
    try:
        cache_service._client.flushdb()
    except Exception:
        pass


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def demo_tokens(client):
    def login(email: str, password: str) -> str:
        r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    return {
        "citizen": login("citizen@example.com", "Citizen@123"),
        "cop": login("cop@example.com", "Police@123"),
        "volunteer": login("volunteer@example.com", "Volunteer@123"),
        "admin": login("admin@example.com", "Admin@123"),
    }


def auth(token: str):
    return {"Authorization": f"Bearer {token}"}