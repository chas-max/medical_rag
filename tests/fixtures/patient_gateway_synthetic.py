"""TLS-only synthetic transport fixture, never an application identity proof."""

import json
import ssl
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def respond(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path == "/assets/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"data: first\n\n")
            self.wfile.flush()
            time.sleep(3)
            self.wfile.write(b"data: complete\n\n")
            self.close_connection = True
            return
        status = 200
        if self.path.startswith("/assets/status-"):
            status = int(self.path.rsplit("-", 1)[1])
        payload = json.dumps({"headers": dict(self.headers), "length": len(body), "synthetic": True}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Request-ID", "synthetic-application-id")
        self.send_header("X-Gateway-Request-ID", "forged-upstream-gateway-id")
        self.send_header("Location", "https://identity.example.org/authorize?state=fictional")
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8443), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain("/fixture/server.pem", "/fixture/server.key")
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    server.serve_forever()
