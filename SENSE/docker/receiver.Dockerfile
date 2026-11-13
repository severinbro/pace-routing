FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# The receiver server uses strictly Python standard library modules (http.server, hashlib, tarfile, json, etc.)
# No external pip dependencies required, ensuring instant builds and ARM64 stability on Raspberry Pi OS.

COPY receiver/ /app/receiver/

EXPOSE 8000

ENTRYPOINT ["python3", "/app/receiver/server.py"]
