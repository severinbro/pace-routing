FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app:/app/receiver
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir redis==6.4.0 Pillow==13.0.0
COPY receiver/live_frame_state.py /app/receiver/live_frame_state.py
COPY hailo_worker/ /app/hailo_worker/
