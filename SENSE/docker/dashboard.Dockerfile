FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app
WORKDIR /app
COPY dashboard/ /app/dashboard/
COPY receiver/verify.py /app/verify.py
EXPOSE 8080
CMD ["python3", "/app/dashboard/server.py"]
