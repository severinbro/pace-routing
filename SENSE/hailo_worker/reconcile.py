"""Resolve a parked segmentation submission without changing its artifacts."""

import argparse
from hashlib import sha256

from receiver.live_frame_state import PROCESSING, read_json, save_json

from .worker import Worker, send_result, valid_response


def resolve(worker, key, action, result_sha):
    with worker.store.transition(key) as frame:
        parked = read_json(frame / "parked.json")
        terminal = read_json(frame / "terminal.json")
        if terminal is not None:
            if action == "accept-existing" and terminal.get("result_sha256") == result_sha:
                return
            raise ValueError("frame is already terminal")
        if parked is None:
            raise ValueError("frame is not parked")
        pointer = read_json(frame / "submission.json")
        if action == "retry":
            if parked["reason"] != "configuration_error":
                raise ValueError("only configuration errors can be retried unchanged")
            if not worker.token_file.read_text(encoding="utf-8").strip():
                raise ValueError("result token is still missing")
            save_json(frame / "resolution.json", {"action": "retry", "parked": parked})
            worker.resume_authorized(key, frame)
            return
        if parked["reason"] != "reconciliation_required" or pointer is None:
            raise ValueError("frame has no confirmed conflicting submission")
        body = (frame / "results" / f'{pointer["result_sha256"]}.json').read_bytes()
        if sha256(body).hexdigest() != pointer["result_sha256"]:
            raise ValueError("frozen result digest changed")
    status, reply = send_result(worker.url, worker.token_file, body)
    if status != 409 or not valid_response(reply, "conflict") or reply["result_sha256"] != result_sha:
        raise ValueError("Django did not confirm the requested committed result")
    with worker.store.transition(key) as frame:
        if read_json(frame / "parked.json") != parked:
            raise ValueError("parked state changed")
        save_json(frame / "terminal.json", {
            **reply, "state": "committed", "reason": None, "operator_resolution": True,
        }, immutable=True)
        worker.redis.lrem(PROCESSING, 0, key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--frame-index", required=True, type=int)
    parser.add_argument("--action", choices=["retry", "accept-existing"], required=True)
    parser.add_argument("--result-sha256")
    args = parser.parse_args()
    if args.action == "accept-existing" and not args.result_sha256:
        parser.error("accept-existing requires --result-sha256")
    resolve(Worker(), f"{args.session_id}/{args.frame_index}", args.action, args.result_sha256)


if __name__ == "__main__":
    main()
