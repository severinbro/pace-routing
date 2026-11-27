"""Durable state and Redis handoff for opt-in live segmentation on Linux."""

from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
from tempfile import mkdtemp, mkstemp
import time
from uuid import uuid4


READY = "halo:live:v2:ready"
PROCESSING = "halo:live:v2:processing"
HEARTBEAT = "halo:live:v2:heartbeat"
REGISTER = """
if not redis.call('LPOS', KEYS[1], ARGV[1])
   and not redis.call('LPOS', KEYS[2], ARGV[1]) then
    redis.call('RPUSH', KEYS[1], ARGV[1])
end
"""
SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
STAGE_EXPIRY_NS = 15 * 60 * 1_000_000_000


class AdmissionError(Exception):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def read_json(path):
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return None


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def mkdir_durable(path):
    if path.is_dir():
        return
    mkdir_durable(path.parent)
    path.mkdir(exist_ok=True)
    sync_dir(path.parent)


def save_bytes(path, content, *, immutable=False):
    mkdir_durable(path.parent)
    fd, name = mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if immutable:
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != content:
                    raise ValueError(f"incompatible immutable file: {path.name}")
        else:
            temporary.replace(path)
        sync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def save_json(path, value, *, immutable=False):
    save_bytes(path, json_bytes(value), immutable=immutable)


@contextmanager
def file_lock(path, *, shared=False, blocking=True):
    import fcntl

    mkdir_durable(path.parent)
    with path.open("ab") as stream:
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(stream, mode if blocking else mode | fcntl.LOCK_NB)
        yield


def register(redis, key):
    redis.eval(REGISTER, 2, READY, PROCESSING, key)


class FrameStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.locks = self.root / "locks"

    def frame(self, key):
        session, index = key.split("/", 1)
        if not SESSION.fullmatch(session) or not index.isascii() or not index.isdecimal():
            raise ValueError("invalid frame key")
        value = int(index)
        if str(value) != index or not 0 <= value <= 2**31 - 1:
            raise ValueError("invalid frame index")
        return self.root / "sessions" / session / "frames" / index

    @contextmanager
    def transition(self, key):
        name = sha256(key.encode("ascii")).hexdigest()
        with file_lock(self.locks / "maintenance.lock", shared=True):
            with file_lock(self.locks / "frames" / f"{name}.lock"):
                yield self.frame(key)

    def staged(self):
        for record in (self.root / "sessions").glob("*/frames/*/stage.json"):
            frame = record.parent
            yield f"{frame.parent.parent.name}/{frame.name}"

    def stage(self, frame, image, metadata, digest, reserve):
        tmp_root = self.root / ".tmp"
        mkdir_durable(tmp_root)
        temporary = Path(mkdtemp(prefix="stage-", dir=tmp_root))
        try:
            save_bytes(temporary / "source.jpg", image, immutable=True)
            save_json(temporary / "stage.json", {
                "schema": 1, "source_sha256": digest, "source_bytes": len(image),
                "metadata": metadata, "staged_ns": time.time_ns(), "reserved_bytes": reserve,
            }, immutable=True)
            sync_dir(temporary)
            mkdir_durable(frame.parent)
            if frame.exists():
                raise ValueError("frame directory already exists")
            temporary.rename(frame)
            sync_dir(frame.parent)
            sync_dir(tmp_root)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def pending(self):
        count = size = 0
        for key in self.staged():
            frame = self.frame(key)
            if not (frame / "terminal.json").exists():
                record = read_json(frame / "stage.json")
                if record is not None:
                    count += 1
                    size += record["reserved_bytes"]
        return count, size

    def live_bytes(self):
        return sum(path.stat().st_size for path in self.root.rglob("*") if path.is_file())

    def check_capacity(self, reserve):
        count, size = self.pending()
        if shutil.disk_usage(self.root).free < (int(os.getenv("MIN_FREE_DISK_MB", "2048")) << 20) + reserve:
            raise AdmissionError(507, "storage_reserve")
        if count >= int(os.getenv("SEGMENTATION_PENDING_MAX_FRAMES", "256")):
            raise AdmissionError(429, "pending_capacity")
        if size + reserve > int(os.getenv("SEGMENTATION_PENDING_MAX_BYTES", str(2 << 30))):
            raise AdmissionError(429, "pending_capacity")
        if self.live_bytes() + size + reserve > int(os.getenv("SEGMENTATION_LIVE_MAX_BYTES", str(4 << 30))):
            raise AdmissionError(429, "pending_capacity")

    def admit(self, redis, key, image, metadata):
        digest = sha256(image).hexdigest()
        with self.transition(key) as frame:
            if (frame.parent.parent / "retired.json").is_file():
                raise AdmissionError(410, "session_retired")
            stage = read_json(frame / "stage.json")
            if stage is not None and (
                stage["source_sha256"] != digest
                or stage["metadata"]["capture_utc_ns"] != metadata["capture_utc_ns"]
            ):
                raise AdmissionError(409, "frame_conflict")
            terminal = read_json(frame / "terminal.json")
            if terminal is not None:
                return 200, terminal["state"], terminal.get("reason")
            if (frame / "accepted.json").is_file():
                return 200, "accepted", None
            if os.getenv("SEGMENTATION_ADMISSION_ENABLED") != "true" or redis is None:
                raise AdmissionError(503, "admission_unavailable")
            heartbeat = redis.get(HEARTBEAT)
            if heartbeat is None:
                raise AdmissionError(503, "admission_unavailable")
            binding = json.loads(heartbeat)
            required_adapter = os.getenv("SEGMENTATION_REQUIRED_ADAPTER")
            if required_adapter and binding.get("adapter") != required_adapter:
                raise AdmissionError(503, "admission_unavailable")
            if binding.get("adapter") == "fixture":
                index_path = Path(os.environ["SEGMENTATION_FIXTURE_INDEX"])
                fixture = read_json(index_path)
                record = fixture["by_source_sha256"].get(digest)
                if record is None:
                    raise AdmissionError(400, "invalid_frame")
                binding = {"adapter": "fixture", "fixture": record}
            if stage is None:
                reserve = len(image) + int(os.getenv("SEGMENTATION_FRAME_ARTIFACT_MAX_BYTES", str(1 << 20)))
                with file_lock(self.locks / "budget.lock"):
                    self.check_capacity(reserve)
                    self.stage(frame, image, metadata, digest, reserve)
            register(redis, key)
            save_json(frame / "accepted.json", {"schema": 1, "accepted_ns": time.time_ns(), "binding": binding})
            return 201, "accepted", None

    def detach_staging(self, frame):
        trash = self.root / ".trash"
        mkdir_durable(trash)
        destination = trash / uuid4().hex
        with file_lock(self.locks / "budget.lock"):
            frame.rename(destination)
            sync_dir(frame.parent)
            sync_dir(trash)
        shutil.rmtree(destination)
