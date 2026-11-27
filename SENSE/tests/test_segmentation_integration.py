"""End-to-end tests for the isolated SENSE segmentation stack."""

from hashlib import sha256
import json
from pathlib import Path
import tempfile
import time
import unittest

from receiver.live_frame_state import FrameStore
from segmentation_fixture import DATA, FIXTURES, SECRETS, request, upload, validate_counts, wait_result
from test_receiver import create_test_session_files, pack_tar


@unittest.skipUnless((SECRETS / "server.crt").is_file(), "isolated Compose stack required")
class SegmentationIntegrationTests(unittest.TestCase):
    def session(self, label):
        return f"fixture-{label}-{time.time_ns()}"

    def check_result(self, session, index=0):
        result = wait_result(session, index)
        expected = json.loads((FIXTURES / "expected.json").read_bytes())
        validate_counts(result, expected)
        mask = DATA / result["mask_image_path"]
        self.assertEqual(sha256(mask.read_bytes()).hexdigest(), result["mask_sha256"])
        self.assertEqual(result["timing_quality"], "unqualified")
        self.assertIsNone(result["hef_sha256"])
        return result

    def test_replay_ambiguous_django_commit(self):
        session = self.session("lost-response")
        trigger = Path("/faults/drop-after-commit")
        trigger.write_text(f"{session}/0", encoding="utf-8")
        self.addCleanup(trigger.unlink, missing_ok=True)
        self.assertEqual(upload(session, 0)[0], 201)
        self.assertEqual(upload(session, 1)[0], 201)
        self.check_result(session, 1)
        self.assertFalse(trigger.exists())
        first = self.check_result(session, 0)
        self.assertEqual(upload(session, 0), (200, {
            "session_id": session, "frame_index": 0, "state": "committed", "reason": None,
        }))
        self.assertEqual(upload(session, 0, capture=124)[0], 409)
        self.assertEqual(self.check_result(session, 0), first)

    def test_worker_restarts(self):
        for boundary in ("after-claim", "after-mask", "after-submit", "after-terminal"):
            with self.subTest(boundary=boundary):
                session = self.session(boundary)
                trigger = Path("/faults") / boundary
                trigger.touch()
                self.addCleanup(trigger.unlink, missing_ok=True)
                self.assertEqual(upload(session)[0], 201)
                self.check_result(session)
                self.assertFalse(trigger.exists())

    def test_private_endpoint_and_public_proxies(self):
        path = "/internal/segmentation/frames/"
        for proxy in ("fixture-ingress", "root-ingress"):
            self.assertEqual(request(proxy, "POST", path, b"{}")[0], 404)
        self.assertEqual(request("pace-web", "GET", path, tls=False)[0], 405)
        self.assertEqual(request("pace-web", "POST", path, b"{}", tls=False)[0], 401)

    def test_existing_v1_path(self):
        session = self.session("archive")
        with tempfile.TemporaryDirectory() as tmp:
            payload = pack_tar(create_test_session_files(Path(tmp) / session))
        archive_digest = sha256(payload).hexdigest()
        status, data = request("fixture-ingress", "PUT", f"/halo-upload/v1/sessions/{session}", payload, {
            "Authorization": f"Bearer {(SECRETS / 'halo_upload').read_text().strip()}",
            "X-HALO-Protocol-Version": "1", "X-HALO-Session-ID": session,
            "X-HALO-Archive-SHA256": archive_digest, "Content-Type": "application/x-tar",
        })
        self.assertEqual(status, 201, data)
        self.assertEqual(json.loads(data)["archive_sha256"], archive_digest)

    def test_direct_replay_of_frozen_result(self):
        session = self.session("direct")
        self.assertEqual(upload(session)[0], 201)
        self.check_result(session)
        frame = FrameStore(DATA).frame(f"{session}/0")
        terminal = json.loads((frame / "terminal.json").read_bytes())
        body = (frame / "results" / f'{terminal["result_sha256"]}.json').read_bytes()
        headers = {
            "Content-Type": "application/json",
            "X-SENSE-Segmentation-Token": (SECRETS / "segmentation_internal").read_text().strip(),
        }
        path = "/internal/segmentation/frames/"
        status, receipt = request("pace-web", "POST", path, body, headers, tls=False)
        self.assertEqual(status, 200, receipt)
        self.assertEqual(json.loads(receipt)["result_sha256"], sha256(body).hexdigest())
        status, receipt = request("pace-web", "POST", path, body + b" ", headers, tls=False)
        self.assertEqual(status, 409, receipt)
        self.assertEqual(json.loads(receipt)["result_sha256"], sha256(body).hexdigest())
