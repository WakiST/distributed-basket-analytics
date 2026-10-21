"""Один вычислительный узел. Несколько процессов дают параллелизм CPU."""

import argparse
import json
import logging
import os
import socket
import threading
import time
import uuid

from .protocol import connect
from .tasks import execute

LOG = logging.getLogger("worker")


class WorkerClient:
    def __init__(self, host="127.0.0.1", port=9001, worker_id=None, delay=0, heartbeat=True, reconnect=True):
        self.host, self.port = host, port
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        if delay < 0:
            raise ValueError("Задержка не может быть отрицательной")
        self.delay, self.heartbeat, self.reconnect = delay, heartbeat, reconnect
        self.stop_event = threading.Event()
        self.connection = None

    def close(self):
        self.stop_event.set()
        if self.connection:
            self.connection.close()

    def _pulse(self, registration, session_stop):
        packet = json.dumps({"type": "heartbeat", "worker_id": self.worker_id,
                             "session_token": registration["session_token"]}).encode()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            while not session_stop.is_set() and not self.stop_event.is_set():
                try:
                    udp.sendto(packet, (self.host, registration["heartbeat_port"]))
                except OSError as exc:
                    LOG.warning("UDP: %s", exc)
                session_stop.wait(registration["heartbeat_interval"])

    def run(self):
        while not self.stop_event.is_set():
            session_stop = threading.Event()
            pulse = None
            try:
                self.connection = connect(self.host, self.port)
                self.connection.send({"type": "register", "protocol": 1, "worker_id": self.worker_id})
                registration = self.connection.receive(self.stop_event, time.monotonic() + 10)
                if registration.get("type") != "registered":
                    raise ValueError(registration.get("error", "Ошибка регистрации"))
                LOG.info("%s подключён", self.worker_id)
                if self.heartbeat:
                    pulse = threading.Thread(target=self._pulse, args=(registration, session_stop), daemon=True)
                    pulse.start()
                while not self.stop_event.is_set():
                    message = self.connection.receive(self.stop_event)
                    if message.get("type") == "ack_received":
                        if not message["accepted"]:
                            LOG.warning("Запоздалый ACK отклонён: %s", message["task_id"])
                        continue
                    if message.get("type") != "task":
                        raise ValueError("Неожиданное сообщение брокера")
                    LOG.info("%s: %s (попытка %s)", message["task_id"][:8], message["operation"], message["attempt"])
                    if self.stop_event.wait(self.delay):
                        break
                    ack = {"type": "ack", "task_id": message["task_id"], "attempt_id": message["attempt_id"]}
                    try:
                        ack.update(ok=True, result=execute(message["operation"], message["data"]))
                    except Exception as exc:
                        ack.update(ok=False, error=f"{type(exc).__name__}: {exc}")
                    self.connection.send(ack)
            except (OSError, EOFError, ValueError) as exc:
                if not self.stop_event.is_set():
                    LOG.warning("Соединение: %s", exc)
            finally:
                session_stop.set()
                if self.connection:
                    self.connection.close()
                if pulse:
                    pulse.join(timeout=2)
            if not self.reconnect or self.stop_event.wait(2):
                break


def main():
    parser = argparse.ArgumentParser(description="Воркер обработки заданий")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--id", dest="worker_id")
    parser.add_argument("--delay", type=float, default=0, help="Искусственная задержка для демонстрации, секунды")
    parser.add_argument("--no-heartbeat", action="store_false", dest="heartbeat", help="Демонстрация отказа UDP heartbeat")
    parser.add_argument("--no-reconnect", action="store_false", dest="reconnect")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    worker = WorkerClient(**vars(args))
    try:
        worker.run()
    except KeyboardInterrupt:
        pass
    finally:
        worker.close()


if __name__ == "__main__":
    main()
