FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# bot.db (encrypted account tokens + trades) lives under DATA_DIR. MUST be a
# persistent disk, or connected accounts are lost on every redeploy.
ENV DATA_DIR=/var/data \
    PYTHONUNBUFFERED=1
RUN mkdir -p /var/data

EXPOSE 9500

# $PORT is injected by Render/Railway/Fly; fall back to 9500 locally.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-9500}"]
