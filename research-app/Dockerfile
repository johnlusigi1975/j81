FROM python:3.12-slim

# ffmpeg: needed by yt-dlp for the TikTok/Instagram audio path (and Whisper
# if enabled). ca-certificates: outbound HTTPS scraping.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# DATA_DIR is where the SQLite trade DB, research_config.json and the
# trade-type library are written. On Render/Railway/Fly mount a persistent
# disk and set DATA_DIR to its mount path so data survives redeploys.
ENV DATA_DIR=/var/data \
    PYTHONUNBUFFERED=1
RUN mkdir -p /var/data

EXPOSE 8001

# $PORT is injected by Render/Railway/Fly; fall back to 8001 locally.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8001}"]
