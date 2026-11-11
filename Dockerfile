FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY config.py media_backends.py encoder_daemon.py server.py ./
COPY static ./static

RUN mkdir -p /data/wall_data/clips

ENV WALL_DIR=/data/wall_data \
    HOST=0.0.0.0 \
    PORT=18500 \
    PYTHONUNBUFFERED=1

EXPOSE 18500

CMD ["python", "server.py"]
