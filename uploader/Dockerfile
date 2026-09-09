FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

# 업로드 원본과 SQLite는 볼륨에 둬서 재배포해도 유지되게 한다.
RUN mkdir -p /data/uploads
VOLUME ["/data"]

EXPOSE 8100
# 호스팅 서비스가 주입하는 $PORT를 우선 사용한다.
CMD ["sh", "-c", "python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8100}"]
