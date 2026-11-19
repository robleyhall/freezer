"""Deliberately small HTTP/1.1 service with a visible backend and connection ID."""
import argparse
import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection_id = uuid.uuid4().hex

    def do_GET(self):
        query = parse_qs(urlsplit(self.path).query)
        delay = min(3.0, max(0.0, float(query.get('delay', ['0'])[0])))
        if self.path.startswith('/work'):
            time.sleep(delay)
        body = json.dumps({'backend': self.server.backend,
                           'connection_id': self.connection_id}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # The load generator intentionally abandons timed-out requests.

    def log_message(self, *_):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bind', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--backend', required=True)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.daemon_threads = True
    server.backend = args.backend
    server.serve_forever()


if __name__ == '__main__':
    main()
