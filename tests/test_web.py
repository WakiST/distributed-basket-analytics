"""Проверки HTTP-клиента с настоящим брокером, воркером и каналом событий."""

import http.client
import json
import threading
import time
import unittest

from basketnet.broker import Broker
from basketnet.web import WebServer, browser_json
from basketnet.worker import WorkerClient
from tests.test_network import eventually


class WebTests(unittest.TestCase):
    def setUp(self):
        self.broker = Broker(client_port=0, worker_port=0, notification_port=0, heartbeat_port=0).start()
        self.web = WebServer(port=0, broker_port=self.broker.client_port, notification_port=self.broker.notification_port,
                             database=":memory:", admin_password="test-password")
        self.thread = threading.Thread(target=self.web.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.workers, self.connections = [], []
        self.cookie = self.new_session()
        status, _, headers = self.request("POST", "/api/auth/login", {"login": "admin", "password": "test-password"})
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";")[0]

    def tearDown(self):
        for connection in self.connections:
            connection.close()
        for worker, _ in self.workers:
            worker.close()
        self.web.shutdown()
        self.web.server_close()
        self.broker.close()
        self.thread.join(timeout=3)
        for _, thread in self.workers:
            thread.join(timeout=3)

    def request(self, method, path, data=None, cookie=None, extra=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.web.server_port, timeout=5)
        headers = {"Cookie": cookie or getattr(self, "cookie", "")}
        if data is not None:
            headers.update({"Content-Type": "application/json", "X-BasketNet-Client": "browser"})
        if extra:
            headers.update(extra)
        try:
            body = json.dumps(data).encode() if data is not None else None
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            content = response.read()
            result = json.loads(content) if "application/json" in response.getheader("Content-Type", "") else content
            return response.status, result, dict(response.getheaders())
        finally:
            connection.close()

    def new_session(self):
        status, body, headers = self.request("GET", "/api/session", cookie="other=none")
        self.assertEqual(status, 200)
        self.assertIsNone(body["user"])
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        return headers["Set-Cookie"].split(";")[0]

    def worker(self):
        worker = WorkerClient(port=self.broker.worker_port, worker_id="web-worker", reconnect=False)
        thread = threading.Thread(target=worker.run, daemon=True)
        self.workers.append((worker, thread))
        thread.start()

    def submit(self, operation="sum", data=None):
        status, record, _ = self.request("POST", "/api/tasks", {"operation": operation, "data": data if data is not None else [3, 4], "priority": 9})
        self.assertEqual(status, 201, record)
        return record["snapshot"]["task_id"]

    def test_static_site_and_no_path_traversal(self):
        status, content, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"BasketNet", content)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        for path in ("/app.js", "/style.css", "/favicon.svg"):
            self.assertEqual(self.request("GET", path)[0], 200)
        self.assertEqual(self.request("GET", "/../broker.py")[0], 404)

    def test_one_worker_result_and_reload(self):
        self.worker()
        task_id = self.submit()
        record = eventually(lambda: (value if (value := self.request("GET", f"/api/tasks/{task_id}")[1])["snapshot"]["status"] == "completed" else None))
        self.assertEqual(record["snapshot"]["result"], 7)
        self.assertEqual([event["status"] for event in record["history"]], ["queued", "running", "completed"])
        status, restored, _ = self.request("GET", "/api/session")
        self.assertEqual(status, 200)
        self.assertEqual(restored["tasks"][0]["snapshot"]["result"], 7)
        status, handle, _ = self.request("GET", f"/api/tasks/{task_id}/handle")
        self.assertEqual(status, 200)
        self.assertIn("task_token", handle)

    def test_sessions_cannot_read_each_others_results(self):
        task_id = self.submit()
        other = self.new_session()
        status, _, headers = self.request("POST", "/api/auth/login", {"login": "admin", "password": "test-password"}, cookie=other)
        self.assertEqual(status, 200)
        other = headers["Set-Cookie"].split(";")[0]
        self.assertEqual(self.request("GET", f"/api/tasks/{task_id}", cookie=other)[0], 404)
        self.assertEqual(self.request("GET", f"/api/tasks/{task_id}/handle", cookie=other)[0], 404)
        self.assertEqual(self.request("GET", "/api/session", cookie=other)[1]["tasks"], [])

    def test_validation_and_cross_origin_rejected(self):
        status, _, _ = self.request("POST", "/api/tasks", {"operation": "factorial", "data": -1})
        self.assertEqual(status, 400)
        status, _, _ = self.request("POST", "/api/tasks", {"operation": "sum", "data": []}, extra={"Origin": "http://another-host.invalid"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/tasks", {"operation": "sum", "data": []}, cookie="unknown=none")
        self.assertEqual(status, 401)
        self.assertEqual(self.broker.monitor()["tasks"]["queued"], 0)

    def test_live_notifications_and_reconnect_snapshot(self):
        task_id = self.submit()
        connection = http.client.HTTPConnection("127.0.0.1", self.web.server_port, timeout=5)
        self.connections.append(connection)
        connection.request("GET", "/api/events", headers={"Cookie": self.cookie})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        self.worker()
        received = []
        while True:
            line = response.readline().decode("utf-8")
            self.assertTrue(line, "Поток событий закрылся до результата")
            if not line.startswith("data: "):
                continue
            payload = json.loads(line[6:])
            records = payload if isinstance(payload, list) else [payload]
            received.extend(record["snapshot"]["status"] for record in records)
            if "completed" in received:
                break
        self.assertIn("queued", received)
        self.assertIn("running", received)
        self.assertEqual(received[-1], "completed")
        response.close()
        connection.close()
        restored = self.request("GET", "/api/session")[1]
        self.assertEqual(restored["tasks"][0]["snapshot"]["task_id"], task_id)

    def test_broker_offline_returns_service_unavailable(self):
        self.broker.close()
        self.assertEqual(self.request("GET", "/api/monitor")[0], 503)
        self.assertEqual(self.request("POST", "/api/tasks", {"operation": "sum", "data": []})[0], 503)
        self.assertEqual(self.request("GET", "/")[0], 200)

    def test_large_integer_results_are_exact_in_browser(self):
        self.assertEqual(json.loads(browser_json({"result": 10000000000000001}))["result"], "10000000000000001")
        self.worker()
        value = 999999999999
        task_id = self.submit(data=[value] * 10000)
        record = eventually(lambda: (value if (value := self.request("GET", f"/api/tasks/{task_id}")[1])["snapshot"]["status"] == "completed" else None))
        self.assertEqual(record["snapshot"]["result"], str(value * 10000))


if __name__ == "__main__":
    unittest.main()
