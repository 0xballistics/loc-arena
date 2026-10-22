"""A stand-in for OpenRouter, run in a container beside the model gateway: it replies with what it received.

Each reply's content is a JSON echo of the request's path, the SHA-256 of its ``Authorization`` header and its
body. A model ending in ``-slow`` streams one chunk a second for 30 seconds; ``"length": n`` pads the reply to
``n`` bytes. Standard library only: it runs as ``python -u _mock_upstream.py`` in any Python image.
"""

import hashlib
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 9000


class Handler(BaseHTTPRequestHandler):
    """Echo each request back as a chat completion, whole or streamed."""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        authorization = hashlib.sha256(self.headers.get("authorization", "").encode()).hexdigest()
        content = json.dumps({"path": self.path, "authorization_sha256": authorization, "body": request})
        content += "x" * max(0, int(request.get("length", 0)) - len(content))
        if not request.get("stream"):
            body = json.dumps({"model": request.get("model"), "choices": [{"message": {"content": content}}]})
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body.encode())))
            self.end_headers()
            self.wfile.write(body.encode())
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        slow = str(request.get("model", "")).endswith("-slow")
        pieces = [f"tick {index} " for index in range(30)] if slow else [content]
        for piece in pieces:
            chunk = json.dumps({"choices": [{"delta": {"content": piece}}]})
            self.send_chunk(f"data: {chunk}\n\n".encode())
            time.sleep(1 if slow else 0)
        self.send_chunk(b"data: [DONE]\n\n")
        self.send_chunk(b"")

    def send_chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
