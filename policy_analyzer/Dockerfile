# 서버 배포용 이미지 (OCR 한글팩 포함)
FROM python:3.12-slim

# 로컬 OCR 백엔드에 필요한 시스템 패키지
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-kor \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render 등은 PORT 환경변수로 포트를 알려줌 (없으면 8000)
ENV PORT=8000
CMD ["sh", "-c", "python -m uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
