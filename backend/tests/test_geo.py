"""GET /api/v1/geo/heat: access control and query-parameter validation.

The endpoint is a map of where cars are. A citizen must not be able to read it,
and it must not accept a window or a bounding box that would turn a COP's map
into a full-database scan.
"""

from datetime import datetime, timedelta, timezone

from tests.conftest import auth
from tests.geo_helpers import grid, put_cell  # noqa: F401  (grid is autouse)

# --------------------------------------------------------------------- access


def test_heat_requires_a_token(client):
    assert client.get("/api/v1/geo/heat").status_code == 401


def test_heat_rejects_a_citizen(client, demo_tokens):
    r = client.get("/api/v1/geo/heat", headers=auth(demo_tokens["citizen"]))
    assert r.status_code == 403


def test_heat_rejects_a_volunteer(client, demo_tokens):
    r = client.get("/api/v1/geo/heat", headers=auth(demo_tokens["volunteer"]))
    assert r.status_code == 403


def test_heat_allows_cop_and_admin(client, demo_tokens):
    put_cell(17.44, 78.48)
    for role in ("cop", "admin"):
        r = client.get("/api/v1/geo/heat", headers=auth(demo_tokens[role]))
        assert r.status_code == 200, r.text
        assert "cells" in r.json()


def test_a_rejected_role_sees_no_cells_not_just_an_error(client, demo_tokens):
    """A 403 must not be dressed up with a body full of coordinates."""
    put_cell(17.44, 78.48)
    r = client.get("/api/v1/geo/heat", headers=auth(demo_tokens["volunteer"]))
    assert r.status_code == 403
    assert "cells" not in r.text


# ------------------------------------------------------------------- geometry


def test_bbox_filters_to_the_requested_window(client, demo_tokens):
    put_cell(17.40, 78.40, car=100)   # inside
    put_cell(19.07, 72.87, car=999)   # Mumbai, outside
    r = client.get(
        "/api/v1/geo/heat",
        params={"bbox": "17.0,78.0,18.0,79.0"},
        headers=auth(demo_tokens["cop"]),
    )
    assert r.status_code == 200, r.text
    assert [c["lat"] for c in r.json()["cells"]] == [17.4]


def test_bbox_needs_four_numbers(client, demo_tokens):
    r = client.get("/api/v1/geo/heat?bbox=1,2,3", headers=auth(demo_tokens["cop"]))
    assert r.status_code == 422


def test_bbox_rejects_inverted_corners(client, demo_tokens):
    r = client.get(
        "/api/v1/geo/heat?bbox=18.0,79.0,17.0,78.0", headers=auth(demo_tokens["cop"])
    )
    assert r.status_code == 422


def test_bbox_rejects_impossible_latitude(client, demo_tokens):
    r = client.get(
        "/api/v1/geo/heat?bbox=-99,78,18,79", headers=auth(demo_tokens["cop"])
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------- window


def test_default_window_returns_something_without_parameters(client, demo_tokens):
    put_cell(17.44, 78.48, hours_ago=0)
    r = client.get("/api/v1/geo/heat", headers=auth(demo_tokens["cop"]))
    assert r.status_code == 200
    assert r.json()["cells"], "an endpoint with no params should still be useful"


def test_from_after_to_is_rejected(client, demo_tokens):
    now = datetime.now(timezone.utc)
    r = client.get(
        "/api/v1/geo/heat",
        params={"from": now.isoformat(), "to": (now - timedelta(hours=1)).isoformat()},
        headers=auth(demo_tokens["cop"]),
    )
    assert r.status_code == 422


def test_window_wider_than_seven_days_is_rejected(client, demo_tokens):
    now = datetime.now(timezone.utc)
    r = client.get(
        "/api/v1/geo/heat",
        params={"from": (now - timedelta(days=30)).isoformat(), "to": now.isoformat()},
        headers=auth(demo_tokens["cop"]),
    )
    assert r.status_code == 422


def test_cells_outside_the_window_are_excluded(client, demo_tokens):
    put_cell(17.44, 78.48, hours_ago=1, car=500)
    put_cell(17.50, 78.50, hours_ago=48, car=999)
    now = datetime.now(timezone.utc)
    r = client.get(
        "/api/v1/geo/heat",
        params={"from": (now - timedelta(hours=6)).isoformat(), "to": now.isoformat()},
        headers=auth(demo_tokens["cop"]),
    )
    assert r.status_code == 200
    assert [c["lat"] for c in r.json()["cells"]] == [17.44]


def test_an_hourly_grid_survives_a_sub_hour_window(client, demo_tokens):
    """The trap this guards: filtering hour buckets by a raw timestamp drops the
    hour containing the window start, so a 15-minute query returns nothing."""
    put_cell(17.44, 78.48, hours_ago=0, car=400)
    now = datetime.now(timezone.utc)
    r = client.get(
        "/api/v1/geo/heat",
        params={"from": (now - timedelta(minutes=5)).isoformat(), "to": now.isoformat()},
        headers=auth(demo_tokens["cop"]),
    )
    assert r.status_code == 200
    assert [c["lat"] for c in r.json()["cells"]] == [17.44]


def test_unknown_layer_is_rejected(client, demo_tokens):
    r = client.get("/api/v1/geo/heat?layer=nonsense", headers=auth(demo_tokens["cop"]))
    assert r.status_code == 422


def test_unknown_vehicle_class_is_rejected(client, demo_tokens):
    r = client.get(
        "/api/v1/geo/heat?vehicle_class=spaceship", headers=auth(demo_tokens["cop"])
    )
    assert r.status_code == 422


def test_unparseable_timestamp_is_rejected(client, demo_tokens):
    r = client.get("/api/v1/geo/heat?from=yesterday", headers=auth(demo_tokens["cop"]))
    assert r.status_code == 422