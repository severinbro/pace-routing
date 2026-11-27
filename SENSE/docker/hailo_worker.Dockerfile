FROM python:3.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN pip install --no-cache-dir redis==6.4.0 Pillow==13.0.0
COPY receiver/live_frame_state.py /app/receiver/live_frame_state.py
COPY hailo_worker/ /app/hailo_worker/
