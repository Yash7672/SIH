import json

import requests

BASE = "http://127.0.0.1:8000/api/v1"


def login(email: str, password: str):
    resp = requests.post(f"{BASE}/auth/login", json={"email": email, "password": password}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def main():
    citizen = login("citizen@example.com", "Citizen@123")
    cop = login("cop@example.com", "Police@123")
    volunteer = login("volunteer@example.com", "Volunteer@123")

    complaint = requests.post(
        f"{BASE}/complaints",
        headers={"Authorization": f"Bearer {citizen['access_token']}"},
        data={"plate": "TS09AB1234", "complaint_type": "STOLEN", "description": "Demo complaint"},
        timeout=20,
    )
    complaint.raise_for_status()
    complaint = complaint.json()

    verified = requests.post(
        f"{BASE}/complaints/{complaint['id']}/verify",
        headers={"Authorization": f"Bearer {cop['access_token']}"},
        json={"fir_reference": "FIR-RAKSHAK-001"},
        timeout=20,
    )
    verified.raise_for_status()
    verified = verified.json()

    device = requests.post(
        f"{BASE}/devices/register",
        headers={"Authorization": f"Bearer {volunteer['access_token']}"},
        json={"device_type": "android", "device_name": "demo-phone"},
        timeout=20,
    )
    device.raise_for_status()
    device = device.json()

    sighting = requests.post(
        f"{BASE}/sightings",
        headers={"Authorization": f"Bearer {volunteer['access_token']}"},
        json={
            "plate": "TS09AB1234",
            "latitude": 12.9716,
            "longitude": 77.5946,
            "timestamp": "2026-09-23T03:45:00Z",
            "confidence": 0.99,
            "device_id": device["id"],
        },
        timeout=20,
    )
    print("sighting status:", sighting.status_code)
    print("sighting body:", sighting.text)
    sighting.raise_for_status()
    sighting = sighting.json()

    hotlist = requests.get(f"{BASE}/hotlist", headers={"Authorization": f"Bearer {cop['access_token']}"}, timeout=20)
    hotlist.raise_for_status()
    hotlist = hotlist.json()

    alerts = requests.get(f"{BASE}/alerts", headers={"Authorization": f"Bearer {cop['access_token']}"}, timeout=20)
    alerts.raise_for_status()
    alerts = alerts.json()

    hotlist_plate = next(item["plate"] for item in hotlist if item["id"] == verified["hotlist_id"])
    result = {
        "complaint_id": complaint["id"],
        "hotlist_id": verified["hotlist_id"],
        "device_id": device["id"],
        "sighting_id": sighting["id"],
        "plate": hotlist_plate,
        "status": "MATCHED",
        "hotlist_count": len(hotlist),
        "alert_count": len(alerts),
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
