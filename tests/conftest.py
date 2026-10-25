"""测试公共设施。"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from meeting_translate import stats  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_stats(tmp_path, monkeypatch):
    """把统计文件指到临时目录，避免测试污染 %APPDATA%\\meeting-translate。"""
    monkeypatch.setattr(stats, "STATS_FILE", tmp_path / "model_stats.json", raising=False)
    monkeypatch.setattr(stats, "_stats", stats.ModelStats())


REPLY = "你好"


class _OpenAIHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _json(self, status: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path.endswith("/models"):
            data = getattr(self.server, "model_data", None) or {
                "data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]
            }
            self._json(200, data)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n) or b"{}")
        srv = self.server
        srv.requests.append({"path": self.path, "body": payload, "auth": self.headers.get("Authorization")})
        if srv.responses:
            status, body = srv.responses.pop(0)
        else:
            status, body = 200, {
                "choices": [{"message": {"content": srv.reply}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            }
        self._json(status, body)


@pytest.fixture
def openai_stub():
    """本地假 OpenAI 兼容端点：记录请求，按队列决定响应。"""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIHandler)
    srv.requests = []
    srv.responses = []
    srv.reply = REPLY
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
