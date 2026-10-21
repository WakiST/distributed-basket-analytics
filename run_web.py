"""Запуск брокера, воркеров и сайта одной командой; клиенту нужен только браузер."""

import argparse
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import subprocess
import secrets
import sys
import tempfile
import time
from urllib.request import build_opener, HTTPCookieProcessor, Request

from basketnet.client import Client
from basketnet.samples import SAMPLES

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description="Сервер BasketNet с веб-клиентом и воркерами")
    parser.add_argument("--host", default="0.0.0.0", help="Адрес сайта; 0.0.0.0 открывает доступ из локальной сети")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--worker-delay", type=float, default=0, help="Задержка обработки для демонстрации, секунды")
    parser.add_argument("--check", action="store_true", help="Проверить все операции через HTTP и завершить работу")
    parser.add_argument("--database", default="data/shop.sqlite3", help="Файл магазина SQLite")
    args = parser.parse_args()
    if not 1 <= args.workers <= 16 or args.worker_delay < 0:
        parser.error("Число воркеров: 1-16, задержка неотрицательная")
    processes, streams = [], []
    temporary = tempfile.TemporaryDirectory(prefix="basketnet-check-") if args.check else None
    database = str(Path(temporary.name) / "shop.sqlite3") if temporary else str(Path(args.database).resolve())
    environment = dict(os.environ, PYTHONUTF8="1")
    if args.check:
        environment["BASKETNET_ADMIN_PASSWORD"] = secrets.token_urlsafe(24)
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)

    def launch(name, *command):
        stream = (logs / f"web-{name}.log").open("w", encoding="utf-8")
        streams.append(stream)
        process = subprocess.Popen([sys.executable, "-u", "-m", *command], cwd=ROOT,
                                   stdout=stream, stderr=subprocess.STDOUT,
                                   env=environment,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        processes.append(process)
        return process

    def ready(process, probe, label):
        for _ in range(40):
            time.sleep(0.1)
            if process.poll() is not None:
                raise RuntimeError(f"{label} не запустился. Проверьте журналы logs/web-*.log и занятость портов.")
            try:
                probe()
                # Исключаем ответ старого процесса, если новый ещё не успел завершиться с ошибкой.
                time.sleep(0.2)
                if process.poll() is not None:
                    raise RuntimeError(f"{label}: порт занят другим процессом.")
                return
            except (OSError, EOFError, ValueError):
                continue
        raise RuntimeError(f"{label} не отвечает")

    try:
        broker = launch("broker", "basketnet.broker")
        client = Client()
        ready(broker, client.monitor, "Брокер")
        workers = []
        for number in range(1, args.workers + 1):
            workers.append(launch(f"worker-{number}", "basketnet.worker", "--id", f"worker-{number}", "--delay", str(args.worker_delay)))
        web = launch("site", "basketnet.web", "--host", args.host, "--port", str(args.port), "--database", database)
        local_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
        url = f"http://{local_host}:{args.port}"
        opener = build_opener(HTTPCookieProcessor(CookieJar()))

        def request(path, body=None):
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
            headers = {"Content-Type": "application/json", "X-BasketNet-Client": "browser"} if payload else {}
            with opener.open(Request(url + path, data=payload, headers=headers), timeout=15) as response:
                return json.load(response)

        ready(web, lambda: request("/api/session"), "Сайт")
        print(f"BasketNet запущен. Воркеров: {args.workers}", flush=True)
        print(f"На этом ПК: {url}", flush=True)
        if args.host == "0.0.0.0":
            print(f"На другом ПК: http://IPv4-адрес-этого-ПК:{args.port}", flush=True)
        print("Остановка всех компонентов: Ctrl+C. Журналы: logs/web-*.log", flush=True)
        if not args.check:
            print(f"Аккаунт администратора: admin. Данные первого входа: {database}.admin.txt", flush=True)
        if args.check:
            request("/api/auth/login", {"login": "admin", "password": environment["BASKETNET_ADMIN_PASSWORD"]})
            for operation, data in SAMPLES.items():
                record = request("/api/tasks", {"operation": operation, "data": data, "priority": 5})
                task_id = record["snapshot"]["task_id"]
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    record = request(f"/api/tasks/{task_id}")
                    if record["snapshot"]["status"] in ("completed", "error"):
                        break
                    time.sleep(0.1)
                if record["snapshot"]["status"] != "completed":
                    raise RuntimeError(f"Не выполнено задание {operation}: {record}")
                print(f"Проверено через сайт: {operation}", flush=True)
            print("Все 7 операций выполнены. Остановка тестового стенда.", flush=True)
        else:
            reported = set()
            while True:
                time.sleep(0.5)
                if broker.poll() is not None or web.poll() is not None:
                    raise RuntimeError("Брокер или сайт завершился. Проверьте журналы logs/web-*.log.")
                for number, worker in enumerate(workers, 1):
                    if worker.poll() is not None and number not in reported:
                        reported.add(number)
                        print(f"Воркер worker-{number} остановлен. Брокер продолжает работу с остальными узлами.", flush=True)
    except KeyboardInterrupt:
        print("\nОстановка BasketNet…", flush=True)
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
        if temporary:
            temporary.cleanup()


if __name__ == "__main__":
    main()
