"""Fixture-only proxy for losing a response after Django accepted a result."""

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path


class FaultProxy(BaseHTTPRequestHandler):
    def do_POST(self):
        data = self.rfile.read(int(self.headers["Content-Length"]))
        upstream = http.client.HTTPConnection("pace-web", 8000, timeout=4)
        try:
            upstream.request("POST", self.path, data, {
                "Content-Type": "application/json",
                "X-SENSE-Segmentation-Token": self.headers["X-SENSE-Segmentation-Token"],
            })
            response = upstream.getresponse()
            body = response.read()
            trigger = Path("/faults/drop-after-commit")
            if response.status == 201 and trigger.is_file():
                payload = json.loads(data)
                if trigger.read_text().strip() == f'{payload["session_id"]}/{payload["frame_index"]}':
                    trigger.unlink()
                    self.close_connection = True
                    return
            self.send_response(response.status)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        finally:
            upstream.close()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8090), FaultProxy).serve_forever()
