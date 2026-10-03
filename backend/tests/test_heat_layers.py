"""What the heat endpoint's numbers actually mean.

A heatmap is only as honest as its weighting. These tests pin down three
properties that would otherwise be easy to get subtly wrong and hard to notice:

1. Density is vehicles *per frame*, not a raw total - otherwise the map rewards
   whoever watched a road longest rather than whoever watched the busiest road.
2. The stolen layer decays with age, so "last hour" is not "last week".
3. Neither the table nor the response can identify a vehicle.
"""

from datetime import datetime, timedelta, timezone

from tests.conftest import auth
from tests.geo_helpers import grid, put_cell, put_sightings  # noqa: F401

# --------------------------------------------------------------------- weights


def test_density_is_per_frame_not_a_raw_total(client, demo_tokens):
    # Same vehicles, 10x the observation time: density must be identical.
    put_cell(17.40, 78.40, frames=100, car=300, two_wheeler=0, bus=0, truck=0)
    put_cell(17.60, 78.60, frames=1000, car=3000, two_wheeler=0, bus=0, truck=0)
    r = client.get("/api/v1/geo/heat", headers=auth(demo_tokens["cop"]))
    cells = {c["lat"]: c["w"] for c in r.json()["cells"]}
    assert abs(cells[17.40] - 3.0) < 1e-6
    assert abs(cells[17.60] - 3.0) < 1e-6


def test_a_cell_needs_enough_frames_before_it_is_shown(client, demo_tokens):
    # One frame with two cars would otherwise paint as bright as a junction.
    put_cell(17.44, 78.48, frames=1, car=2)
    r = client.get("/api/v1/geo/heat", headers=auth(demo_tokens["cop"]))
    assert r.json()["cells"] == []


def test_an_empty_but_watched_cell_is_reported_as_zero(client, demo_tokens):
    """Frames that saw nothing are data: an empty road is quiet, not missing."""
    put_cell(17.44, 78.48, frames=250, car=0, two_wheeler=0, bus=0, truck=0)
    r = client.get("/api/v1/geo/heat", headers=auth(demo_tokens["cop"]))
    cells = r.json()["cells"]
    assert [c["lat"] for c in cells] == [17.44]
    assert cells[0]["w"] == 0.0
    assert cells[0]["n"] == 250


def test_vehicle_class_filter_selects_one_column(client, demo_tokens):
    put_cell(17.40, 78.40, frames=100, car=500, two_wheeler=50, bus=10, truck=5)
    r = client.get(
        "/api/v1/geo/heat?vehicle_class=car", headers=auth(demo_tokens["cop"])
    )
    assert r.status_code == 200
    cells = r.json()["cells"]
    assert cells and abs(cells[0]["w"] - 5.0) < 1e-6


def test_cells_are_ranked_by_weight_not_insertion_order(client, demo_tokens):
    put_cell(17.40, 78.40, frames=100, car=100, two_wheeler=0, bus=0, truck=0)
    put_cell(17.41, 78.41, frames=100, car=900, two_wheeler=0, bus=0, truck=0)
    r = client.get("/api/v1/geo/heat", headers=auth(demo_tokens["cop"]))
    assert [c["w"] for c in r.json()["cells"]] == [9.0, 1.0]


# ----------------------------------------------------------------- stolen layer


def test_stolen_layer_decays_with_age(client, demo_tokens):
    """A sighting from ten minutes ago must outweigh one from yesterday."""
    put_sightings(coords=[(17.44, 78.48), (17.44, 78.48)], ages_minutes=[10, 40 * 60])

    # The stolen layer keeps exact timestamps (unlike the hourly traffic grid),
    # so the window has to be widened explicitly to include the old sighting.
    now = datetime.now(timezone.utc)
    r = client.get(
        "/api/v1/geo/heat",
        params={
            "layer": "stolen",
            "from": (now - timedelta(hours=48)).isoformat(),
            "to": now.isoformat(),
        },
        headers=auth(demo_tokens["cop"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cells"], "stolen layer returned no cells"
    # Both sightings share one cell; the weight is their decayed sum, dominated
    # by the recent one (exp(-10m/6h) ~ 0.97, exp(-40h/6h) ~ 0.0013).
    assert 0.95 < body["cells"][0]["w"] < 1.01
    assert body["cells"][0]["n"] == 2
    assert body["tau_hours"] == 6.0


def test_stolen_layer_respects_the_window(client, demo_tokens):
    """A two-hour-old sighting is not "current" for the default 15-minute window."""
    put_sightings(coords=[(17.44, 78.48)], ages_minutes=[120])
    r = client.get(
        "/api/v1/geo/heat?layer=stolen", headers=auth(demo_tokens["cop"])
    )
    assert r.status_code == 200
    assert r.json()["cells"] == []


def test_stolen_layer_ignores_vehicle_class(client, demo_tokens):
    """There is no class split for a plate; the parameter must not change the answer."""
    put_sightings(coords=[(17.44, 78.48)], ages_minutes=[5])
    now = datetime.now(timezone.utc)
    params = {"layer": "stolen", "from": (now - timedelta(hours=1)).isoformat(),
              "to": now.isoformat()}
    plain = client.get("/api/v1/geo/heat", params=params, headers=auth(demo_tokens["cop"]))
    with_class = client.get(
        "/api/v1/geo/heat",
        params={**params, "vehicle_class": "car"},
        headers=auth(demo_tokens["cop"]),
    )
    assert plain.status_code == with_class.status_code == 200
    a, b = plain.json()["cells"], with_class.json()["cells"]
    assert [c["lat"] for c in a] == [c["lat"] for c in b]
    assert [c["n"] for c in a] == [c["n"] for c in b]
    # Not exactly equal: the decay is computed against now() in SQL, so two
    # requests milliseconds apart differ in the last few bits.
    assert abs(a[0]["w"] - b[0]["w"]) < 1e-3


# --------------------------------------------------------------------- privacy


def test_grid_holds_no_identifying_column():
    """A density grid that can be joined back to a car is not a density grid."""
    from app.models import TrafficCell

    columns = {c.name for c in TrafficCell.__table__.columns}
    for forbidden in ("plate", "device_id", "user_id", "image", "jpeg", "frame",
                      "hotlist_id", "sighting_id", "latitude", "longitude"):
        assert forbidden not in columns, f"traffic_cells.{forbidden} must not exist"


def test_response_carries_no_plate_or_device(client, demo_tokens):
    put_cell(17.44, 78.48)
    r = client.get("/api/v1/geo/heat", headers=auth(demo_tokens["cop"]))
    text = r.text.upper()
    for leak in ("TS09AB1234", "PLATE", "DEVICE_ID", "JWT", "BEARER"):
        assert leak not in text, f"heat response leaked {leak}"


def test_a_sighting_is_aggregated_not_attributed(client, demo_tokens):
    """Two sightings of the same plate at the same spot become one cell."""
    put_sightings(coords=[(17.44, 78.48), (17.44, 78.48)], ages_minutes=[5, 6])
    now = datetime.now(timezone.utc)
    r = client.get(
        "/api/v1/geo/heat",
        params={"layer": "stolen", "from": (now - timedelta(hours=1)).isoformat(),
                "to": now.isoformat()},
        headers=auth(demo_tokens["cop"]),
    )
    cells = r.json()["cells"]
    assert len(cells) == 1
    assert cells[0]["n"] == 2
    assert set(cells[0]) == {"lat", "lng", "w", "n"}