"""Disposable fixture-only TLS backchannel capture; never logs requests."""

import ssl
from http.server import BaseHTTPRequestHandler, HTTPServer


class Collector(BaseHTTPRequestHandler):
    """Capture at most one bounded form in memory without logging values."""

    captured = b""

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        """Store the unchanged URL-encoded body without interpreting claims."""
        size = int(self.headers.get("Content-Length", "0"))
        if self.path != "/capture" or not 0 < size <= 32768:
            self.send_error(400)
            return
        Collector.captured = self.rfile.read(size)
        self.send_response(200)
        self.end_headers()

    def do_GET(self) -> None:
        """Return the last capture to the fixture operator over verified TLS."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-www-form-urlencoded")
        self.end_headers()
        self.wfile.write(Collector.captured)


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 8443), Collector)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain("/fixture/server.pem", "/fixture/server.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()
