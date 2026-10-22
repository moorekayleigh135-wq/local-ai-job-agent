from pathlib import Path

from app.main import dashboard, health


def test_root_serves_dashboard() -> None:
    response = dashboard()
    assert response.media_type == "text/html"
    assert "<h1>Overview</h1>" in Path(response.path).read_text(encoding="utf-8")


def test_health_is_healthy() -> None:
    assert health() == {"status": "healthy"}
