# 팀 공통 백엔드 이미지
#API와 기본 검증을 같은 Python 환경에서 실행

FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY . .
RUN python -m pip install --no-cache-dir ".[test]"

# 컨테이너 밖의 로컬 브라우저에서 접속할 수 있도록 전체 인터페이스에 바인딩합니다.
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
