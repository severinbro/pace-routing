#!/usr/bin/env python3
"""HALO local control entrypoint. Exposed to SENSE only through a forced SSH command."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
SESSIONS = DATA / 'sessions'
STATE = DATA / 'ui-state.json'
STOP = DATA / '.ui_stop'
UNIT = 'halo-ui-record'
MIN_START = 5 * 1024**3
MIN_RUNNING = 4 * 1024**3
SESSION_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*\Z')


def run(*args, timeout=12):
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout)


def active():
    return run('sudo', '-n', 'systemctl', 'is-active', UNIT).stdout.strip() in ('active', 'activating')


def load_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def atomic_json(path, value):
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(json.dumps(value) + '\n')
    tmp.replace(path)


def probe_i2c(address):
    try:
        with open('/dev/i2c-1', 'rb', buffering=0) as bus:
            fcntl.ioctl(bus.fileno(), 0x0703, address)
            return bool(bus.read(1))
    except OSError:
        return False


def readiness():
    config = {}
    for line in (ROOT / 'config/capture.env').read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            config[key] = value
    cameras = run('rpicam-hello', '--list-cameras', timeout=15)
    camera_count = len(re.findall(r'^\s*\d+\s*:', cameras.stdout + cameras.stderr, re.M))
    thermal = config.get('THERMAL_DEVICE', '')
    free = shutil.disk_usage(DATA).free
    checks = {
        'stereo_cameras': {'ok': camera_count >= 2, 'detail': f'{camera_count} of 2 detected'},
        'imu': {'ok': config.get('IMU_ENABLED') == 'true' and probe_i2c(int(config.get('IMU_ADDRESS', '0x4a'), 0)), 'detail': 'BNO085 I²C probe'},
        'light': {'ok': config.get('LIGHT_ENABLED') == 'true' and probe_i2c(int(config.get('LIGHT_ADDRESS', '0x23'), 0)), 'detail': 'BH1750 I²C probe'},
        'thermal': {'ok': config.get('THERMAL_ENABLED') == 'true' and bool(thermal) and Path(thermal).exists(), 'detail': thermal or 'not configured'},
        'time_sync': {'ok': run('timedatectl', 'show', '-p', 'NTPSynchronized', '--value').stdout.strip() == 'yes', 'detail': 'SENSE NTP'},
        'disk': {'ok': free >= MIN_START, 'detail': f'{free / 1024**3:.1f} GiB free; 5 GiB required'},
    }
    return {'ready': all(v['ok'] for v in checks.values()), 'checks': checks,
            'free_bytes': free, 'stop_reserve_bytes': MIN_RUNNING}


def session_info(path):
    report = load_json(path / 'sync_report.json')
    capture = load_json(path / 'capture.json')
    if path.name.startswith('.') and path.name.endswith('.partial'):
        stage = 'incomplete'
    elif (path / '.transfer_complete').exists():
        stage = 'transferred'
    elif (path / '.transfer_pending').exists():
        stage = 'uploading'
    elif (path / '.extraction_complete').exists():
        stage = 'extracted'
    elif (path / '.capture_complete').exists():
        stage = 'extracting'
    else:
        stage = 'recording'
    return {'id': path.name.lstrip('.').removesuffix('.partial'), 'stage': stage,
            'sync': report, 'capture': capture,
            'raw_video_present': any((path / 'video').glob('*.mkv')) if (path / 'video').exists() else False,
            'images_on_halo': any((path / 'frames').rglob('*')) if (path / 'frames').exists() else False,
            'transfer_receipt': load_json(path / '.transfer_complete')}


def sessions():
    return [session_info(p) for p in sorted(SESSIONS.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True) if p.is_dir()][:50]


def status():
    state = load_json(STATE)
    running = active()
    items = sessions()
    if running:
        partials = [x for x in items if x['stage'] == 'incomplete']
        stage = 'finalizing' if STOP.exists() else 'recording'
        if partials:
            state['session_id'] = partials[0]['id']
    else:
        stage = state.get('stage', 'idle')
        if stage in ('recording', 'starting', 'finalizing'):
            stage = 'failed'
        elif stage == 'processing' and items:
            stage = items[0]['stage']
    return {'online': True, 'recording_active': running, 'stage': stage,
            'session_id': state.get('session_id'), 'stop_reason': state.get('stop_reason'),
            'error': state.get('error'), 'readiness': readiness(), 'sessions': items}


def start():
    with (DATA / '.ui_control.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if active():
            return {'error': 'Recording already active', 'status': status()}
        ready = readiness()
        if not ready['ready']:
            return {'error': 'HALO is not ready', 'status': status()}
        STOP.unlink(missing_ok=True)
        atomic_json(STATE, {'stage': 'starting', 'started_at': time.time()})
        command = ['sudo', '-n', 'systemd-run', '--unit=' + UNIT, '--collect',
                   '--property=WorkingDirectory=' + str(ROOT), '--uid=Pi_Mz',
                   '/usr/bin/python3', str(ROOT / 'control/runner.py')]
        result = run(*command, timeout=20)
        if result.returncode:
            atomic_json(STATE, {'stage': 'failed', 'error': result.stderr.strip()[:300]})
            return {'error': result.stderr.strip()[:300], 'status': status()}
        return {'status': status()}


def stop():
    if active():
        STOP.touch()
    return {'status': status()}


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def cleanup(session_id, digest, manifest_hash):
    if (not SESSION_RE.fullmatch(session_id) or not re.fullmatch(r'[a-f0-9]{64}', digest)
            or not re.fullmatch(r'[a-f0-9]{64}', manifest_hash)):
        raise ValueError('invalid receipt parameters')
    folder = SESSIONS / session_id
    if not folder.is_dir() or folder.is_symlink():
        raise ValueError('session missing')
    receipt = load_json(folder / '.transfer_complete')
    if receipt and (receipt.get('session_id') != session_id or receipt.get('archive_sha256') != digest):
        raise ValueError('local receipt does not match SENSE receipt')
    if not (folder / '.extraction_complete').exists() or not (folder / 'sync_report.json').is_file():
        raise ValueError('processed session is incomplete')
    verified_marker = folder / '.sense_copy_verified'
    previously_verified = load_json(verified_marker)
    matched_marker = (previously_verified.get('session_id') == session_id
                      and previously_verified.get('archive_sha256') == digest
                      and previously_verified.get('manifest_sha256') == manifest_hash)
    if not matched_marker:
        sys.path.insert(0, str(ROOT / 'capture/bin'))
        from upload_sessions import build_transfer_manifest, transfer_files
        # Match every processed byte against the receiver's verified manifest.
        expected_manifest = build_transfer_manifest(folder, transfer_files(folder, include_video=False))
        if hashlib.sha256(expected_manifest).hexdigest() != manifest_hash:
            raise ValueError('processed files differ from SENSE')
    manifest = folder / 'SHA256SUMS'
    if not manifest.is_file():
        raise ValueError('checksum manifest missing')
    retained = []
    video_entries = []
    for line in manifest.read_text().splitlines():
        expected, relative = line.split('  ', 1)
        target = folder / relative
        if target.resolve().parent != folder.resolve() and not target.resolve().is_relative_to(folder.resolve()):
            raise ValueError('unsafe manifest path')
        if not target.is_file() or target.is_symlink() or sha256(target) != expected:
            raise ValueError('local checksum verification failed: ' + relative)
        if relative in ('video/left.mkv', 'video/right.mkv'):
            video_entries.append(target)
        elif not relative.startswith('frames/'):
            retained.append(line)
    if video_entries and len(video_entries) != 2:
        raise ValueError('both videos must be present')
    # Replace the manifest first. A retry can then remove leftover video files.
    temporary = folder / '.SHA256SUMS.cleanup'
    temporary.write_text('\n'.join(retained) + '\n')
    temporary.replace(manifest)
    atomic_json(verified_marker, {'session_id': session_id, 'archive_sha256': digest,
                                  'manifest_sha256': manifest_hash})
    video_dir = folder / 'video'
    for side in ('left', 'right'):
        video = video_dir / f'{side}.mkv'
        if video.is_file() and video not in video_entries:
            video_entries.append(video)
    for video in video_entries:
        video.unlink()
    frames = folder / 'frames'
    had_images = frames.exists()
    if frames.exists():
        shutil.rmtree(frames)
    if not receipt:
        atomic_json(folder / '.transfer_complete', {'state': 'committed', 'session_id': session_id,
                    'archive_sha256': digest, 'confirmed_by': 'SENSE dashboard manifest comparison'})
    return {'cleaned': bool(video_entries) or had_images, 'session_id': session_id}


def main():
    # A forced SSH command passes one of these exact verbs; never evaluate a shell fragment.
    args = sys.argv[1:]
    if args == ['status']:
        result = status()
    elif args == ['sessions']:
        result = {'sessions': sessions()}
    elif args == ['start']:
        result = start()
    elif args == ['stop']:
        result = stop()
    elif len(args) == 4 and args[0] == 'cleanup':
        result = cleanup(args[1], args[2], args[3])
    else:
        raise ValueError('unsupported operation')
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': str(exc)}))
        raise SystemExit(1)
