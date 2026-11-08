from fastapi.testclient import TestClient

from app.main import app


def test_health():
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["content-type"] == "application/json"


def test_health_does_not_require_database_or_llm(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://invalid:1/unavailable")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_health_openapi_operation():
    operation = app.openapi()["paths"]["/health"]["get"]
    assert operation["operationId"] == "health"
    assert "200" in operation["responses"]
