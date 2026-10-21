from concurrent.futures import ThreadPoolExecutor
import json
import logging
import socket
import threading
import time
import unittest

from basketnet.broker import Broker
from basketnet.client import Client
from basketnet.protocol import Connection, ProtocolError, connect
from basketnet.worker import WorkerClient

logging.disable(logging.CRITICAL)


def eventually(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("Условие не выполнено до таймаута")


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.broker = Broker(client_port=0, worker_port=0, notification_port=0, heartbeat_port=0,
                             heartbeat_timeout=2, task_timeout=3).start()
        self.client = Client(port=self.broker.client_port, notification_port=self.broker.notification_port)
        self.workers, self.raw_connections = [], []

    def tearDown(self):
        for worker, _ in self.workers:
            worker.close()
        for connection in self.raw_connections:
            connection.close()
        self.broker.close()
        for _, thread in self.workers:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive(), "Воркер не остановился")

    def worker(self, worker_id, **kwargs):
        worker = WorkerClient(port=self.broker.worker_port, worker_id=worker_id, reconnect=False, **kwargs)
        thread = threading.Thread(target=worker.run, daemon=True)
        self.workers.append((worker, thread))
        thread.start()
        eventually(lambda: any(w["worker_id"] == worker_id and w["state"] != "dead" for w in self.client.monitor()["workers"]))
        return worker

    def raw_worker(self, name="raw"):
        connection = connect("127.0.0.1", self.broker.worker_port)
        self.raw_connections.append(connection)
        connection.send({"type": "register", "protocol": 1, "worker_id": name})
        registration = connection.receive(deadline=time.monotonic() + 2)
        self.assertEqual(registration["type"], "registered")
        return connection, registration

    def receive(self, connection):
        return connection.receive(deadline=time.monotonic() + 3)

    def ack(self, connection, task, **extra):
        message = {"type": "ack", "task_id": task["task_id"], "attempt_id": task["attempt_id"], "ok": True, "result": 42}
        message.update(extra)
        connection.send(message)

    def test_priority_and_fifo_over_real_tcp(self):
        low = self.client.submit("sum", [1], 0)
        urgent1 = self.client.submit("sum", [2], 9)
        urgent2 = self.client.submit("sum", [3], 9)
        connection, _ = self.raw_worker()
        order = []
        # Подтверждения ACK и следующие задания могут чередоваться в одном соединении.
        while len(order) < 3:
            message = self.receive(connection)
            if message["type"] == "task":
                order.append(message["task_id"])
                self.ack(connection, message)
        self.assertEqual(order, [urgent1["task_id"], urgent2["task_id"], low["task_id"]])
        self.assertEqual(self.client.wait(low)["status"], "completed")

    def test_replayed_and_live_notifications_have_ordered_versions(self):
        handle = self.client.submit("sum", [5, 7])
        stream = self.client.subscribe(handle, timeout=5)
        self.assertEqual(next(stream)["status"], "queued")
        self.worker("live", delay=0.15)
        rest = list(stream)
        self.assertEqual([e["status"] for e in rest], ["running", "completed"])
        self.assertEqual([e["version"] for e in rest], [2, 3])
        self.assertEqual(rest[-1]["result"], 12)
        self.assertEqual([e["status"] for e in self.client.subscribe(handle, after_version=2)], ["completed"])

    def test_parallel_workers(self):
        self.worker("one", delay=0.5)
        self.worker("two", delay=0.5)
        handles = [self.client.submit("sum", [i, i]) for i in range(2)]
        eventually(lambda: self.client.monitor()["tasks"]["running"] == 2)
        results = [self.client.wait(handle) for handle in handles]
        self.assertEqual({e["worker_id"] for e in results}, {"one", "two"})
        self.assertEqual([e["result"] for e in results], [0, 2])

    def test_tcp_disconnect_requeues_to_other_worker(self):
        handle = self.client.submit("sum", [10, 20])
        connection, _ = self.raw_worker("broken")
        self.receive(connection)
        connection.close()
        eventually(lambda: self.client.status(handle)["status"] == "queued")
        self.worker("replacement")
        result = self.client.wait(handle)
        self.assertEqual((result["result"], result["attempt"], result["worker_id"]), (30, 2, "replacement"))

    def test_udp_timeout_despite_live_tcp(self):
        self.broker.heartbeat_timeout = 0.35
        self.broker.task_timeout = 4
        handle = self.client.submit("sum", [7])
        connection, registration = self.raw_worker("no-pulse")
        self.receive(connection)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.sendto(b"invalid-json", ("127.0.0.1", self.broker.heartbeat_port))
            udp.sendto(json.dumps({"type": "heartbeat", "worker_id": "no-pulse", "session_token": "wrong"}).encode(),
                       ("127.0.0.1", self.broker.heartbeat_port))
        eventually(lambda: self.client.status(handle)["status"] == "queued")
        self.assertIn("UDP heartbeat", self.client.monitor()["workers"][0]["reason"])
        self.worker("healthy")
        self.assertEqual(self.client.wait(handle)["attempt"], 2)

    def test_task_timeout_with_continuing_heartbeat(self):
        self.broker.task_timeout = 0.3
        self.broker.heartbeat_timeout = 1
        handle = self.client.submit("sum", [8])
        self.worker("slow", delay=2)
        eventually(lambda: self.client.status(handle)["status"] == "queued")
        self.assertIn("ACK", self.client.monitor()["workers"][0]["reason"])
        self.worker("fast")
        result = self.client.wait(handle)
        self.assertEqual((result["result"], result["attempt"]), (8, 2))

    def test_retry_waits_for_a_different_node(self):
        handle = self.client.submit("sum", [13], priority=9)
        connection, _ = self.raw_worker("original")
        self.receive(connection)
        connection.close()
        eventually(lambda: self.client.status(handle)["status"] == "queued")
        self.worker("original")
        other_task = self.client.submit("sum", [2], priority=0)
        self.assertEqual(self.client.wait(other_task)["result"], 2)
        self.assertEqual(self.client.status(handle)["status"], "queued")
        self.worker("other-node")
        result = self.client.wait(handle)
        self.assertEqual((result["result"], result["worker_id"], result["attempt"]), (13, "other-node", 2))

    def test_retry_limit_finishes_with_error(self):
        self.broker.max_attempts = 1
        self.broker.task_timeout = 0.2
        handle = self.client.submit("fib", 100)
        connection, _ = self.raw_worker()
        self.receive(connection)
        result = self.client.wait(handle)
        self.assertEqual(result["status"], "error")
        self.assertIn("Исчерпаны попытки", result["error"])

    def test_stale_and_duplicate_acks_do_not_overwrite_result(self):
        handle = self.client.submit("sum", [42])
        connection, _ = self.raw_worker()
        task = self.receive(connection)
        self.ack(connection, task, attempt_id="old-attempt", result=-1)
        self.assertFalse(self.receive(connection)["accepted"])
        self.assertEqual(self.client.status(handle)["status"], "running")
        self.ack(connection, task)
        self.assertTrue(self.receive(connection)["accepted"])
        self.ack(connection, task, result=-99)
        self.assertFalse(self.receive(connection)["accepted"])
        self.assertEqual(self.client.status(handle)["result"], 42)

    def test_worker_error_is_final(self):
        handle = self.client.submit("sum", [])
        connection, _ = self.raw_worker()
        task = self.receive(connection)
        self.ack(connection, task, ok=False, error="Вычисление не удалось")
        result = self.client.wait(handle)
        self.assertEqual((result["status"], result["attempt"]), ("error", 1))
        self.assertEqual(result["error"], "Вычисление не удалось")

    def test_invalid_input_and_wrong_token(self):
        with self.assertRaises(ValueError):
            self.client.submit("factorial", -1)
        with self.assertRaises(ValueError):
            self.client.submit("sum", [1], 10)
        handle = self.client.submit("sum", [1])
        forged = dict(handle, task_token="bad")
        with self.assertRaises(ValueError):
            self.client.status(forged)
        with self.assertRaises(ValueError):
            list(self.client.subscribe(forged, timeout=2))

    def test_many_clients(self):
        self.worker("batch-1")
        self.worker("batch-2")
        def request(i):
            handle = self.client.submit("sum", [i, 1], priority=i % 10)
            return self.client.wait(handle)["result"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(list(pool.map(request, range(24))), list(range(1, 25)))

    def test_duplicate_worker_id_rejected(self):
        self.raw_worker("same")
        duplicate = connect("127.0.0.1", self.broker.worker_port)
        self.raw_connections.append(duplicate)
        duplicate.send({"type": "register", "protocol": 1, "worker_id": "same"})
        self.assertEqual(self.receive(duplicate)["type"], "error")
        self.assertEqual(self.client.monitor()["workers"][0]["state"], "idle")

    def test_client_wait_times_out_while_task_stays_queued(self):
        handle = self.client.submit("sum", [])
        with self.assertRaises(TimeoutError):
            self.client.wait(handle, timeout=0.1)
        self.assertEqual(self.client.status(handle)["status"], "queued")

    def test_retention_removes_completed_tasks(self):
        self.broker.retention = 0.15
        self.worker("retention")
        handle = self.client.submit("sum", [1])
        self.client.wait(handle)
        eventually(lambda: self.client.monitor()["tasks"]["completed"] == 0)
        with self.assertRaises(ValueError):
            self.client.status(handle)


class ProtocolTests(unittest.TestCase):
    def test_fragmented_and_combined_messages(self):
        left, right = socket.socketpair()
        connection = Connection(left)
        try:
            right.sendall(b'{"type":"a"')
            right.sendall(b'}\n{"type":"b"}\n')
            self.assertEqual(connection.receive()["type"], "a")
            self.assertEqual(connection.receive()["type"], "b")
        finally:
            connection.close()
            right.close()

    def test_non_object_and_nonfinite_json_rejected(self):
        for payload in (b"[]\n", b'{"data":NaN}\n', b"not-json\n"):
            left, right = socket.socketpair()
            connection = Connection(left)
            try:
                right.sendall(payload)
                with self.assertRaises(ProtocolError):
                    connection.receive()
            finally:
                connection.close()
                right.close()


if __name__ == "__main__":
    unittest.main()
