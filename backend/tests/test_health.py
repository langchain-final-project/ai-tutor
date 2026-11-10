# 개발용 DB와 분리한 임시 SQLite로 API 상태 확인을 테스트

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app



# 데이터베이스 연결 상태를 보고하는 헬스 체크 테스트
def test_health_reports_database_connection(tmp_path: Path) -> None:
    with TestClient(create_app(f"sqlite:///{tmp_path / 'health.db'}")) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "sqlite"}

# 데이터베이스 연결 불가 시 헬스 체크 테스트
def test_health_reports_unavailable_database(tmp_path: Path) -> None:
    with TestClient(create_app(f"sqlite:///{tmp_path / 'missing' / 'health.db'}")) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}
