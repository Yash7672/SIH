def test_health_endpoint_available(client):
    r = client.get("/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] in {"ok", "degraded"}
