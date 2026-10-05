"""Items 5 and 7, checked against the running backend over HTTP.

Item 5 asks for traffic density from vehicles nobody reported: a cell, a minute
(or hour) bucket, the class and a count, written in batches, exposed on
`GET /api/v1/geo/heat?layer=traffic|stolen&from&to`, police-only. Item 7 asks
that the frame itself never be stored or logged.

This script does not inspect the database directly - it goes through the same
public API a police dashboard would use, because that is the surface the
requirement is about.
"""

from __future__ import annotations

import json
import os
import sys
import time

import requests

BASE = os.environ.get("RAKSHAK_API", "http://127.0.0.1:8000")
API_V1 = f"{BASE}/api/v1"

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((bool(ok), name, detail))
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}  -- {detail}")
    return bool(ok)


def login(email: str, password: str) -> str:
    r = requests.post(
        f"{API_V1}/auth/login", json={"email": email, "password": password}, timeout=20
    )
    r.raise_for_status()
    return r.json()["access_token"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def main() -> int:
    print("\n=== items 5 and 7: traffic heat + privacy, over the public API ===\n")
    cop = login("cop@example.com", "Police@123")
    citizen = login("citizen@example.com", "Citizen@123")
    volunteer = login("volunteer@example.com", "Volunteer@123")

    now = time.time()
    window = {"from": _iso(now - 3600), "to": _iso(now + 60)}

    print("  -- access control --")
    r = requests.get(f"{API_V1}/geo/heat", params=window, timeout=20)
    check("no token is refused", r.status_code == 401, f"HTTP {r.status_code}")
    r = requests.get(f"{API_V1}/geo/heat", headers=auth(citizen), params=window, timeout=20)
    check("a CITIZEN is refused", r.status_code == 403, f"HTTP {r.status_code} {r.text[:60]}")
    r = requests.get(f"{API_V1}/geo/heat", headers=auth(volunteer), params=window, timeout=20)
    check("a VOLUNTEER is refused", r.status_code == 403, f"HTTP {r.status_code}")
    r = requests.get(f"{API_V1}/geo/heat", headers=auth(cop), params=window, timeout=20)
    check("a COP is served", r.status_code == 200, f"HTTP {r.status_code} {len(r.json().get('cells', []))} cells")

    print("\n  -- layers --")
    for layer in ("traffic", "stolen"):
        r = requests.get(
            f"{API_V1}/geo/heat", headers=auth(cop), params={**window, "layer": layer}, timeout=30
        )
        body = r.json() if r.status_code == 200 else {}
        cells = body.get("cells") or []
        shape_ok = all(
            {"lat", "lng", "w"} <= set(c) for c in cells
        ) and "generated_at" in body
        check(
            f"layer={layer} returns cells with lat/lng/weight and a generated_at",
            r.status_code == 200 and shape_ok and (layer != "traffic" or bool(cells)),
            f"HTTP {r.status_code}, {len(cells)} cells, keys={sorted(cells[0].keys()) if cells else None}",
        )
    r = requests.get(
        f"{API_V1}/geo/heat", headers=auth(cop), params={**window, "layer": "nonsense"}, timeout=20
    )
    check("an unknown layer is refused", r.status_code == 422, f"HTTP {r.status_code}")

    print("\n  -- caps --")
    r = requests.get(
        f"{API_V1}/geo/heat",
        headers=auth(cop),
        params={**window, "layer": "traffic", "from": _iso(now - 8 * 86400), "to": _iso(now + 60)},
        timeout=60,
    )
    check("a range beyond the cap is refused", r.status_code == 422, f"HTTP {r.status_code} {r.text[:70]}")
    cells = r.json().get("cells", []) if r.status_code == 200 else []
    r2 = requests.get(
        f"{API_V1}/geo/heat", headers=auth(cop), params={**window, "layer": "traffic"}, timeout=60
    )
    total = len(r2.json().get("cells", []))
    check(
        "no response exceeds 5,000 cells",
        total <= 5000 and len(cells) <= 5000,
        f"{total} cells in a 1 h window",
    )

    print("\n  -- the 10 s cache --")
    # Timings would be noise here: the query is two cells wide, so a cache hit
    # and a miss differ by less than the request overhead. `generated_at` is
    # stamped when the payload is BUILT, so an identical timestamp across two
    # calls is the cache proving itself, and a new timestamp after the TTL
    # proves the TTL.
    key = {**window, "layer": "traffic"}
    first = requests.get(f"{API_V1}/geo/heat", headers=auth(cop), params=key, timeout=60).json()
    time.sleep(2.5)
    again = requests.get(f"{API_V1}/geo/heat", headers=auth(cop), params=key, timeout=60).json()
    check(
        "a repeat inside 10 s is served from cache",
        first["generated_at"] == again["generated_at"],
        f"generated_at held at {first['generated_at']}",
    )
    time.sleep(11)
    expired = requests.get(f"{API_V1}/geo/heat", headers=auth(cop), params=key, timeout=60).json()
    check(
        "the entry is rebuilt after the 10 s TTL",
        expired["generated_at"] != again["generated_at"],
        f"rebuilt at {expired['generated_at']}",
    )

    print("\n  -- item 7: frames are never stored or served --")
    r = requests.post(
        f"{API_V1}/sightings",
        headers=auth(volunteer),
        files={"image": ("frame.jpg", b"\xff\xd8\xff\xe0 not really a jpeg", "image/jpeg")},
        data={"plate": "MH12JK4567"},
        timeout=20,
    )
    check("a multipart frame upload to /sightings is refused", r.status_code in (403, 415, 422), f"HTTP {r.status_code} {r.text[:70]}")
    r = requests.post(
        f"{API_V1}/sightings",
        headers=auth(volunteer),
        json={"plate": "MH12JK4567", "image": "AAAA", "video": "AAAA"},
        timeout=20,
    )
    check(
        "image/video fields on /sightings are refused",
        r.status_code in (403, 422),
        f"HTTP {r.status_code} {r.text[:70]}",
    )
    r = requests.get(f"{API_V1}/sightings", headers=auth(cop), timeout=20)
    keys = set()
    if r.status_code == 200 and r.json():
        keys = set(r.json()[0].keys())
    check(
        "a sighting row has no image or frame column",
        not (keys & {"image", "images", "frame", "frames", "video", "clip", "thumbnail"}),
        f"sighting keys = {sorted(keys)}",
    )

    print()
    failed = [n for ok, n, _ in RESULTS if not ok]
    print(f"  {len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    for n in failed:
        print(f"    FAILED: {n}")
    return 1 if failed else 0


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


if __name__ == "__main__":
    sys.exit(main())