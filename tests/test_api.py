from fastapi.testclient import TestClient

from app.main import app


def test_health():
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


def test_api_requires_identity():
    with TestClient(app) as client:
        response = client.get("/api/datasources")
        assert response.status_code == 401


def test_datasources_not_advertised_when_database_is_unconfigured(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with TestClient(app) as client:
        response = client.get("/api/datasources", headers={"X-User-Id": "researcher"})
        assert response.status_code == 200
        assert response.json()["items"] == []


def test_datasources_advertised_when_database_is_configured(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://configured-but-not-contacted/example")
    with TestClient(app) as client:
        response = client.get("/api/datasources", headers={"X-User-Id": "researcher"})
        assert response.status_code == 200
        assert response.json()["items"][0]["id"] == "training_db"

