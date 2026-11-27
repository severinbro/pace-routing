"""Fixture adapter for testing the worker before the HEF is available."""

from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path

from PIL import Image


class InvalidFrame(ValueError):
    """A frame that cannot produce a valid result with its pinned fixture."""


def fixture_file(root, relative):
    resolved = (root / relative).resolve()
    if Path(relative).is_absolute() or not resolved.is_relative_to(root):
        raise InvalidFrame("fixture path escapes its mount")
    return resolved.read_bytes()


def fixture_inference(frame, binding, root):
    if binding.get("adapter") != "fixture":
        raise ValueError("unsupported inference adapter")
    record = binding["fixture"]
    mask_bytes = fixture_file(root, record["mask"])
    manifest_bytes = fixture_file(root, record["manifest"])
    if (
        sha256(mask_bytes).hexdigest() != record["mask_sha256"]
        or sha256(manifest_bytes).hexdigest() != record["manifest_id"]
    ):
        raise InvalidFrame("pinned fixture digest changed")
    try:
        manifest = json.loads(manifest_bytes)
        with Image.open(frame / "source.jpg", formats=["JPEG"]) as source:
            source_size = source.size
            source.verify()
        with Image.open(BytesIO(mask_bytes), formats=["PNG"]) as mask:
            if mask.size != source_size or mask.mode not in ("P", "L"):
                raise InvalidFrame("fixture geometry or mask encoding mismatch")
            if manifest.get("schema") != 1 or manifest.get("mask_bit_depth") != 8:
                raise InvalidFrame("unsupported fixture manifest")
            return mask.copy(), mask_bytes, manifest, manifest_bytes
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise InvalidFrame(str(error)) from error
