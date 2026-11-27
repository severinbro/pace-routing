"""Verified segmentation image handling shared by the importer and renderer."""

from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path

from PIL import Image


def verified_file(root, relative, digest, limit):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("artifact path outside live root")
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit or sha256(content).hexdigest() != digest:
        raise ValueError("artifact digest or size mismatch")
    return content


def load_images(root, result):
    source_bytes = verified_file(root, result["source_image_path"], result["source_sha256"], 5 << 20)
    mask_bytes = verified_file(root, result["mask_image_path"], result["mask_sha256"], 32 << 20)
    manifest_bytes = verified_file(
        root, f'manifests/{result["manifest_id"]}.json', result["manifest_id"], 64 << 10
    )
    with Image.open(BytesIO(source_bytes), formats=["JPEG"]) as source:
        rgb = source.convert("RGB")
    with Image.open(BytesIO(mask_bytes), formats=["PNG"]) as mask:
        if mask.mode not in ("L", "P", "I;16", "I;16B") or mask.size != rgb.size:
            raise ValueError("mask must match source geometry and contain class IDs")
        labels = mask.copy()
    manifest = json.loads(manifest_bytes)
    mode_depth = 16 if labels.mode.startswith("I;16") else 8
    if manifest.get("schema") != 1 or manifest.get("mask_bit_depth") != mode_depth:
        raise ValueError("invalid manifest schema or mask depth")
    if manifest.get("model_version") != result["model_version"]:
        raise ValueError("model version does not match manifest")
    if manifest.get("hef_sha256") != result["hef_sha256"]:
        raise ValueError("HEF digest does not match manifest")
    return rgb, labels, manifest


def render_overlay(frame, root, path):
    fields = ("source_image_path", "source_sha256", "mask_image_path", "mask_sha256",
              "manifest_id", "model_version", "hef_sha256")
    source, mask, manifest = load_images(root, {field: getattr(frame, field) for field in fields})
    palette = {int(key): value["rgb"] for key, value in manifest["classes"].items()}
    void = set(manifest["void_ids"])
    pixels = []
    for rgb, label in zip(source.get_flattened_data(), mask.get_flattened_data(), strict=True):
        if label in void:
            pixels.append(rgb)
        else:
            pixels.append(tuple((channel + color) // 2 for channel, color in zip(rgb, palette[label], strict=True)))
    source.putdata(pixels)
    source.save(path, format="PNG")
