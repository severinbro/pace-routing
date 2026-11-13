#!/usr/bin/env python3
"""Systemd-owned recording runner; survives SSH/browser disconnects."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
STATE = DATA / 'ui-state.json'
STOP = DATA / '.ui_stop'
RESERVE = 4 * 1024**3


def state(**values):
    temp = DATA / '.ui-state.json.tmp'
    temp.write_text(json.dumps(values) + '\n')
    temp.replace(STATE)


def main():
    started = time.time()
    state(stage='recording', started_at=started)
    with (DATA / 'ui-record.log').open('a') as log:
        process = subprocess.Popen(['/bin/bash', str(ROOT / 'control/record_ui.sh')], cwd=ROOT,
                                   stdout=log, stderr=subprocess.STDOUT)
        reason = None
        stop_started = None
        while process.poll() is None:
            if not STOP.exists() and shutil.disk_usage(DATA).free <= RESERVE:
                STOP.touch()
                reason = 'low_disk'
            elif STOP.exists() and reason is None:
                reason = 'manual'
            if STOP.exists() and stop_started is None:
                stop_started = time.time()
            if stop_started is not None and time.time() - stop_started > 120 and process.poll() is None:
                # A stuck camera is retained as incomplete; avoid indefinite capture.
                containers = subprocess.run(['sudo', '-n', 'docker', 'ps', '-q',
                    '--filter', 'label=com.docker.compose.project=halo',
                    '--filter', 'label=com.docker.compose.service=capture'],
                    capture_output=True, text=True).stdout.split()
                for container in containers:
                    subprocess.run(['sudo', '-n', 'docker', 'stop', '-t', '10', container],
                                   capture_output=True, timeout=25)
                process.terminate()
                reason = 'stop_timeout'
            time.sleep(1)
        code = process.wait()
    sessions = sorted((DATA / 'sessions').iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    session_id = sessions[0].name.lstrip('.').removesuffix('.partial') if sessions else None
    stage = 'failed' if code else 'processing'
    state(stage=stage, session_id=session_id, stop_reason=reason or 'time_limit',
          error=f'capture exited {code}' if code else None)
    STOP.unlink(missing_ok=True)
    raise SystemExit(code)


if __name__ == '__main__':
    main()
