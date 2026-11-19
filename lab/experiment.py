"""Runs on the Hyper-V parent. All timing observations come from that parent."""
import argparse
import json
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from events import listen, set_state, stats, utc
from traffic import Traffic, request


def checked_thread(function, errors):
    def run():
        try:
            function()
        except Exception as error:
            errors.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    return thread


def run_one(root, output, mode, duration, repeat, interval_ms, fall, rise):
    experiment = f'{mode}-{duration}s-{repeat}'
    folder = output / experiment
    folder.mkdir()
    address = ('192.168.250.11', 8000) if mode == 'direct' else ('192.168.250.12', 8080)
    admin = ('192.168.250.12', 9999)
    stop, errors = threading.Event(), []
    traffic = Traffic(address, folder / 'requests.csv', experiment, mode, duration)
    notice = folder / 'synthetic.json'
    workers = []
    freeze = None
    set_state(admin, 'apps/app1', 'ready')

    def monitor():
        with (folder / 'health.jsonl').open('w', encoding='utf-8') as stream:
            while not stop.is_set():
                for row in stats(admin):
                    if row['pxname'] == 'apps' and row['svname'] in ('app1', 'app2'):
                        stream.write(json.dumps({'utc': utc(), 'backend': row['svname'],
                                                 'status': row['status']}) + '\n')
                stream.flush()
                stop.wait(0.1)

    try:
        deadline = time.monotonic() + 30
        while True:
            target = next(r for r in stats(admin) if r['pxname'] == 'apps' and r['svname'] == 'app1')
            if target['status'] == 'UP':
                break
            if time.monotonic() > deadline:
                raise RuntimeError('app1 did not become UP before experiment')
            time.sleep(0.2)
        workers.append(checked_thread(monitor, errors))
        workers.append(checked_thread(traffic.run, errors))
        if not traffic.ready.wait(15) or traffic.errors:
            raise RuntimeError('Baseline TCP lanes were not established')
        if 'app1' not in traffic.established:
            raise RuntimeError('No existing TCP lane was pinned to app1; run invalid')
        time.sleep(2)
        if mode == 'event-drain':
            workers.append(checked_thread(lambda: listen(stop, admin, folder / 'notices.jsonl', notice), errors))
            temporary = notice.with_suffix('.tmp')
            temporary.write_text(json.dumps({'DocumentIncarnation': 1, 'Events': [{
                'EventId': experiment, 'EventType': 'Freeze', 'ResourceType': 'VirtualMachine',
                'Resources': ['freezer-app1'], 'EventStatus': 'Scheduled',
                'NotBefore': (datetime.now(timezone.utc) + timedelta(seconds=2)).strftime('%a, %d %b %Y %H:%M:%S GMT'),
                'EventSource': 'User', 'Description': 'Synthetic nested guest freeze; not an Azure event'
            }]}), encoding='utf-8')
            temporary.replace(notice)
            deadline = time.monotonic() + 5
            while not (folder / 'notices.jsonl').exists() or '"drained"' not in (folder / 'notices.jsonl').read_text():
                if errors or time.monotonic() > deadline:
                    raise RuntimeError('Synthetic notification did not drain app1')
                time.sleep(0.05)
            time.sleep(2)  # Advance notice removes new traffic, not existing pinned sessions.
        if errors:
            raise errors[0]
        freeze = subprocess.Popen(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                   '-File', str(root / 'scripts' / 'Invoke-Freeze.ps1'),
                                   '-DurationSeconds', str(duration), '-EventsPath', str(folder / 'freeze.jsonl'),
                                   '-ExperimentId', experiment], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _, stderr = freeze.communicate(timeout=duration + 60)
        if freeze.returncode:
            raise RuntimeError('Hyper-V freeze script failed: ' + stderr.decode(errors='replace'))
        # Restore only after independently checking application readiness.
        probe = request(('192.168.250.11', 8000), experiment, mode, duration, 'recovery-probe', 0)
        if not probe['ok']:
            raise RuntimeError('app1 HTTP service did not recover after Resume-VM')
        set_state(admin, 'apps/app1', 'ready')
        time.sleep(max(5, 2 * interval_ms * rise / 1000))
    finally:
        # The PowerShell script also has finally + an independent scheduled rescue task.
        subprocess.run(['powershell.exe', '-NoProfile', '-Command',
                        "if ((Get-VM -Name freezer-app1).State -eq 'Paused') { Resume-VM -Name freezer-app1 -ErrorAction Stop }"],
                       check=True, capture_output=True)
        traffic.stop.set()
        stop.set()
        for worker in workers:
            worker.join(timeout=20)
            if worker.is_alive():
                errors.append(RuntimeError('Worker did not stop'))
        set_state(admin, 'apps/app1', 'ready')
    if errors:
        raise RuntimeError('; '.join(str(e) for e in errors))
    (folder / 'metadata.json').write_text(json.dumps({
        'experiment': experiment, 'mode': mode, 'duration_s': duration, 'validation': 'azure-hyper-v',
        'interval_ms': interval_ms, 'fall': fall, 'rise': rise,
        'new_connections_per_second': traffic.rate, 'existing_connections': traffic.persistent,
        'request_timeout_s': 12, 'deadline_s': 7, 'completed_utc': utc()
    }, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path('C:/freezer'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--durations', type=int, nargs='+', default=[2, 5, 7, 10])
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--interval-ms', type=int, default=500)
    parser.add_argument('--fall', type=int, default=2)
    parser.add_argument('--rise', type=int, default=2)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for repeat in range(1, args.repeats + 1):
        for duration in args.durations:
            for mode in ('direct', 'reactive', 'event-drain'):
                run_one(args.root, args.output, mode, duration, repeat, args.interval_ms, args.fall, args.rise)
                print(f'Completed {mode} {duration}s repeat {repeat}', flush=True)


if __name__ == '__main__':
    main()
