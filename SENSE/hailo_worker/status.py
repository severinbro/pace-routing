"""Read-only live frame diagnostics for a SENSE operator."""

import argparse
import json
import time

from receiver.live_frame_state import HEARTBEAT, PROCESSING, READY, read_json

from .worker import Worker


def frame_status(worker, key):
    frame = worker.store.frame(key)
    for name in ("terminal", "parked", "deferred"):
        record = read_json(frame / f"{name}.json")
        if record is not None:
            return {"key": key, "status": name, **record}
    if worker.redis.lpos(PROCESSING, key) is not None:
        state = "processing"
    elif worker.redis.lpos(READY, key) is not None:
        state = "queued"
    elif (frame / "accepted.json").exists():
        state = "accepted"
    elif (frame / "stage.json").exists():
        state = "staged"
    else:
        state = "missing"
    return {"key": key, "status": state}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--frame-index", type=int)
    args = parser.parse_args()
    worker = Worker()
    worker.store.frame(f"{args.session_id}/0")
    if args.frame_index is None:
        keys = [key for key in worker.store.staged() if key.split("/")[0] == args.session_id]
    else:
        keys = [f"{args.session_id}/{args.frame_index}"]
    count, size = worker.store.pending()
    print(json.dumps({
        "worker_ready": bool(worker.redis.get(HEARTBEAT)),
        "heartbeat_ttl_seconds": worker.redis.ttl(HEARTBEAT),
        "pending_count": count, "pending_bytes": size,
        "checked_utc_ns": time.time_ns(),
        "frames": [frame_status(worker, key) for key in keys],
    }, indent=2))


if __name__ == "__main__":
    main()
