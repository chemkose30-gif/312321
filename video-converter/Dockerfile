FROM python:3.12-slim

# 시스템 ffmpeg (NVIDIA GPU 인코딩 지원 포함)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# 유튜브 영상 정보 해석에 필요한 JavaScript 런타임 (yt-dlp 권장)
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
COPY static ./static

ENV HOST=0.0.0.0 PORT=8000 WORK_DIR=/data
EXPOSE 8000
CMD ["python", "server.py"]
