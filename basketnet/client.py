"""Клиентский API и командная строка. Подписки используют отдельный TCP-порт."""

import argparse
import json
from pathlib import Path
import threading
import time

from .protocol import connect
from .tasks import OPERATIONS


class Client:
    def __init__(self, host="127.0.0.1", port=9000, notification_port=9002):
        self.host, self.port, self.notification_port = host, port, notification_port

    def request(self, message):
        connection = connect(self.host, self.port)
        try:
            connection.send(message)
            response = connection.receive(deadline=time.monotonic() + 10)
            if response.get("type") == "error":
                raise ValueError(response["error"])
            return response
        finally:
            connection.close()

    def submit(self, operation, data, priority=0):
        return self.request({"type": "submit", "operation": operation, "data": data, "priority": priority})

    def status(self, handle):
        return self.request({"type": "status", "task_id": handle["task_id"], "task_token": handle["task_token"]})

    def monitor(self):
        return self.request({"type": "monitor"})

    def subscribe(self, handle, after_version=0, stop=None, timeout=None):
        stop = stop or threading.Event()
        deadline = time.monotonic() + timeout if timeout is not None else None
        connection = connect(self.host, handle.get("notification_port", self.notification_port))
        try:
            connection.send({"type": "subscribe", "task_id": handle["task_id"],
                             "task_token": handle["task_token"], "after_version": after_version})
            while not stop.is_set():
                event = connection.receive(stop, deadline)
                if event.get("type") == "error":
                    raise ValueError(event["error"])
                if event.get("type") == "ping":
                    continue
                yield event
                if event.get("status") in ("completed", "error"):
                    return
        finally:
            connection.close()

    def wait(self, handle, timeout=60):
        for event in self.subscribe(handle, timeout=timeout):
            if event["status"] in ("completed", "error"):
                return event
        raise EOFError("Подписка завершилась без результата")


def main():
    parser = argparse.ArgumentParser(description="Клиент анализа потребительской корзины")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--notification-port", type=int, default=9002)
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit", help="Отправить задание")
    submit.add_argument("operation", choices=OPERATIONS)
    inputs = submit.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--data", help="Данные в JSON; текст также должен быть JSON-строкой")
    inputs.add_argument("--file", help="Файл с JSON-данными UTF-8")
    submit.add_argument("--priority", type=int, default=0)
    submit.add_argument("--save", help="Сохранить task_id и токен в файл")
    submit.add_argument("--wait", action="store_true")
    submit.add_argument("--timeout", type=float, default=60)
    for name in ("status", "watch"):
        command = commands.add_parser(name)
        command.add_argument("handle", help="JSON-файл с task_id и task_token")
    commands.add_parser("monitor")
    args = parser.parse_args()
    client = Client(args.host, args.port, args.notification_port)

    def show(value):
        print(json.dumps(value, ensure_ascii=False, indent=2))

    try:
        if args.command == "submit":
            data = json.loads(Path(args.file).read_text(encoding="utf-8-sig") if args.file else args.data)
            handle = client.submit(args.operation, data, args.priority)
            if args.save:
                Path(args.save).write_text(json.dumps(handle, ensure_ascii=False, indent=2), encoding="utf-8")
            show(handle)
            if args.wait:
                final = None
                for final in client.subscribe(handle, timeout=args.timeout):
                    show(final)
                if final and final["status"] == "error":
                    raise SystemExit(1)
        elif args.command == "monitor":
            show(client.monitor())
        else:
            handle = json.loads(Path(args.handle).read_text(encoding="utf-8-sig"))
            if args.command == "status":
                show(client.status(handle))
            else:
                for event in client.subscribe(handle):
                    show(event)
    except (ValueError, OSError, EOFError, KeyError) as exc:
        parser.exit(1, f"Ошибка: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Ожидание прервано; задание продолжает выполняться.\n")


if __name__ == "__main__":
    main()
