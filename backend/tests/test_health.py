from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_root_returns_service_name() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["name"] == "Local AI Job Agent API"


def test_health_is_healthy() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}
