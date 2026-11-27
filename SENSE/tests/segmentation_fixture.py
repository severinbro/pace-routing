"""Prepare and exercise the isolated Protocol v2 segmentation stack."""

import argparse
from hashlib import sha256
import http.client
import json
from pathlib import Path
import secrets
import shutil
import ssl
import subprocess
import sys
import time
import unittest

from PIL import Image

from receiver.live_frame_state import FrameStore, json_bytes, read_json, save_bytes
from test_live_receiver import build_multipart


FIXTURES = Path("/fixtures")
SECRETS = Path("/run/secrets")
DATA = Path("/data/live-v2")
OUTPUT = Path("/fixture-output")
CASE = Path(__file__).with_name("fixtures") / "segmentation" / "case.json"


def digest(content):
    return sha256(content).hexdigest()


def add_fixture(jpeg, mask, manifest, expected):
    key = digest(jpeg.read_bytes())
    directory = FIXTURES / key
    directory.mkdir(exist_ok=True)
    with Image.open(jpeg, formats=["JPEG"]) as source, Image.open(mask, formats=["PNG"]) as labels:
        if source.size != labels.size:
            raise ValueError("source and expected mask dimensions differ")
    for name, path in (("mask", mask), ("manifest", manifest), ("expected", expected)):
        filename = "mask.png" if name == "mask" else f"{name}.json"
        save_bytes(directory / filename, path.read_bytes(), immutable=True)
    row = {
        "mask": f"{key}/mask.png", "mask_sha256": digest(mask.read_bytes()),
        "manifest": f"{key}/manifest.json", "manifest_id": digest(manifest.read_bytes()),
        "expected": f"{key}/expected.json",
    }
    index = read_json(FIXTURES / "index.json") or {"schema": 1, "by_source_sha256": {}}
    previous = index["by_source_sha256"].setdefault(key, row)
    if previous != row:
        raise ValueError("fixture index changes an existing source")
    save_bytes(FIXTURES / "index.json", json_bytes(index))


def prepare():
    FIXTURES.mkdir(exist_ok=True)
    SECRETS.mkdir(exist_ok=True)
    for name in ("halo_upload", "segmentation_internal"):
        token = SECRETS / name
        if not token.exists():
            token.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    if not (SECRETS / "server.crt").exists():
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
            "-subj", "/CN=fixture-ingress",
            "-addext", "subjectAltName=DNS:fixture-ingress,DNS:root-ingress",
            "-keyout", str(SECRETS / "server.key"), "-out", str(SECRETS / "server.crt"),
        ], check=True, capture_output=True)
        shutil.copyfile(SECRETS / "server.crt", SECRETS / "pace.crt")
        shutil.copyfile(SECRETS / "server.key", SECRETS / "pace.key")
    case = read_json(CASE)
    size = case["width"], case["height"]
    jpeg = FIXTURES / "source.jpg"
    Image.new("RGB", size, tuple(case["source_rgb"])).save(jpeg, format="JPEG")
    labels = Image.new("L", size, case["void_id"])
    for value, points in case["regions"].items():
        for point in points:
            labels.putpixel(tuple(point), int(value))
    mask = FIXTURES / "mask.png"
    labels.save(mask)
    manifest = FIXTURES / "manifest.json"
    manifest.write_bytes(json_bytes({
        "schema": 1, "adapter": "fixture", "model_version": "fixture-v1", "hef_sha256": None,
        "mask_bit_depth": 8, "void_ids": [case["void_id"]],
        "classes": {"1": {"name": "vegetation", "rgb": [200, 0, 0]}, "2": {"name": "building", "rgb": [0, 200, 0]}},
        "metric_groups": {"gvi": [1], "bsi": [2]},
    }))
    expected = FIXTURES / "expected.json"
    expected.write_bytes(json_bytes({
        "schema": 1, "width": size[0], "height": size[1],
        "source_sha256": digest(jpeg.read_bytes()), "mask_sha256": digest(mask.read_bytes()),
        **case["expected"],
    }))
    add_fixture(jpeg, mask, manifest, expected)


def request(host, method, path, body=None, headers=None, *, tls=True):
    if tls:
        context = ssl.create_default_context(cafile=str(SECRETS / "server.crt"))
        conn = http.client.HTTPSConnection(host, context=context, timeout=8)
    else:
        conn = http.client.HTTPConnection(host, 8000, timeout=8)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def upload(session, index=0, *, image=None, capture=123):
    metadata = {"session_id": session, "frame_index": index, "capture_utc_ns": capture}
    if image is None:
        image = (FIXTURES / "source.jpg").read_bytes()
    body, content_type = build_multipart(json_bytes(metadata), image)
    status, response = request("fixture-ingress", "POST", f"/halo-upload/v2/sessions/{session}/frames", body, {
        "Authorization": f"Bearer {(SECRETS / 'halo_upload').read_text().strip()}",
        "Content-Type": content_type,
    })
    return status, json.loads(response)


def wait_result(session, index=0, timeout=30):
    frame = FrameStore(DATA).frame(f"{session}/{index}")
    stop = time.monotonic() + timeout
    while time.monotonic() < stop:
        terminal = read_json(frame / "terminal.json")
        if terminal is not None:
            if terminal["state"] != "committed":
                raise RuntimeError(terminal)
            body = (frame / "results" / f'{terminal["result_sha256"]}.json').read_bytes()
            if digest(body) != terminal["result_sha256"]:
                raise RuntimeError("result digest changed")
            return json.loads(body)
        if (frame / "parked.json").exists():
            raise RuntimeError(read_json(frame / "parked.json"))
        time.sleep(0.1)
    raise TimeoutError(f"result not committed: {session}/{index}")


def validate_counts(result, expected):
    for field in ("class_counts", "valid_pixel_count", "void_pixel_count"):
        if result[field] != expected[field]:
            raise ValueError(f"{field} does not match fixture: {result[field]}")


def validate_overlay(path, expected):
    with Image.open(path, formats=["PNG"]) as overlay:
        if overlay.size != (expected["width"], expected["height"]):
            raise ValueError("overlay dimensions differ")
        for pixel in expected["overlay_pixels"]:
            if list(overlay.getpixel((pixel["x"], pixel["y"]))) != pixel["rgb"]:
                raise ValueError(f"overlay pixel differs: {pixel}")


def replay(args):
    jpeg, mask, manifest, expected = (Path(item) for item in (args.jpeg, args.mask, args.manifest, args.expected))
    expectations = read_json(expected)
    if expectations["source_sha256"] != digest(jpeg.read_bytes()) or expectations["mask_sha256"] != digest(mask.read_bytes()):
        raise ValueError("replay artifacts differ from expectations")
    add_fixture(jpeg, mask, manifest, expected)
    session = f"fixture-replay-{time.time_ns()}"
    status, receipt = upload(session, image=jpeg.read_bytes())
    if status != 201:
        raise RuntimeError(receipt)
    validate_counts(wait_result(session), expectations)
    print(session)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("prepare")
    actions.add_parser("verify")
    replay_args = actions.add_parser("replay")
    for name in ("jpeg", "mask", "manifest", "expected"):
        replay_args.add_argument(f"--{name}", required=True)
    compare = actions.add_parser("check-overlay")
    compare.add_argument("--expected", required=True, type=Path)
    compare.add_argument("--overlay", default=OUTPUT / "overlay.png", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare()
    elif args.action == "replay":
        replay(args)
    elif args.action == "check-overlay":
        validate_overlay(args.overlay, read_json(args.expected))
    else:
        pattern = "test_segmentation_integration.py"
        suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent), pattern=pattern)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
