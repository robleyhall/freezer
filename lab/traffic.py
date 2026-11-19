"""Bounded concurrent new connections plus explicitly pre-existing TCP lanes."""
import argparse
import csv
import http.client
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from events import utc

FIELDS = ['experiment', 'mode', 'duration_s', 'lane', 'sequence', 'start_utc', 'end_utc',
          'start_mono', 'latency_ms', 'status', 'ok', 'backend', 'connection_id',
          'error', 'deadline_exceeded', 'scheduler_lag_ms', 'reconnected']


def request(address, experiment, mode, duration, lane, sequence, connection=None,
            timeout=12.0, scheduled=None, path='/work?delay=0.05'):
    start = time.perf_counter()
    row = dict.fromkeys(FIELDS, '')
    row.update(experiment=experiment, mode=mode, duration_s=duration, lane=lane,
               sequence=sequence, start_utc=utc(), start_mono=start, ok=False,
               deadline_exceeded=False, reconnected=False,
               scheduler_lag_ms=round(max(0, start - (scheduled or start)) * 1000, 3))
    own = connection is None
    connection = connection or http.client.HTTPConnection(*address, timeout=timeout)
    try:
        connection.request('GET', path)
        response = connection.getresponse()
        payload = json.loads(response.read())
        row.update(status=response.status, ok=response.status == 200,
                   backend=payload.get('backend', ''), connection_id=payload.get('connection_id', ''))
    except (OSError, TimeoutError, http.client.HTTPException, ValueError) as error:
        row['error'] = type(error).__name__  # No environment-specific endpoints in artifacts.
        connection.close()
    finally:
        row.update(end_utc=utc(), latency_ms=round((time.perf_counter() - start) * 1000, 3))
        row['deadline_exceeded'] = row['latency_ms'] >= 7000
        if own:
            connection.close()
    return row


class Traffic:
    def __init__(self, address, output, experiment, mode, duration, rate=5, persistent=4):
        self.address, self.output = address, Path(output)
        self.experiment, self.mode, self.duration = experiment, mode, duration
        self.rate, self.persistent = rate, persistent
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.rows = []
        self.errors = []
        self.established = []

    def record(self, row):
        with self.lock:
            self.rows.append(row)

    def existing(self, number):
        connection = http.client.HTTPConnection(*self.address, timeout=12)
        connection_id = None
        try:
            for sequence in range(100000):
                if self.stop.is_set():
                    break
                row = request(self.address, self.experiment, self.mode, self.duration,
                              f'existing-{number}', sequence, connection)
                if sequence == 0:
                    if not row['ok']:
                        raise RuntimeError('Could not establish baseline TCP connection')
                    connection_id = row['connection_id']
                    with self.lock:
                        self.established.append(row['backend'])
                        if len(self.established) == self.persistent:
                            self.ready.set()
                elif row['connection_id'] and row['connection_id'] != connection_id:
                    row['reconnected'] = True
                self.record(row)
                # Do not silently reconnect an "existing TCP" lane after failure.
                if not row['ok']:
                    break
                self.stop.wait(0.1)
        except Exception as error:
            self.errors.append(error)
            self.ready.set()
        finally:
            connection.close()

    def run(self):
        # 5 new requests/s * 12 s timeout can leave 60 requests outstanding.
        with ThreadPoolExecutor(max_workers=96) as pool:
            lanes = [pool.submit(self.existing, n) for n in range(self.persistent)]
            tick, sequence = time.perf_counter(), 0
            pending = set()
            while not self.stop.is_set():
                pending = {f for f in pending if not f.done()}
                if len(pending) >= 80:
                    self.errors.append(RuntimeError('Traffic worker capacity exceeded; run invalid'))
                    self.stop.set()
                    break
                future = pool.submit(request, self.address, self.experiment, self.mode,
                                     self.duration, 'new', sequence, None, 12, tick)
                future.add_done_callback(lambda f: self.record(f.result()))
                pending.add(future)
                sequence += 1
                tick += 1 / self.rate
                self.stop.wait(max(0, tick - time.perf_counter()))
            for lane in lanes:
                lane.result()
        with self.output.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(sorted(self.rows, key=lambda row: row['start_mono']))
        if self.errors:
            raise RuntimeError('; '.join(str(e) for e in self.errors))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='192.168.250.12')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--seconds', type=float, default=60)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    traffic = Traffic((args.host, args.port), args.output, 'manual', 'manual', 0)
    timer = threading.Timer(args.seconds, traffic.stop.set)
    timer.start()
    try:
        traffic.run()
    finally:
        traffic.stop.set()
        timer.cancel()


if __name__ == '__main__':
    main()
