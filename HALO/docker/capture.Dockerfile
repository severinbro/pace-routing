FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg && \
    install -d -m 0755 /etc/apt/keyrings && \
    curl -fsSL https://archive.raspberrypi.com/debian/raspberrypi.gpg.key \
      | gpg --dearmor -o /etc/apt/keyrings/raspberrypi.gpg && \
    echo 'deb [arch=arm64 signed-by=/etc/apt/keyrings/raspberrypi.gpg] https://archive.raspberrypi.com/debian/ bookworm main' \
      > /etc/apt/sources.list.d/raspberrypi.list && \
    apt-get update && apt-get install -y --no-install-recommends \
      rpicam-apps-core rpicam-apps-encoder libcamera-ipa \
      ffmpeg python3 python3-venv && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential python3-dev && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    python3 -m venv /opt/pace-venv && \
    /opt/pace-venv/bin/pip install --no-cache-dir \
      adafruit-circuitpython-bno08x==1.3.3 adafruit-extended-bus==1.0.2 \
      redis==5.3.1 'gpiod>=2.2.2,<3' adafruit-circuitpython-bh1750

ENV PATH="/opt/pace-venv/bin:${PATH}" \
    PACE_CONFIG=/etc/halo-camera/capture.env

WORKDIR /app
COPY capture/bin/ /app/capture/bin/
ENTRYPOINT ["/app/capture/bin/run_pipeline.sh"]
CMD ["once"]
