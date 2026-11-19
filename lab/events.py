"""Scheduled Events listener. Synthetic input is explicit and never sent to IMDS."""
import argparse
import csv
import io
import json
import socket
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def utc():
    return datetime.now(timezone.utc).isoformat()


def runtime(address, command):
    with socket.create_connection(address, timeout=2) as sock:
        sock.settimeout(2)
        sock.sendall((command + '\n').encode())
        sock.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return b''.join(chunks).decode()


def stats(address):
    return list(csv.DictReader(io.StringIO(runtime(address, 'show stat').lstrip('# '))))


def set_state(address, backend, state):
    if backend not in ('apps/app1', 'apps/app2') or state not in ('drain', 'ready'):
        raise ValueError('Only the two fixed lab backends may be changed')
    output = runtime(address, f'set server {backend} state {state}')
    if output.strip():
        raise RuntimeError(f'HAProxy rejected runtime change: {output.strip()}')
    server = backend.split('/')[1]
    row = next(r for r in stats(address) if r['pxname'] == 'apps' and r['svname'] == server)
    if state == 'drain' and not row['status'].startswith('DRAIN'):
        raise RuntimeError(f'Drain not confirmed: {row["status"]}')


def handle(document, mapping, seen, drain, emit, synthetic=False):
    if not isinstance(document, dict) or not isinstance(document.get('Events'), list):
        raise ValueError('Expected Scheduled Events object with Events array')
    for event in document['Events']:
        if event.get('EventType') != 'Freeze' or event.get('EventStatus') not in ('Scheduled', 'Started'):
            continue
        event_id = event['EventId']
        if event_id in seen:
            continue
        resources = event.get('Resources', [])
        if not isinstance(resources, list) or not all(isinstance(r, str) for r in resources):
            raise ValueError('Resources must be an array of VM instance names')
        targets = sorted({target for resource in resources for target in mapping.get(resource, [])})
        if not targets:
            continue
        emit({'kind': 'notice_received', 'event_id': event_id, 'utc': utc(), 'synthetic': synthetic})
        for target in targets:
            drain(target)
            emit({'kind': 'drained', 'backend': target, 'event_id': event_id,
                  'utc': utc(), 'synthetic': synthetic})
        seen.add(event_id)  # Retry failed commands rather than marking them handled.


def listen(stop, address, output, synthetic_file=None, poll_imds=False, interval=0.25):
    seen = set()
    # A real host event affects BOTH guests. There is no surviving independent host here.
    mapping = {'freezer-app1': ['apps/app1']} if synthetic_file else {
        'freezer-host': ['apps/app1', 'apps/app2']}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with Path(output).open('a', encoding='utf-8') as stream:
        def emit(event):
            stream.write(json.dumps(event) + '\n')
            stream.flush()
        while not stop.is_set():
            if synthetic_file and Path(synthetic_file).exists():
                document = json.loads(Path(synthetic_file).read_text(encoding='utf-8'))
                handle(document, mapping, seen, lambda b: set_state(address, b, 'drain'), emit, True)
            if poll_imds:
                request = urllib.request.Request(
                    'http://169.254.169.254/metadata/scheduledevents?api-version=2020-07-01',
                    headers={'Metadata': 'true'})
                try:
                    with opener.open(request, timeout=2) as response:
                        document = json.load(response)
                except (OSError, TimeoutError) as error:
                    emit({'kind': 'imds_error', 'utc': utc(), 'error': type(error).__name__})
                    print(f'IMDS poll failed: {type(error).__name__}', file=sys.stderr)
                else:
                    handle(document, mapping, seen, lambda b: set_state(address, b, 'drain'), emit)
            stop.wait(interval)


def main():
    import threading
    parser = argparse.ArgumentParser()
    parser.add_argument('--synthetic-file')
    parser.add_argument('--imds', action='store_true')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if bool(args.synthetic_file) == args.imds:
        parser.error('Choose exactly one of --imds or --synthetic-file')
    stop = threading.Event()
    try:
        listen(stop, ('192.168.250.12', 9999), args.output, args.synthetic_file, args.imds)
    except KeyboardInterrupt:
        stop.set()


if __name__ == '__main__':
    main()
