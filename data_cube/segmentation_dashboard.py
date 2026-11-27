"""Staff inspection and downloads for committed camera frame results."""

import csv
from io import BytesIO
import json

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.db.models import Max
from django.http import FileResponse, Http404, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe
from PIL import Image

from .models import SegmentationFrame
from .segmentation_overlay import render_overlay, verified_file
from .segmentation_views import SESSION

TABS = {"overview": "Overview", "preview": "Preview", "export": "Export"}
METRICS = {
    "gvi": "Green view index (GVI)", "sky_ratio": "Sky", "bsi": "Building share index (BSI)",
    "sidewalk_ratio": "Sidewalk", "obstacle_ratio": "Obstacles",
}
CSV_FIELDS = (
    "session_id", "frame_index", "capture_utc_ns", "qualified_capture_utc_ns", "timing_quality",
    "committed_at", "model_version", "hef_sha256", "manifest_id", "result_sha256",
    "source_sha256", "mask_sha256", "inference_ms", "valid_pixel_count", "void_pixel_count",
    *METRICS, "class_counts", "class_ratios_json",
)
ARTIFACTS = {
    "source.jpg": ("image/jpeg", "Source JPEG"),
    "mask.png": ("image/png", "Class-ID mask PNG"),
    "overlay.png": ("image/png", "Overlay PNG"),
    "manifest.json": ("application/json", "Class manifest JSON"),
}


@staff_member_required(login_url="admin_login")
@require_safe
@never_cache
def dashboard(request, tab="overview"):
    if tab not in TABS:
        raise Http404
    results = SegmentationFrame.objects.all()
    summaries = results.order_by().values("session_id").annotate(latest=Max("committed_at"))
    sessions = Paginator(summaries.order_by("-latest", "session_id"), 12).get_page(request.GET.get("sessions"))
    session = request.GET.get("session")
    if session and not SESSION.fullmatch(session):
        raise Http404
    if not session and sessions.object_list:
        session = sessions.object_list[0]["session_id"]

    frame = frame_page = None
    if session:
        frames = results.filter(session_id=session).order_by("-frame_index")
        frame_page = Paginator(frames, 20).get_page(request.GET.get("page"))
        if frame_page.object_list:
            index = request.GET.get("frame")
            if index is None:
                frame = frame_page.object_list[0]
            elif len(index) <= 10 and index.isascii() and index.isdecimal() and int(index) <= 2**31 - 1:
                frame = get_object_or_404(frames, frame_index=int(index))
            else:
                raise Http404

    metrics, class_stats = [], []
    if frame and tab == "overview":
        for field, label in METRICS.items():
            ratio = getattr(frame, field)
            metrics.append((label, ratio * 100 if ratio is not None else None))
        class_stats = [
            (key, frame.class_counts[key], frame.class_ratios_json[key] * 100)
            for key in sorted(frame.class_counts, key=int)
        ]

    return render(request, "data_cube/segmentation_list.html", {
        "tab": tab, "tabs": TABS, "sessions": sessions, "session": session, "frame_page": frame_page,
        "frame": frame, "selected_index": frame.frame_index if frame else None,
        "metrics": metrics, "class_stats": class_stats,
        "downloads": [(name, label) for name, (_, label) in ARTIFACTS.items()],
    })


def _csv_cell(value):
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


class _CSVLine:
    def write(self, value):
        return value


@staff_member_required(login_url="admin_login")
@require_safe
@never_cache
def export_csv(request, session):
    if not SESSION.fullmatch(session):
        raise Http404
    frames = SegmentationFrame.objects.filter(session_id=session).order_by("frame_index")
    if not frames.exists():
        raise Http404

    def rows():
        writer = csv.writer(_CSVLine())
        yield writer.writerow(CSV_FIELDS)
        for values in frames.values_list(*CSV_FIELDS).iterator(chunk_size=500):
            yield writer.writerow([_csv_cell(value) for value in values])

    response = StreamingHttpResponse(rows(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="segmentation-{session}.csv"'
    return response


@staff_member_required(login_url="admin_login")
@require_safe
@never_cache
def artifact(request, pk, name):
    if name not in ARTIFACTS:
        raise Http404
    frame = get_object_or_404(SegmentationFrame, pk=pk)
    root = settings.SEGMENTATION_LIVE_ROOT
    try:
        if name == "overlay.png":
            content = BytesIO()
            render_overlay(frame, root, content)
            content.seek(0)
        else:
            relative, digest, limit = {
                "source.jpg": (frame.source_image_path, frame.source_sha256, 5 << 20),
                "mask.png": (frame.mask_image_path, frame.mask_sha256, 32 << 20),
                "manifest.json": (f"manifests/{frame.manifest_id}.json", frame.manifest_id, 64 << 10),
            }[name]
            content = BytesIO(verified_file(root, relative, digest, limit))
    except (OSError, ValueError, KeyError, TypeError, Image.DecompressionBombError):
        return HttpResponse("This frame artifact is unavailable or failed verification.", status=404, content_type="text/plain")
    return FileResponse(
        content, as_attachment=request.GET.get("download") == "1", filename=f"{frame.session_id}-{frame.frame_index}-{name}",
        content_type=ARTIFACTS[name][0],
    )
