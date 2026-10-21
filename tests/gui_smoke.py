"""Проверка интерфейса с настоящим сетевым соединением: python -m tests.gui_smoke."""

import threading
import time
import tkinter as tk

from basketnet.broker import Broker
from basketnet.gui import Application
from basketnet.worker import WorkerClient


def main():
    broker = Broker(client_port=0, worker_port=0, notification_port=0, heartbeat_port=0).start()
    worker = WorkerClient(port=broker.worker_port, worker_id="gui-smoke", reconnect=False, delay=0.1)
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    root = tk.Tk()
    root.withdraw()
    app = Application(root, port=broker.client_port, notification_port=broker.notification_port)
    try:
        app.submit()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            root.update()
            if app.latest and next(iter(app.latest.values()))["status"] == "completed" and app.worker_tree.get_children():
                break
            time.sleep(0.02)
        else:
            raise AssertionError("Интерфейс не показал завершённое задание и воркер")
        task_id = next(iter(app.latest))
        assert app.latest[task_id]["result"]["basket_count"] == 4
        assert [event["status"] for event in app.history[task_id]] == ["queued", "running", "completed"]
        assert '"pairs"' in app.result_text.get("1.0", "end")
        assert app.task_tree.item(task_id)["values"][3] == "Завершено"
        assert "Чеков: 4" in app.summary_text.get("1.0", "end")
        assert "Молоко" in app.summary_text.get("1.0", "end")
        print("Интерфейс проверен: отправка, уведомления, результат и мониторинг воркеров")
    finally:
        app.close()
        worker.close()
        broker.close()
        thread.join(timeout=3)


if __name__ == "__main__":
    main()
