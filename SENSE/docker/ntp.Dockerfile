FROM debian:bookworm-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends chrony \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 123/udp
# Docker restarts retain the container filesystem. Remove a stale PID file left
# after an unclean stop before starting chronyd as PID 1 again.
ENTRYPOINT ["/bin/sh", "-c", "rm -f /run/chrony/chronyd.pid && exec chronyd -d -x -f /etc/chrony/chrony.conf"]
