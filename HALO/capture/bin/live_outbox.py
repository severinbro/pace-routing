"""Shared publication and retirement for HALO's live camera outbox."""

from contextlib import contextmanager
import fcntl
import json
import logging
import os
import re
import shutil
from tempfile import NamedTemporaryFile
from uuid import uuid4


LOG = logging.getLogger("halo_live_outbox")
FRAME_RE = re.compile(r"frame_(\d{6})\.meta\.json")


@contextmanager
def locked(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".lock").open("ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, payload):
    with NamedTemporaryFile(dir=path.parent, prefix=".live-", delete=False) as stream:
        temporary = path.parent / os.path.basename(stream.name)
        try:
            stream.write(json.dumps(payload, sort_keys=True, allow_nan=False).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
            temporary.replace(path)
            sync_dir(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


def frozen_path(jpg):
    return jpg.with_suffix(".upload.json")


def list_frames(root):
    if not root.is_dir():
        return []
    frames = [
        (meta.stat().st_mtime_ns, meta, meta.with_name(f"frame_{match[1]}.jpg"))
        for meta in root.glob("*/frame_*.meta.json") if (match := FRAME_RE.fullmatch(meta.name))
    ]
    return sorted(frames)


def remove_pair(meta, jpg):
    meta.unlink(missing_ok=True)
    jpg.unlink(missing_ok=True)
    frozen_path(jpg).unlink(missing_ok=True)
    sync_dir(meta.parent)


def enforce_cap(root, max_frames, max_bytes=None):
    frames = list_frames(root)
    count = len(frames)
    sizes = {
        meta: sum(path.stat().st_size for path in (meta, jpg, frozen_path(jpg)) if path.is_file())
        for _, meta, jpg in frames
    }
    size = sum(sizes.values())
    evicted = 0
    for _, meta, jpg in frames:
        if count <= max_frames and (max_bytes is None or size <= max_bytes):
            break
        if frozen_path(jpg).exists():
            continue
        remove_pair(meta, jpg)
        LOG.warning("Live outbox limit evicted %s/%s", meta.parent.name, jpg.name)
        count -= 1
        size -= sizes[meta]
        evicted += 1
    return evicted


def quarantine(meta, jpg, reason, status=None):
    root = meta.parent.parent.parent / "live_quarantine"
    root.mkdir(exist_ok=True)
    entry = root / f"{meta.parent.name}-{jpg.stem}-{uuid4().hex[:12]}"
    staged = root / ("." + entry.name)
    staged.mkdir()
    try:
        for path in (meta, jpg, frozen_path(jpg)):
            if path.is_file():
                os.link(path, staged / path.name)
        write_json(staged / "reason.json", {"reason": reason, "http_status": status})
        staged.rename(entry)
        sync_dir(root)
        remove_pair(meta, jpg)
    finally:
        if staged.exists():
            shutil.rmtree(staged)
    entries = sorted((path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")),
                     key=lambda path: path.stat().st_mtime_ns)
    for expired in entries[:-120]:
        LOG.warning("Live quarantine limit discarded %s", expired.name)
        shutil.rmtree(expired)
    LOG.warning("Quarantined %s: %s", entry.name, reason)
