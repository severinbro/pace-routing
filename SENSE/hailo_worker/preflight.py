"""Check the deployment wiring without publishing readiness or committing a result."""

import argparse
import json
import os
from pathlib import Path
from tempfile import TemporaryFile

from receiver.live_frame_state import HEARTBEAT

from .worker import Worker, send_result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ready", action="store_true", help="require a live Hailo heartbeat")
    ready = parser.parse_args().ready
    worker = Worker()
    if ready:
        heartbeat = worker.redis.get(HEARTBEAT)
        if not heartbeat or json.loads(heartbeat).get("adapter") != "hailo":
            raise SystemExit("Hailo worker heartbeat unavailable")
        return
    worker.redis.ping()
    worker.store.root.mkdir(parents=True, exist_ok=True)
    with TemporaryFile(dir=worker.store.root) as probe:
        probe.write(b"segmentation")
        probe.flush()
        os.fsync(probe.fileno())
    if not Path("/models").is_dir():
        raise SystemExit("Read-only model mount is missing")
    status, reply = send_result(worker.url, worker.token_file, b"{}")
    if status != 400 or reply != {"error": "invalid_result"}:
        raise SystemExit(f"Authenticated result endpoint probe failed (HTTP {status})")
    print("SENSE Redis, writable frame storage, model mount and result endpoint are reachable")


if __name__ == "__main__":
    main()
