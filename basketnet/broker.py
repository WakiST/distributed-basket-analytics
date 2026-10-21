"""Многопоточный брокер: приоритетная очередь, таймауты задач и контроль узлов по UDP."""

import argparse
from collections import Counter
from dataclasses import dataclass, field
import heapq
import hmac
import json
import logging
import queue
import secrets
import socket
import threading
import time
import uuid

from .protocol import Connection, ProtocolError, encode
from .tasks import validate

LOG = logging.getLogger("broker")
FINAL = {"completed", "error"}


@dataclass
class Task:
    id: str
    token: str
    operation: str
    data: object
    priority: int
    sequence: int
    created_at: float = field(default_factory=time.time)
    status: str = "queued"
    attempt: int = 0
    attempt_id: str | None = None
    worker_id: str | None = None
    deadline: float = 0
    version: int = 0
    result: object = None
    error: str | None = None
    finished_at: float | None = None
    last_failed_worker: str | None = None
    events: list = field(default_factory=list)
    subscribers: set = field(default_factory=set)


@dataclass
class Worker:
    id: str
    token: str
    connection: Connection
    address: str
    last_seen: float = field(default_factory=time.monotonic)
    alive: bool = True
    task_id: str | None = None
    completed: int = 0
    reason: str | None = None


class Broker:
    def __init__(self, host="127.0.0.1", client_port=9000, worker_port=9001,
                 notification_port=9002, heartbeat_port=9003, heartbeat_timeout=5,
                 task_timeout=30, max_attempts=3, retention=3600, max_tasks=10000):
        if heartbeat_timeout <= 0 or task_timeout <= 0 or retention <= 0:
            raise ValueError("Таймауты и время хранения должны быть положительными")
        if not 1 <= max_attempts <= 20 or max_tasks <= 0:
            raise ValueError("max_attempts: 1-20; max_tasks: положительное число")
        self.host = host
        self.client_port, self.worker_port = client_port, worker_port
        self.notification_port, self.heartbeat_port = notification_port, heartbeat_port
        self.heartbeat_timeout, self.task_timeout = heartbeat_timeout, task_timeout
        self.max_attempts, self.retention, self.max_tasks = max_attempts, retention, max_tasks
        self.condition = threading.Condition(threading.RLock())
        self.stop_event = threading.Event()
        self.tasks, self.workers = {}, {}
        self.pending = []
        self.sequence = 0
        self.threads, self.sockets, self.connections = [], [], set()
        self.connection_slots = threading.BoundedSemaphore(256)

    def _spawn(self, target, *args):
        thread = threading.Thread(target=target, args=args, daemon=True)
        with self.condition:
            self.threads = [t for t in self.threads if t.is_alive()]
            self.threads.append(thread)
        thread.start()

    def start(self):
        try:
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sockets.append(udp)
            udp.bind((self.host, self.heartbeat_port))
            udp.settimeout(0.5)
            self.heartbeat_port = udp.getsockname()[1]
            listeners = []
            for attribute, handler in (("client_port", self._client), ("worker_port", self._worker),
                                       ("notification_port", self._subscription)):
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sockets.append(sock)
                sock.bind((self.host, getattr(self, attribute)))
                sock.listen(64)
                sock.settimeout(0.5)
                setattr(self, attribute, sock.getsockname()[1])
                listeners.append((sock, handler))
            for sock, handler in listeners:
                self._spawn(self._accept, sock, handler)
            self._spawn(self._heartbeats, udp)
            self._spawn(self._schedule)
            self._spawn(self._watchdog)
        except Exception:
            self.close()
            raise
        LOG.info("TCP client=%s worker=%s notifications=%s; UDP heartbeat=%s",
                 self.client_port, self.worker_port, self.notification_port, self.heartbeat_port)
        return self

    def close(self):
        self.stop_event.set()
        with self.condition:
            self.condition.notify_all()
            connections = list(self.connections)
        for connection in connections:
            connection.close()
        for sock in self.sockets:
            sock.close()
        for thread in list(self.threads):
            if thread is not threading.current_thread():
                thread.join(timeout=2)

    def _accept(self, sock, handler):
        while not self.stop_event.is_set():
            try:
                client, address = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self.connection_slots.acquire(blocking=False):
                client.close()
                continue
            connection = Connection(client)
            with self.condition:
                self.connections.add(connection)
            self._spawn(self._serve, handler, connection, address)

    def _serve(self, handler, connection, address):
        try:
            handler(connection, address)
        except (ValueError, KeyError, TypeError) as exc:
            try:
                connection.send({"type": "error", "error": str(exc)})
            except (OSError, ValueError):
                pass
        except (OSError, EOFError):
            pass
        except Exception:
            LOG.exception("Ошибка обработчика соединения")
        finally:
            connection.close()
            with self.condition:
                self.connections.discard(connection)
            self.connection_slots.release()

    def _snapshot(self, task, reason=None):
        return {"type": "status", "task_id": task.id, "operation": task.operation,
                "priority": task.priority, "status": task.status, "attempt": task.attempt,
                "worker_id": task.worker_id, "version": task.version, "result": task.result,
                "error": task.error, "created_at": task.created_at, "finished_at": task.finished_at,
                "timestamp": time.time(), "reason": reason}

    def _emit(self, task, reason=None):
        task.version += 1
        event = self._snapshot(task, reason)
        task.events.append(event)
        del task.events[:-100]
        for subscriber in list(task.subscribers):
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                # Просим отставшего подписчика переподключиться, чтобы не потерять итоговое событие.
                task.subscribers.discard(subscriber)
                while True:
                    try:
                        subscriber.get_nowait()
                    except queue.Empty:
                        break
                subscriber.put_nowait({"type": "error", "error": "Подписчик отстаёт: переподключитесь"})
        LOG.info("task=%s status=%s attempt=%s worker=%s %s", task.id[:8], task.status,
                 task.attempt, task.worker_id, reason or "")

    def _authorized_task(self, message):
        task_id, token = message.get("task_id"), message.get("task_token")
        if not isinstance(task_id, str) or not isinstance(token, str):
            raise ValueError("Необходимы task_id и task_token")
        task = self.tasks.get(task_id)
        if task is None or not hmac.compare_digest(task.token.encode(), token.encode()):
            raise ValueError("Задание не найдено или токен неверен")
        return task

    def _client(self, connection, address):
        message = connection.receive(self.stop_event, time.monotonic() + 10)
        kind = message.get("type")
        if kind == "submit":
            operation, data, priority = message.get("operation"), message.get("data"), message.get("priority", 0)
            if type(priority) is not int or not 0 <= priority <= 9:
                raise ValueError("Приоритет - целое число от 0 до 9")
            validate(operation, data)
            if len(encode({"data": data})) > 1500000:
                raise ValueError("Данные задания превышают 1 500 000 байт")
            with self.condition:
                if len(self.tasks) >= self.max_tasks:
                    raise ValueError("Хранилище заданий заполнено; дождитесь очистки завершённых заданий")
                self.sequence += 1
                task = Task(uuid.uuid4().hex, secrets.token_hex(24), operation, data, priority, self.sequence)
                self.tasks[task.id] = task
                heapq.heappush(self.pending, (-priority, task.sequence, task.id))
                self._emit(task)
                self.condition.notify_all()
                response = {"type": "submitted", "task_id": task.id, "task_token": task.token,
                            "notification_port": self.notification_port}
        elif kind == "status":
            with self.condition:
                response = self._snapshot(self._authorized_task(message))
        elif kind == "monitor":
            response = self.monitor()
        else:
            raise ValueError("Ожидается submit, status или monitor")
        connection.send(response)

    def monitor(self):
        with self.condition:
            now = time.monotonic()
            counts = Counter(t.status for t in self.tasks.values())
            return {"type": "monitor", "tasks": {s: counts[s] for s in ("queued", "running", "completed", "error")},
                    "workers": [{"worker_id": w.id, "state": ("busy" if w.task_id else "idle") if w.alive else "dead",
                                 "task_id": w.task_id, "heartbeat_age": round(now - w.last_seen, 2),
                                 "completed": w.completed, "address": w.address, "reason": w.reason}
                                for w in self.workers.values()],
                    "queue": [{"task_id": t.id, "operation": t.operation, "priority": t.priority}
                              for t in sorted((t for t in self.tasks.values() if t.status == "queued"),
                                              key=lambda t: (-t.priority, t.sequence))[:100]],
                    "max_attempts": self.max_attempts, "task_timeout": self.task_timeout,
                    "heartbeat_timeout": self.heartbeat_timeout, "retention_seconds": self.retention}

    def _subscription(self, connection, address):
        message = connection.receive(self.stop_event, time.monotonic() + 10)
        if message.get("type") != "subscribe":
            raise ValueError("Ожидается subscribe")
        version = message.get("after_version", 0)
        if type(version) is not int or version < 0:
            raise ValueError("after_version должен быть неотрицательным целым числом")
        events = queue.Queue(maxsize=128)
        with self.condition:
            task = self._authorized_task(message)
            if version > task.version:
                raise ValueError("after_version превышает текущую версию")
            replay = [e for e in task.events if e["version"] > version]
            if replay and version < task.events[0]["version"] - 1:
                replay = [task.events[-1]]
            for event in replay:
                events.put_nowait(event)
            if task.status in FINAL and not replay:
                events.put_nowait(task.events[-1])
            task.subscribers.add(events)
        try:
            while not self.stop_event.is_set():
                try:
                    event = events.get(timeout=1)
                except queue.Empty:
                    connection.send({"type": "ping"})
                    continue
                connection.send(event)
                if event.get("status") in FINAL or event["type"] == "error":
                    return
        finally:
            with self.condition:
                task.subscribers.discard(events)

    def _worker(self, connection, address):
        message = connection.receive(self.stop_event, time.monotonic() + 10)
        worker_id = message.get("worker_id")
        if message.get("type") != "register" or message.get("protocol") != 1:
            raise ValueError("Ожидается register с protocol=1")
        if not isinstance(worker_id, str) or not 1 <= len(worker_id) <= 100:
            raise ValueError("worker_id - строка длиной от 1 до 100")
        worker = Worker(worker_id, secrets.token_hex(24), connection, address[0])
        with self.condition:
            previous = self.workers.get(worker_id)
            if previous and previous.alive:
                raise ValueError("Воркер с таким именем уже подключён")
            # Подтверждаем регистрацию до того, как планировщик сможет отправить задание.
            connection.send({"type": "registered", "session_token": worker.token,
                             "heartbeat_port": self.heartbeat_port,
                             "heartbeat_interval": min(1, self.heartbeat_timeout / 3)})
            self.workers[worker_id] = worker
            self.condition.notify_all()
        LOG.info("Воркер подключён: %s", worker_id)
        try:
            while not self.stop_event.is_set():
                message = connection.receive(self.stop_event)
                if message.get("type") != "ack":
                    raise ValueError("Ожидается ack")
                with self.condition:
                    task_id = message.get("task_id")
                    task = self.tasks.get(task_id) if isinstance(task_id, str) else None
                    accepted = bool(worker.alive and task and task.status == "running" and
                                    task.worker_id == worker.id and worker.task_id == task.id and
                                    task.attempt_id == message.get("attempt_id") and
                                    time.monotonic() < task.deadline)
                    if accepted:
                        if type(message.get("ok")) is not bool:
                            raise ValueError("ACK должен содержать ok: true/false")
                        result = message.get("result") if message["ok"] else None
                        # Оставляем место для остальных полей сообщения о статусе.
                        if len(encode({"result": result})) > 1800000:
                            raise ValueError("Результат слишком большой")
                        task.result = result
                        task.error = None if message["ok"] else str(message.get("error", "Ошибка вычисления"))[:2000]
                        task.status = "completed" if message["ok"] else "error"
                        task.finished_at = time.time()
                        task.data = None
                        worker.task_id = None
                        worker.completed += 1
                        self._emit(task)
                        self.condition.notify_all()
                connection.send({"type": "ack_received", "task_id": task_id, "accepted": accepted})
        finally:
            self._lose_worker(worker, "Разрыв TCP-соединения")

    def _lose_worker(self, worker, reason):
        with self.condition:
            if not worker.alive or self.workers.get(worker.id) is not worker:
                return
            worker.alive, worker.reason = False, reason
            task = self.tasks.get(worker.task_id)
            worker.task_id = None
            if task and task.status == "running" and task.worker_id == worker.id:
                task.last_failed_worker = worker.id
                task.attempt_id, task.worker_id = None, None
                if task.attempt >= self.max_attempts:
                    task.status, task.error = "error", f"Исчерпаны попытки ({self.max_attempts}): {reason}"
                    task.finished_at, task.data = time.time(), None
                else:
                    task.status = "queued"
                    heapq.heappush(self.pending, (-task.priority, task.sequence, task.id))
                self._emit(task, reason)
            self.condition.notify_all()
        worker.connection.close()
        LOG.warning("Воркер %s отключён: %s", worker.id, reason)

    def _schedule(self):
        while not self.stop_event.is_set():
            with self.condition:
                available = [w for w in self.workers.values() if w.alive and w.task_id is None]
                if not self.pending or not available:
                    self.condition.wait(timeout=0.2)
                    continue
                deferred = []
                worker = task = None
                while self.pending:
                    entry = heapq.heappop(self.pending)
                    candidate = self.tasks.get(entry[2])
                    if candidate is None or candidate.status != "queued":
                        continue
                    worker = next((w for w in available if w.id != candidate.last_failed_worker), None)
                    if worker is not None:
                        task = candidate
                        break
                    deferred.append(entry)
                for entry in deferred:
                    heapq.heappush(self.pending, entry)
                if task is None:
                    self.condition.wait(timeout=0.2)
                    continue
                task.status, task.worker_id = "running", worker.id
                task.attempt += 1
                task.attempt_id = uuid.uuid4().hex
                task.deadline = time.monotonic() + self.task_timeout
                worker.task_id = task.id
                message = {"type": "task", "task_id": task.id, "attempt_id": task.attempt_id,
                           "attempt": task.attempt, "operation": task.operation, "data": task.data}
                self._emit(task)
            try:
                worker.connection.send(message)
            except (OSError, ProtocolError):
                self._lose_worker(worker, "Ошибка передачи задания")

    def _heartbeats(self, sock):
        while not self.stop_event.is_set():
            try:
                packet, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                message = json.loads(packet)
                if not isinstance(message, dict) or message.get("type") != "heartbeat":
                    continue
                worker_id, token = message.get("worker_id"), message.get("session_token")
                if not isinstance(worker_id, str) or not isinstance(token, str):
                    continue
                with self.condition:
                    worker = self.workers.get(worker_id)
                    if worker and worker.alive and hmac.compare_digest(worker.token.encode(), token.encode()):
                        worker.last_seen = time.monotonic()
            except (ValueError, UnicodeError, RecursionError):
                continue

    def _watchdog(self):
        while not self.stop_event.wait(0.1):
            with self.condition:
                now = time.monotonic()
                expired = []
                for worker in self.workers.values():
                    if not worker.alive:
                        continue
                    task = self.tasks.get(worker.task_id)
                    if now - worker.last_seen > self.heartbeat_timeout:
                        expired.append((worker, "Таймаут UDP heartbeat"))
                    elif task and task.status == "running" and now >= task.deadline:
                        expired.append((worker, "Таймаут выполнения: ACK не получен"))
                for worker, reason in expired:
                    self._lose_worker(worker, reason)
                for task_id, task in list(self.tasks.items()):
                    if task.finished_at and time.time() - task.finished_at > self.retention and not task.subscribers:
                        del self.tasks[task_id]


def main():
    parser = argparse.ArgumentParser(description="Брокер анализа торговых транзакций")
    parser.add_argument("--host", default="127.0.0.1")
    for name, default in (("client", 9000), ("worker", 9001), ("notification", 9002), ("heartbeat", 9003)):
        parser.add_argument(f"--{name}-port", type=int, default=default)
    parser.add_argument("--heartbeat-timeout", type=float, default=5)
    parser.add_argument("--task-timeout", type=float, default=30)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--retention", type=float, default=3600)
    parser.add_argument("--max-tasks", type=int, default=10000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    broker = Broker(**vars(args))
    try:
        broker.start()
        while not broker.stop_event.wait(1):
            pass
    except KeyboardInterrupt:
        LOG.info("Остановка брокера")
    finally:
        broker.close()


if __name__ == "__main__":
    main()
