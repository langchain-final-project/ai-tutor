# 팀 공통 백엔드 이미지
#API와 기본 검증을 같은 Python 환경에서 실행

FROM python:3.12-slim

WORKDIR /app/backend

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 스크립트가 저장소 루트 기준으로 data/와 backend/를 찾으므로,
# 컨테이너에도 로컬 저장소와 같은 구조를 유지한다.
COPY backend /app/backend
COPY data /app/data
RUN python -m pip install --no-cache-dir ".[test]"

# 컨테이너 밖의 로컬 브라우저에서 접속할 수 있도록 전체 인터페이스에 바인딩합니다.
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
