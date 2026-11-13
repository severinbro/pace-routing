#!/usr/bin/env python3
"""Durable, at-least-once frame extraction queue for local capture sessions."""

import argparse
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from redis import Redis
from redis.exceptions import RedisError


PENDING = "halo:frames:pending"
PROCESSING = "halo:frames:processing"
QUEUED = "halo:frames:queued"
SESSION_NAME = re.compile(r"[A-Za-z0-9._-]+\Z")
RESCAN_SECONDS = 10
RETRY_SECONDS = 10

# Keep the membership set and pending list in sync across a producer crash.
ENQUEUE_SCRIPT = """
if redis.call('SADD', KEYS[1], ARGV[1]) == 1 then
  redis.call('LPUSH', KEYS[2], ARGV[1])
  return 1
end
return 0
"""

# A completed job leaves both the processing list and the membership set.
ACK_SCRIPT = """
local removed = redis.call('LREM', KEYS[1], 1, ARGV[1])
redis.call('SREM', KEYS[2], ARGV[1])
return removed
"""


def redis_client() -> Redis:
    return Redis(
        host=os.environ.get("REDIS_HOST", "redis"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=8,
        health_check_interval=30,
    )


def sessions_root() -> Path:
    return (Path(os.environ["DATA_ROOT"]) / "sessions").resolve()


def enqueue(client: Redis, session_id: str) -> bool:
    return bool(client.eval(ENQUEUE_SCRIPT, 2, QUEUED, PENDING, session_id))


def acknowledge(client: Redis, session_id: str) -> None:
    client.eval(ACK_SCRIPT, 2, PROCESSING, QUEUED, session_id)


def enqueue_command(session_arg: str) -> int:
    root = sessions_root()
    session = Path(session_arg).resolve(strict=True)
    if session.parent != root or not SESSION_NAME.fullmatch(session.name):
        raise ValueError(f"Session must be a direct child of {root}: {session}")
    if not (session / ".capture_complete").is_file():
        raise ValueError(f"Capture is not complete: {session}")

    client = redis_client()
    try:
        if enqueue(client, session.name):
            logging.info("Queued session %s", session.name)
        else:
            logging.info("Session already queued: %s", session.name)
    finally:
        client.close()
    return 0


def recover_inflight(client: Redis) -> None:
    recovered = 0
    while client.lmove(PROCESSING, PENDING, src="RIGHT", dest="LEFT") is not None:
        recovered += 1
    if recovered:
        logging.warning("Returned %d interrupted extraction job(s) to the queue", recovered)


def reconcile_disk(client: Redis, root: Path) -> None:
    for session in sorted(root.iterdir()):
        if not session.is_dir() or not SESSION_NAME.fullmatch(session.name):
            continue
        if (session / ".capture_complete").is_file() and not (
            (session / ".extraction_complete").is_file()
            or (session / ".extraction_failed").is_file()
        ):
            if enqueue(client, session.name):
                logging.info("Recovered unqueued session from disk: %s", session.name)


def process_job(client: Redis, root: Path, session_id: str) -> None:
    if not SESSION_NAME.fullmatch(session_id):
        logging.error("Discarding invalid queue entry: %r", session_id)
        acknowledge(client, session_id)
        return

    session = root / session_id
    if not (session / ".capture_complete").is_file():
        logging.error("Discarding queue entry without a completed capture: %s", session)
        acknowledge(client, session_id)
        return

    if (session / ".extraction_complete").is_file() or (
        session / ".extraction_failed"
    ).is_file():
        acknowledge(client, session_id)
        return

    extractor = Path(__file__).with_name("extract_frames.sh")
    logging.info("Extracting frames from %s", session_id)
    try:
        result = subprocess.run([str(extractor), str(session)], check=False)
    except OSError as exc:
        logging.error("Could not start extraction for %s: %s", session_id, exc)
        result = None

    if result is not None and result.returncode == 0 and (
        session / ".extraction_complete"
    ).is_file():
        acknowledge(client, session_id)
        logging.info("Extraction complete: %s", session_id)
        return

    if (session / ".extraction_failed").is_file():
        acknowledge(client, session_id)
        logging.error("Permanent timing failure for %s; diagnostic kept on disk", session_id)
        return

    logging.error("Extraction failed for %s; retrying in %d seconds", session_id, RETRY_SECONDS)
    time.sleep(RETRY_SECONDS)
    client.lmove(PROCESSING, PENDING, src="RIGHT", dest="LEFT")


def worker_command() -> int:
    root = sessions_root()
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.nice(10)
    except OSError as exc:
        logging.warning("Could not lower extractor priority: %s", exc)

    while True:
        client = redis_client()
        try:
            client.ping()
            recover_inflight(client)
            next_scan = 0.0
            while True:
                if time.monotonic() >= next_scan:
                    reconcile_disk(client, root)
                    next_scan = time.monotonic() + RESCAN_SECONDS
                session_id = client.blmove(
                    PENDING, PROCESSING, timeout=5, src="RIGHT", dest="LEFT"
                )
                if session_id is not None:
                    process_job(client, root, session_id)
        except (RedisError, OSError) as exc:
            logging.warning("Queue unavailable; reconnecting in 5 seconds: %s", exc)
            time.sleep(5)
        finally:
            client.close()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [session_queue] %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    enqueue_parser = subcommands.add_parser("enqueue")
    enqueue_parser.add_argument("session_dir")
    subcommands.add_parser("worker")
    args = parser.parse_args()

    try:
        if args.command == "enqueue":
            return enqueue_command(args.session_dir)
        return worker_command()
    except (KeyError, ValueError, FileNotFoundError, RedisError) as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
