"""Single-consumer SENSE worker with durable result submission."""

from datetime import datetime, timezone
from hashlib import sha256
import http.client
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit

import redis

from receiver.live_frame_state import (
    HEARTBEAT, PROCESSING, READY, STAGE_EXPIRY_NS, FrameStore, file_lock, json_bytes,
    read_json, register, save_bytes, save_json, sync_dir,
)

from .adapters import InvalidFrame, fixture_inference
from .metrics import mask_metrics


LOG = logging.getLogger("hailo_worker")
HEX_DIGEST = re.compile(r"[0-9a-f]{64}")


def redis_client(blocking=False):
    return redis.Redis(
        host=os.environ.get("REDIS_HOST", "redis"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=3 if blocking else 2,
    )


def send_result(url, token_file, body):
    target = urlsplit(url)
    if target.scheme != "http" or not target.hostname:
        raise ValueError("result URL must be internal HTTP")
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("missing result token")
    conn = http.client.HTTPConnection(target.hostname, target.port, timeout=5)
    try:
        conn.request("POST", target.path, body=body, headers={
            "Content-Type": "application/json", "X-SENSE-Segmentation-Token": token,
        })
        response = conn.getresponse()
        raw = response.read(64 * 1024 + 1)
        try:
            return response.status, json.loads(raw)
        except (ValueError, UnicodeError):
            return response.status, None
    finally:
        conn.close()


def valid_response(body, expected_state):
    return (
        isinstance(body, dict)
        and body.get("state") == expected_state
        and all(isinstance(body.get(name), str) and HEX_DIGEST.fullmatch(body[name])
                for name in ("result_sha256", "mask_sha256", "manifest_id"))
    )


class Worker:
    def __init__(self):
        self.store = FrameStore(os.getenv("SEGMENTATION_LIVE_ROOT", "/data/live-v2"))
        self.redis = redis_client()
        self.claims = redis_client(blocking=True)
        self.fixture_root = Path(os.getenv("SEGMENTATION_FIXTURE_INDEX", "/fixtures/index.json")).resolve().parent
        self.url = os.getenv("SEGMENTATION_RESULT_URL", "http://pace-web:8000/internal/segmentation/frames/")
        self.token_file = Path(os.getenv("SEGMENTATION_RESULT_TOKEN_FILE", "/run/secrets/segmentation_internal"))
        self.reconcile_interval = float(os.getenv("SEGMENTATION_RECONCILE_INTERVAL_S", "30"))
        self.defer_seconds = float(os.getenv("SEGMENTATION_DEFER_S", "30"))
        self.boot = datetime.now(timezone.utc).isoformat()
        self.stopping = threading.Event()

    def test_fault(self, name):
        directory = os.getenv("SEGMENTATION_FIXTURE_FAULTS")
        if directory and os.getenv("SEGMENTATION_ADAPTER") == "fixture":
            trigger = Path(directory) / name
            try:
                trigger.unlink()
            except FileNotFoundError:
                return
            os._exit(71)

    def recover_claims(self):
        while self.redis.lmove(PROCESSING, READY, src="RIGHT", dest="LEFT") is not None:
            pass

    def reconcile(self):
        now = time.time_ns()
        for key in self.store.staged():
            with self.store.transition(key) as frame:
                stage = read_json(frame / "stage.json")
                self.resume_authorized(key, frame)
                if stage is None or (frame / "terminal.json").is_file() or (frame / "parked.json").is_file():
                    continue
                if (frame.parent.parent / "retired.json").is_file():
                    continue
                if not (frame / "accepted.json").is_file():
                    if now - stage["staged_ns"] >= STAGE_EXPIRY_NS:
                        self.redis.lrem(READY, 0, key)
                        self.redis.lrem(PROCESSING, 0, key)
                        self.store.detach_staging(frame)
                    continue
                deferred = read_json(frame / "deferred.json")
                if deferred is None or deferred["due_ns"] <= now:
                    register(self.redis, key)

    def resume_authorized(self, key, frame):
        resolution = read_json(frame / "resolution.json")
        if resolution is None or resolution.get("action") != "retry":
            return
        if (frame / "terminal.json").exists():
            (frame / "resolution.json").unlink()
            sync_dir(frame)
            return
        parked = read_json(frame / "parked.json")
        if parked is not None and parked != resolution["parked"]:
            return
        (frame / "deferred.json").unlink(missing_ok=True)
        (frame / "parked.json").unlink(missing_ok=True)
        sync_dir(frame)
        gate = self.store.root / "configuration-error.json"
        if (read_json(gate) or {}).get("frame") == key:
            gate.unlink()
            sync_dir(self.store.root)
        register(self.redis, key)
        (frame / "resolution.json").unlink()
        sync_dir(frame)

    def maintenance(self):
        next_reconcile = 0.0
        ttl = int(os.getenv("SEGMENTATION_HEARTBEAT_TTL_S", "15"))
        while not self.stopping.is_set():
            try:
                now = time.monotonic()
                if now >= next_reconcile:
                    self.reconcile()
                    next_reconcile = now + self.reconcile_interval
                if (self.store.root / "configuration-error.json").exists():
                    self.redis.delete(HEARTBEAT)
                else:
                    self.redis.set(HEARTBEAT, json_bytes({"adapter": "fixture", "boot": self.boot}), ex=ttl)
            except (redis.RedisError, OSError, ValueError, KeyError):
                LOG.exception("live queue maintenance failed")
                next_reconcile = 0
            self.stopping.wait(min(5, self.reconcile_interval))

    def freeze(self, frame):
        pointer = read_json(frame / "submission.json")
        if pointer is not None:
            return pointer
        stage = read_json(frame / "stage.json")
        accepted = read_json(frame / "accepted.json")
        if sha256((frame / "source.jpg").read_bytes()).hexdigest() != stage["source_sha256"]:
            raise InvalidFrame("staged source digest changed")
        mask, pixels, manifest, manifest_bytes = fixture_inference(frame, accepted["binding"], self.fixture_root)
        mask_sha = sha256(pixels).hexdigest()
        manifest_id = sha256(manifest_bytes).hexdigest()
        mask_path = frame / "masks" / f"{mask_sha}.png"
        metadata = stage["metadata"]
        qualified = metadata.get("qualified_capture_utc_ns")
        timing_quality = "qualified" if metadata.get("timing_quality") == "qualified" else "unqualified"
        result = {
            "session_id": metadata["session_id"], "frame_index": metadata["frame_index"],
            "capture_utc_ns": metadata["capture_utc_ns"], "timing_quality": timing_quality,
            "qualified_capture_utc_ns": qualified if timing_quality == "qualified" else None,
            "inference_ms": None, "source_image_path": (frame / "source.jpg").relative_to(self.store.root).as_posix(),
            "source_sha256": stage["source_sha256"], "mask_image_path": mask_path.relative_to(self.store.root).as_posix(),
            "mask_sha256": mask_sha, "manifest_id": manifest_id, "hef_sha256": None,
            "model_version": manifest["model_version"], **mask_metrics(mask, manifest),
        }
        envelope = json_bytes(result)
        digest = sha256(envelope).hexdigest()
        if len(pixels) + len(envelope) + len(manifest_bytes) > stage["reserved_bytes"] - stage["source_bytes"]:
            raise InvalidFrame("fixture artifacts exceed frame allowance")
        save_bytes(mask_path, pixels, immutable=True)
        save_bytes(self.store.root / "manifests" / f"{manifest_id}.json", manifest_bytes, immutable=True)
        save_bytes(frame / "results" / f"{digest}.json", envelope, immutable=True)
        pointer = {"result_sha256": digest, "mask_sha256": mask_sha, "manifest_id": manifest_id}
        save_json(frame / "submission.json", pointer, immutable=True)
        return pointer

    def release(self, key):
        self.redis.lrem(PROCESSING, 0, key)

    def process(self, key):
        with self.store.transition(key) as frame:
            deferred = read_json(frame / "deferred.json")
            if (
                not (frame / "accepted.json").is_file()
                or (frame / "terminal.json").is_file()
                or (frame / "parked.json").is_file()
                or (deferred is not None and deferred["due_ns"] > time.time_ns())
            ):
                self.release(key)
                return
        self.test_fault("after-claim")
        try:
            with self.store.transition(key):
                pointer = self.freeze(frame)
        except InvalidFrame as error:
            LOG.warning("invalid source or fixture %s: %s", key, error)
            with self.store.transition(key):
                save_json(frame / "terminal.json", {"state": "failed", "reason": "invalid_source"}, immutable=True)
                self.release(key)
            return
        self.test_fault("after-mask")
        body = (frame / "results" / f'{pointer["result_sha256"]}.json').read_bytes()
        if sha256(body).hexdigest() != pointer["result_sha256"]:
            raise ValueError("stored result digest mismatch")
        with self.store.transition(key):
            attempt = (read_json(frame / "attempt.json") or {}).get("attempt", 0) + 1
            save_json(frame / "attempt.json", {"attempt": attempt, "may_have_committed": True})
        try:
            status, reply = send_result(self.url, self.token_file, body)
        except (OSError, http.client.HTTPException):
            status, reply = 503, None
        self.test_fault("after-submit")
        with self.store.transition(key):
            if status in (200, 201) and valid_response(reply, "committed") and all(
                reply[name] == value for name, value in pointer.items()
            ):
                save_json(frame / "terminal.json", {"state": "committed", "reason": None, **pointer}, immutable=True)
                self.test_fault("after-terminal")
            elif status == 409 and valid_response(reply, "conflict"):
                save_json(frame / "parked.json", {
                    "reason": "reconciliation_required", "attempt": attempt,
                    "candidate": pointer, "existing": reply,
                })
                self.test_fault("after-park")
            elif status == 400:
                save_json(frame / "parked.json", {"reason": "invalid_result", "attempt": attempt, "candidate": pointer})
            elif status in (409, 429) or status >= 500 or 200 <= status < 300:
                save_json(frame / "deferred.json", {
                    "due_ns": time.time_ns() + int(self.defer_seconds * 1e9), "attempt": attempt,
                })
                self.test_fault("after-defer")
            else:
                self.park_configuration(key, frame, f"result HTTP {status}")
            self.release(key)

    def park_configuration(self, key, frame, reason):
        save_json(frame / "parked.json", {
            "reason": "configuration_error", "detail": reason, "parked_ns": time.time_ns(),
        })
        save_json(self.store.root / "configuration-error.json", {"frame": key})
        self.redis.delete(HEARTBEAT)

    def run(self):
        if os.getenv("SEGMENTATION_ADAPTER") == "hailo":
            raise SystemExit("Hailo inference requires the replacement HEF and its verified runtime adapter")
        if os.getenv("SEGMENTATION_ADAPTER") != "fixture":
            raise ValueError("select the fixture adapter in an isolated deployment")
        with file_lock(self.store.locks / "worker.lock", blocking=False):
            self.recover_claims()
            self.reconcile()
            threading.Thread(target=self.maintenance, daemon=True).start()
            needs_recovery = False
            while True:
                try:
                    if needs_recovery:
                        self.recover_claims()
                        needs_recovery = False
                    if (self.store.root / "configuration-error.json").exists():
                        time.sleep(1)
                        continue
                    key = self.claims.blmove(READY, PROCESSING, 1, src="LEFT", dest="RIGHT")
                    if key is not None:
                        try:
                            self.process(key)
                        except (ValueError, KeyError, TypeError, OSError) as error:
                            LOG.exception("fixture configuration error for %s", key)
                            with self.store.transition(key) as frame:
                                self.park_configuration(key, frame, str(error))
                                self.release(key)
                except (redis.RedisError, OSError):
                    LOG.exception("live worker interrupted")
                    needs_recovery = True
                    time.sleep(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    Worker().run()
