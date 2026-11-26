FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN pip install --no-cache-dir 'redis>=5,<7'

COPY receiver/ /app/receiver/

EXPOSE 8000

ENTRYPOINT ["python3", "/app/receiver/server.py"]
