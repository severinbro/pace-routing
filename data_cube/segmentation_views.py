"""Private endpoint for immutable SENSE segmentation results."""

from collections import Counter
from hashlib import sha256
import hmac
import json
import math
import re

from django.conf import settings
from django.db import DatabaseError, IntegrityError, transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from PIL import Image

from .models import SegmentationFrame
from .segmentation_overlay import load_images

SHA = re.compile(r"[0-9a-f]{64}")
SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
GROUPS = ("gvi", "sky_ratio", "bsi", "sidewalk_ratio", "obstacle_ratio")
RESULT_FIELDS = {
    "session_id", "frame_index", "capture_utc_ns", "qualified_capture_utc_ns",
    "timing_quality", "inference_ms", "class_counts", "valid_pixel_count", "void_pixel_count",
    "class_ratios_json", "source_image_path", "source_sha256", "mask_image_path", "mask_sha256",
    "model_version", "hef_sha256", "manifest_id", *GROUPS,
}


def error(status, code):
    return JsonResponse({"error": code}, status=status)


def bounded_int(value, maximum):
    return type(value) is int and 0 <= value <= maximum


def valid_sha(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def validate_result(data):
    if not isinstance(data, dict) or data.keys() != RESULT_FIELDS:
        raise ValueError("invalid result fields")
    session, index = data["session_id"], data["frame_index"]
    if not isinstance(session, str) or not SESSION.fullmatch(session) or not bounded_int(index, 2**31 - 1):
        raise ValueError("invalid frame identity")
    if not all(valid_sha(data[field]) for field in ("source_sha256", "mask_sha256", "manifest_id")):
        raise ValueError("invalid artifact digest")
    if data["hef_sha256"] is not None and not valid_sha(data["hef_sha256"]):
        raise ValueError("invalid HEF digest")
    version = data["model_version"]
    if not isinstance(version, str) or not 1 <= len(version) <= 64:
        raise ValueError("invalid model version")
    base = f"sessions/{session}/frames/{index}"
    if data["source_image_path"] != f"{base}/source.jpg":
        raise ValueError("invalid source path")
    if data["mask_image_path"] != f'{base}/masks/{data["mask_sha256"]}.png':
        raise ValueError("invalid mask path")
    capture = data["capture_utc_ns"]
    qualified = data["qualified_capture_utc_ns"]
    if not bounded_int(capture, 2**63 - 1):
        raise ValueError("invalid capture time")
    if data["timing_quality"] == "qualified":
        if not bounded_int(qualified, 2**63 - 1) or qualified != capture:
            raise ValueError("missing qualified capture time")
    elif data["timing_quality"] != "unqualified" or qualified is not None:
        raise ValueError("invalid timing quality")
    elapsed = data["inference_ms"]
    if elapsed is not None and (type(elapsed) not in (float, int) or not math.isfinite(elapsed) or elapsed < 0):
        raise ValueError("invalid inference time")


def require_ratio(value, numerator, denominator):
    if not denominator:
        if value is not None:
            raise ValueError("ratio must be null with no valid pixels")
    elif (
        type(value) not in (float, int)
        or not math.isfinite(value)
        or not 0 <= value <= 1
        or not math.isclose(value, numerator / denominator, abs_tol=1e-6)
    ):
        raise ValueError("ratio does not match mask")


def validate_artifacts(data):
    _, mask, manifest = load_images(settings.SEGMENTATION_LIVE_ROOT, data)
    all_counts = Counter(mask.get_flattened_data())
    void = set(manifest["void_ids"])
    allowed = set(manifest["classes"])
    counts = {str(k): v for k, v in all_counts.items() if k not in void}
    valid_count = sum(counts.values())
    reported_counts, ratios = data["class_counts"], data["class_ratios_json"]
    if (
        not isinstance(reported_counts, dict)
        or any(not bounded_int(count, 2**63 - 1) for count in reported_counts.values())
        or not counts.keys() <= allowed
        or counts != reported_counts
        or not bounded_int(data["valid_pixel_count"], 2**63 - 1)
        or not bounded_int(data["void_pixel_count"], 2**63 - 1)
        or data["valid_pixel_count"] != valid_count
        or data["void_pixel_count"] != mask.width * mask.height - valid_count
        or not isinstance(ratios, dict)
        or ratios.keys() != counts.keys()
    ):
        raise ValueError("pixel counts do not match mask")
    for label, count in counts.items():
        require_ratio(ratios[label], count, valid_count)
    for name in GROUPS:
        labels = {str(label) for label in manifest["metric_groups"].get(name, [])}
        if not labels <= allowed:
            raise ValueError("unknown class in metric group")
        require_ratio(data[name], sum(counts.get(label, 0) for label in labels), valid_count)


def receipt(frame, status):
    return JsonResponse({
        "state": "conflict" if status == 409 else "committed",
        "result_sha256": frame.result_sha256,
        "mask_sha256": frame.mask_sha256,
        "manifest_id": frame.manifest_id,
    }, status=status)


@csrf_exempt
@require_POST
def commit_frame(request):
    try:
        token = settings.SEGMENTATION_RESULT_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return error(503, "unavailable")
    if not token:
        return error(503, "unavailable")
    supplied = request.headers.get("X-SENSE-Segmentation-Token", "")
    if not hmac.compare_digest(token.encode("utf-8"), supplied.encode("utf-8")):
        return error(401, "unauthorized")
    try:
        raw = request.read((64 << 10) + 1)
        if len(raw) > 64 << 10:
            return error(400, "invalid_result")
        data = json.loads(raw.decode("utf-8"))
        validate_result(data)
        digest = sha256(raw).hexdigest()
        frames = SegmentationFrame.objects.filter(session_id=data["session_id"], frame_index=data["frame_index"])
        previous = frames.first()
        if previous is not None:
            return receipt(previous, 200 if previous.result_sha256 == digest else 409)
        validate_artifacts(data)
        try:
            with transaction.atomic():
                frame = SegmentationFrame.objects.create(result_sha256=digest, **data)
        except IntegrityError:
            previous = frames.first()
            if previous is None:
                raise
            return receipt(previous, 200 if previous.result_sha256 == digest else 409)
        return receipt(frame, 201)
    except (ValueError, TypeError, KeyError, AttributeError, OSError, OverflowError, Image.DecompressionBombError):
        return error(400, "invalid_result")
    except DatabaseError:
        return error(503, "unavailable")
