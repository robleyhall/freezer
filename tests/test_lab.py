import csv
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lab'))
from events import handle, set_state
from report import generate, summarize
from service import Handler
from traffic import request, Traffic


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.daemon_threads = True
        cls.server.backend = 'app1'
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_existing_lane_keeps_connection_identity(self):
        import http.client
        address = self.server.server_address
        connection = http.client.HTTPConnection(*address)
        try:
            a = request(address, 'test', 'direct', 2, 'existing-0', 0, connection)
            b = request(address, 'test', 'direct', 2, 'existing-0', 1, connection)
        finally:
            connection.close()
        self.assertTrue(a['ok'] and b['ok'])
        self.assertEqual(a['connection_id'], b['connection_id'])

    def test_timeout_is_failure_not_deadline_success(self):
        row = request(self.server.server_address, 'test', 'direct', 2, 'new', 0,
                      timeout=.02, path='/work?delay=0.1')
        self.assertFalse(row['ok'])
        self.assertEqual(row['error'], 'TimeoutError')
        self.assertFalse(row['deadline_exceeded'])

    def test_continuous_generator_writes_both_lanes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'requests.csv'
            traffic = Traffic(self.server.server_address, path, 'test', 'direct', 2, rate=10, persistent=1)
            thread = threading.Thread(target=traffic.run)
            thread.start()
            self.assertTrue(traffic.ready.wait(3))
            traffic.stop.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            with path.open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual({row['lane'] for row in rows}, {'new', 'existing-0'})


class EventsTests(unittest.TestCase):
    def event(self, resources=None):
        return {'Events': [{'EventId': 'synthetic-1', 'EventType': 'Freeze',
                            'EventStatus': 'Scheduled', 'Resources': resources or ['freezer-app1']}]}

    def test_scoped_drain_is_deduplicated(self):
        seen, drained, emitted = set(), [], []
        document = self.event(['freezer-app1', 'unrelated'])
        for _ in range(2):
            handle(document, {'freezer-app1': ['apps/app1']}, seen, drained.append, emitted.append, True)
        self.assertEqual(drained, ['apps/app1'])
        self.assertTrue(all(row['synthetic'] for row in emitted))

    def test_failed_drain_is_retried(self):
        seen = set()
        def fail(_):
            raise RuntimeError('unavailable')
        with self.assertRaises(RuntimeError):
            handle(self.event(), {'freezer-app1': ['apps/app1']}, seen, fail, lambda _: None)
        self.assertEqual(seen, set())

    def test_unrelated_event_does_not_change_backends(self):
        drained = []
        handle(self.event(['unrelated']), {'freezer-app1': ['apps/app1']}, set(), drained.append, lambda _: None)
        self.assertEqual(drained, [])

    def test_runtime_rejection_is_not_success(self):
        with patch('events.runtime', return_value='No such server'):
            with self.assertRaisesRegex(RuntimeError, 'rejected'):
                set_state(('127.0.0.1', 9999), 'apps/app1', 'drain')


class ReportTests(unittest.TestCase):
    def make_case(self, directory):
        folder = Path(directory) / 'direct-7s-1'
        folder.mkdir()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        stamp = lambda seconds: (base + timedelta(seconds=seconds)).isoformat()
        (folder / 'metadata.json').write_text(json.dumps({
            'experiment': 'direct-7s-1', 'mode': 'direct', 'duration_s': 7, 'validation': 'unit-test'}))
        (folder / 'freeze.jsonl').write_text('\n'.join(json.dumps({'kind': kind, 'utc': stamp(second)})
                                                    for kind, second in [('paused', 0), ('resumed', 7)]))
        rows = [dict(lane='new', start_utc=stamp(.1), end_utc=stamp(.2), latency_ms=100,
                     scheduler_lag_ms=0, ok=False, backend='', deadline_exceeded=False),
                dict(lane='existing-0', start_utc=stamp(-.1), end_utc=stamp(7.1), latency_ms=7200,
                     scheduler_lag_ms=0, ok=True, backend='app1', deadline_exceeded=True)]
        with (folder / 'requests.csv').open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        return folder

    def test_fast_failure_and_slow_success_both_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = self.make_case(directory)
            rows, _ = summarize(folder)
            self.assertTrue(all(not row['pass_observed'] for row in rows))
            self.assertEqual(rows[0]['failed_requests'], 1)
            self.assertEqual(rows[1]['deadline_exceeded'], 1)
            self.assertIsNone(rows[0]['detect_unhealthy_ms'])
            generate(directory)
            self.assertIn('INCOMPLETE', (Path(directory) / 'report.md').read_text())

    def test_missing_pause_is_not_zero_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = self.make_case(directory)
            (folder / 'freeze.jsonl').write_text('')
            with self.assertRaisesRegex(ValueError, 'confirmed'):
                summarize(folder)


class ControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('controller', ROOT / 'tools/freezer.py')
        cls.controller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.controller)

    def test_guest_script_failure_is_not_azure_success(self):
        with patch.object(self.controller, 'az', return_value={'value': [{'message': '[stdout]\nfailure'}]}):
            with self.assertRaisesRegex(RuntimeError, 'Remote command failed'):
                self.controller.host({'subscription': 'test', 'resource_group': 'freezer-lab'}, 'throw')

    def test_powershell_quotes_are_escaped(self):
        self.assertEqual(self.controller.ps_quote("it's"), "'it''s'")


if __name__ == '__main__':
    unittest.main()
