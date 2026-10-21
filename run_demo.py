"""Запуск брокера, трёх отдельных процессов-воркеров и графического клиента."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

from basketnet.client import Client

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description="Локальная демонстрация BasketNet")
    parser.add_argument("--headless", action="store_true", help="Проверить семь операций без GUI и выйти")
    args = parser.parse_args()
    processes, streams = [], []
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)

    def launch(name, *command):
        stream = (logs / f"{name}.log").open("w", encoding="utf-8")
        streams.append(stream)
        process = subprocess.Popen([sys.executable, "-u", "-m", *command], cwd=ROOT,
                                   stdout=stream, stderr=subprocess.STDOUT,
                                   env=dict(os.environ, PYTHONUTF8="1"),
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        processes.append(process)
        return process

    try:
        broker = launch("broker", "basketnet.broker")
        client = Client()
        for _ in range(50):
            time.sleep(0.1)
            if broker.poll() is not None:
                raise RuntimeError("Брокер не запустился. Проверьте logs/broker.log и занятость портов 9000-9003.")
            try:
                client.monitor()
                break
            except (OSError, ValueError, EOFError):
                continue
        else:
            raise RuntimeError("Брокер не отвечает")
        # Проверяем, что при запуске не ответил уже работающий сторонний экземпляр брокера.
        time.sleep(0.2)
        if broker.poll() is not None:
            raise RuntimeError("Порты заняты другим процессом; см. logs/broker.log")
        for number in range(1, 4):
            launch(f"worker-{number}", "basketnet.worker", "--id", f"worker-{number}", "--delay", "0.5")
        if args.headless:
            from basketnet.samples import SAMPLES
            handles = [(op, client.submit(op, data, priority=i % 10)) for i, (op, data) in enumerate(SAMPLES.items())]
            for operation, handle in handles:
                result = client.wait(handle, timeout=30)
                if result["status"] != "completed":
                    raise RuntimeError(str(result))
                print(f"OK {operation}: worker={result['worker_id']}, attempt={result['attempt']}")
            print("Все 7 операций успешно выполнены.")
        else:
            import tkinter as tk
            from basketnet.gui import Application
            root = tk.Tk()
            Application(root)
            root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for stream in streams:
            stream.close()


if __name__ == "__main__":
    main()
