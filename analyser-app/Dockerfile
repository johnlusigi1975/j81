FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite (strategies/insights/candles/comms/productivity/study) lives under
# DATA_DIR — mount a persistent disk here so it survives redeploys.
ENV DATA_DIR=/var/data \
    PYTHONUNBUFFERED=1
RUN mkdir -p /var/data

EXPOSE 9000

# $PORT is injected by Render/Railway/Fly; fall back to 9000 locally.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-9000}"]
