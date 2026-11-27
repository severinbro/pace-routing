from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from data_cube.models import SegmentationFrame
from data_cube.segmentation_overlay import render_overlay


class Command(BaseCommand):
    help = "Render a committed HALO result to a PNG file."

    def add_arguments(self, parser):
        parser.add_argument("session_id")
        parser.add_argument("frame_index", type=int)
        parser.add_argument("output", type=Path)

    def handle(self, session_id, frame_index, output, **options):
        try:
            frame = SegmentationFrame.objects.get(session_id=session_id, frame_index=frame_index)
            render_overlay(frame, settings.SEGMENTATION_LIVE_ROOT, output)
        except (SegmentationFrame.DoesNotExist, OSError, ValueError) as error:
            raise CommandError(str(error)) from error
