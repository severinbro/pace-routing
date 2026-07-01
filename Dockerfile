# Use a slim Python image for ARM64 (RPi5 native)
FROM python:3.11-slim-bookworm

# Prevent Python from writing .pyc files
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

WORKDIR /code

# Install system dependencies
RUN apt-get update && apt-get install -y \
    binutils \
    libproj-dev \
    gdal-bin \
    python3-gdal \
    libgdal-dev \
    i2c-tools \
    python3-smbus \
    libportaudio2 \
    libasound2-dev \
    portaudio19-dev \
    build-essential \
    python3-dev \
    libopenblas-dev \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements FIRST so Docker detects changes here
COPY requirements.txt /code/

# Install Python dependencies with verbose output to catch errors
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy the rest of the project
COPY . /code/