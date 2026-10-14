"""Общий каркас фейковых HTTP-сервисов: stdlib-сервер в фоновом потоке.

Обработчик - функция `(method, path, query, headers, body) -> (status, json)`.
Поток один на сервис, запросы - каждый в своём потоке (`ThreadingHTTPServer`):
long polling `getUpdates` не должен держать остальные вызовы бота."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit


class JsonServer:
    def __init__(self, handle):
        self._handle = handle
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self):
                parts = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                status, payload = outer._handle(
                    self.command, parts.path, dict(parse_qsl(parts.query)), self.headers, body
                )
                # Байты - как есть (скачивание файла Bot API), остальное - JSON.
                is_raw = isinstance(payload, bytes)
                raw = payload if is_raw else json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header(
                    "Content-Type", "application/octet-stream" if is_raw else "application/json"
                )
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = _serve

            def log_message(self, *args):  # тише: журнал тестов не для этого
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        # Короткий опрос: `shutdown()` ждёт его тик, а фейк гасится в каждом тесте.
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self):
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
