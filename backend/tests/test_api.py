"""API and WebSocket integration tests against the real FastAPI app."""

from __future__ import annotations


def test_root_serves_frontend(app_client):
    response = app_client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "LankaPulse" in response.text
    assert "/static/app.js" in response.text


def test_health_reports_database_and_pipeline(app_client):
    response = app_client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["database"]["ok"] is True
    assert payload["pipeline"]["running"] is False
    assert payload["llm"]["provider"] == "heuristic"


def test_sources_lists_free_providers_and_licenses(app_client):
    response = app_client.get("/api/sources")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["feeds"]) >= 6
    assert any(feed["provider"] == "google_news" for feed in payload["feeds"])
    assert "adaderana.lk" in payload["trusted_domains"]
    assert payload["licenses"]["OpenStreetMap"] == "ODbL 1.0"
    assert payload["licenses"]["geoBoundaries"] == "ODbL 1.0"


def test_events_and_filters(app_client, seeded_event_id):
    response = app_client.get("/api/events", params={"hours": 0})
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] >= 1
    event = next(item for item in payload["events"] if item["id"] == seeded_event_id)
    assert event["district"] == "Ratnapura"
    assert event["category"] == "WEATHER"
    assert len(event["media_reports"]) == 2

    filtered = app_client.get(
        "/api/events", params={"hours": 0, "category": "ACCIDENT"}
    ).json()
    assert all(item["category"] == "ACCIDENT" for item in filtered["events"])

    wrong_district = app_client.get(
        "/api/events", params={"hours": 0, "district": "Jaffna"}
    ).json()
    assert wrong_district["count"] == 0


def test_event_detail_and_404(app_client, seeded_event_id):
    response = app_client.get(f"/api/events/{seeded_event_id}")
    assert response.status_code == 200
    assert response.json()["id"] == seeded_event_id

    missing = app_client.get("/api/events/999999")
    assert missing.status_code == 404


def test_markers_have_coordinates(app_client, seeded_event_id):
    response = app_client.get("/api/markers", params={"hours": 0})
    assert response.status_code == 200
    markers = response.json()["markers"]
    marker = next(item for item in markers if item["id"] == seeded_event_id)
    assert marker["latitude"] == 6.6828
    assert marker["longitude"] == 80.3992
    assert marker["minutes_ago"] >= 0


def test_stats_aggregate_seeded_event(app_client, seeded_event_id):
    response = app_client.get("/api/stats")
    assert response.status_code == 200
    payload = response.json()
    assert payload["events_24h"] >= 1
    assert payload["districts_active"] >= 1
    assert payload["category_breakdown"].get("WEATHER", 0) >= 1


def test_websocket_sends_initial_state(app_client, seeded_event_id):
    with app_client.websocket_connect("/ws/live") as websocket:
        message = websocket.receive_json()
        assert message["type"] == "initial_state"
        ids = {event["id"] for event in message["events"]}
        assert seeded_event_id in ids


def test_district_boundaries_are_served(app_client):
    response = app_client.get("/static/sri-lanka-districts.geojson")
    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "FeatureCollection"
    assert len(payload["features"]) == 25
    names = {feature["properties"]["name"] for feature in payload["features"]}
    assert {"Colombo", "Gampaha", "Jaffna", "Ratnapura"} <= names


def test_favicon_is_quiet(app_client):
    assert app_client.get("/favicon.ico").status_code == 204
