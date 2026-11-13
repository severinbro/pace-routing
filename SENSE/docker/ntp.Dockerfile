FROM debian:bookworm-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends chrony \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 123/udp
ENTRYPOINT ["chronyd", "-d", "-x", "-f", "/etc/chrony/chrony.conf"]
