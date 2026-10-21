"""Веб-клиент: HTTP для браузера, TCP для брокера и отдельный канал уведомлений."""

import argparse
from collections import deque
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit

from .client import Client
from .samples import DESCRIPTIONS, SAMPLES
from .tasks import OPERATIONS
from .shop import ShopStore, OrderProcessor

LOG = logging.getLogger("web")
STATIC = Path(__file__).with_name("web_static")
MAX_BODY = 1600000
FINAL = {"completed", "error"}


def browser_json(value):
    """Сохраняем точность целых, которые JavaScript не может представить без округления."""
    def safe(item):
        if type(item) is int and abs(item) > 9007199254740991:
            return str(item)
        if isinstance(item, dict):
            return {key: safe(value) for key, value in item.items()}
        if isinstance(item, (list, tuple)):
            return [safe(value) for value in item]
        return item
    return json.dumps(safe(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


@dataclass
class Session:
    user: dict | None = None
    valid: bool = True
    tasks: dict = field(default_factory=dict)
    condition: threading.Condition = field(default_factory=threading.Condition)
    events: deque = field(default_factory=lambda: deque(maxlen=200))
    sequence: int = 0
    streams: int = 0
    last_seen: float = field(default_factory=time.monotonic)


class WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, host="127.0.0.1", port=8088, broker_host="127.0.0.1", broker_port=9000,
                 notification_port=9002, database="data/shop.sqlite3", admin_password=None):
        self.client = Client(broker_host, broker_port, notification_port)
        self.sessions = {}
        self.session_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.watchers = set()
        super().__init__((host, port), Handler)
        self.store = ShopStore(database, admin_password)
        self.processor = OrderProcessor(self.store, self.client)
        self.auth_attempts = {}

    def rotate_session(self, old, user):
        with self.session_lock:
            old.valid = False
            with old.condition:
                old.condition.notify_all()
            for token, session in list(self.sessions.items()):
                if session is old:
                    del self.sessions[token]
            token = secrets.token_urlsafe(32)
            self.sessions[token] = Session(user=user)
            return token

    def get_session(self, cookie, create=False):
        cookies = SimpleCookie()
        try:
            cookies.load(cookie or "")
            token = cookies["basketnet_session"].value if "basketnet_session" in cookies else None
        except Exception:
            token = None
        with self.session_lock:
            now = time.monotonic()
            session = self.sessions.get(token)
            if session is not None and now - session.last_seen < 43200 and session.valid:
                session.last_seen = now
                return session, None
            if not create:
                return None, None
            for key, old in list(self.sessions.items()):
                with old.condition:
                    active = any(t["snapshot"].get("status") not in FINAL and not t.get("unavailable") for t in old.tasks.values())
                    if now - old.last_seen > 3600 and not old.streams and not active:
                        del self.sessions[key]
            if len(self.sessions) >= 128:
                raise ValueError("Достигнут предел сеансов. Повторите позже.")
            token = secrets.token_urlsafe(32)
            session = Session()
            self.sessions[token] = session
            return session, token

    @staticmethod
    def public_task(record):
        return {"snapshot": record["snapshot"], "history": list(record["history"]),
                "connection_error": record.get("connection_error")}

    def publish(self, session, record):
        session.sequence += 1
        session.events.append((session.sequence, self.public_task(record)))
        session.condition.notify_all()

    def submit(self, session, operation, data, priority):
        with session.condition:
            if len(session.tasks) >= 100:
                raise ValueError("В одном сеансе допускается 100 заданий. Сохраните результаты и откройте новый сеанс браузера.")
            active = sum(t["snapshot"]["status"] not in FINAL and not t.get("unavailable") for t in session.tasks.values())
            if active >= 20:
                raise ValueError("Дождитесь завершения заданий: одновременно допускается 20 активных расчётов.")
            handle = self.client.submit(operation, data, priority)
            record = {"handle": handle, "snapshot": {"task_id": handle["task_id"], "operation": operation,
                      "priority": priority, "status": "queued", "attempt": 0, "version": 0,
                      "worker_id": None, "result": None, "error": None, "created_at": time.time()}, "history": []}
            session.tasks[handle["task_id"]] = record
            self.publish(session, record)
            result = self.public_task(record)
        thread = threading.Thread(target=self.watch, args=(session, record), daemon=True)
        with self.session_lock:
            self.watchers.add(thread)
        thread.start()
        return result

    def watch(self, session, record):
        version = 0
        try:
            while not self.stop_event.is_set():
                try:
                    for event in self.client.subscribe(record["handle"], after_version=version, stop=self.stop_event):
                        version = event["version"]
                        with session.condition:
                            record["snapshot"] = event
                            record["connection_error"] = None
                            record["history"].append(event)
                            del record["history"][:-100]
                            self.publish(session, record)
                        if event["status"] in FINAL:
                            return
                except ValueError as exc:
                    with session.condition:
                        record["connection_error"] = f"Задание недоступно у брокера: {exc}"
                        record["unavailable"] = True
                        self.publish(session, record)
                    return
                except (OSError, EOFError) as exc:
                    if not self.stop_event.is_set():
                        with session.condition:
                            record["connection_error"] = f"Восстанавливаем соединение с брокером: {exc}"
                            self.publish(session, record)
                if self.stop_event.wait(2):
                    return
        finally:
            with self.session_lock:
                self.watchers.discard(threading.current_thread())

    def server_close(self):
        self.stop_event.set()
        self.processor.close()
        with self.session_lock:
            sessions = list(self.sessions.values())
            watchers = list(self.watchers)
        for session in sessions:
            with session.condition:
                session.condition.notify_all()
        super().server_close()
        for thread in watchers:
            thread.join(timeout=2)
        self.store.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "BasketNet/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, fmt, *args):
        LOG.debug(fmt, *args)

    def headers_for(self, status, mime, length=None, token=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        if length is not None:
            self.send_header("Content-Length", str(length))
        if token:
            self.send_header("Set-Cookie", f"basketnet_session={token}; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()

    def reply(self, status, data, token=None):
        payload = browser_json(data).encode("utf-8")
        self.headers_for(status, "application/json; charset=utf-8", len(payload), token)
        self.wfile.write(payload)

    def session(self, create=False):
        session, token = self.server.get_session(self.headers.get("Cookie"), create)
        if session is None:
            self.reply(401, {"error": "Сеанс завершён. Обновите страницу."})
        return session, token

    def require_user(self, session, admin=False):
        if not session.user:
            self.reply(401, {"error": "Войдите в аккаунт"})
            return False
        if admin and session.user["role"] != "admin":
            self.reply(403, {"error": "Этот раздел доступен только администратору"})
            return False
        return True

    def do_GET(self):
        try:
            path = urlsplit(self.path).path
            assets = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                      "/style.css": ("style.css", "text/css"), "/favicon.svg": ("favicon.svg", "image/svg+xml")}
            if path in assets:
                filename, mime = assets[path]
                payload = (STATIC / filename).read_bytes()
                self.headers_for(200, mime + "; charset=utf-8", len(payload))
                self.wfile.write(payload)
                return
            if path == "/api/catalog":
                self.reply(200, {"products": self.server.store.products()})
                return
            session, token = self.session(create=path == "/api/session")
            if session is None:
                return
            if path == "/api/session":
                with session.condition:
                    tasks = [self.server.public_task(t) for t in session.tasks.values()]
                self.reply(200, {"operations": OPERATIONS if session.user and session.user["role"] == "admin" else [],
                                 "samples": SAMPLES if session.user and session.user["role"] == "admin" else {},
                                 "descriptions": DESCRIPTIONS, "user": session.user, "tasks": tasks}, token)
            elif path in ("/api/cart", "/api/coupons", "/api/orders"):
                if not self.require_user(session):
                    return
                store = self.server.store
                if path == "/api/cart":
                    result = store.cart(session.user["id"])
                elif path == "/api/coupons":
                    result = store.coupons(session.user["id"])
                else:
                    result = [store.public_order(o) for o in store.orders(session.user["id"]) if not o["batch_id"]]
                self.reply(200, result)
            elif path == "/api/admin/state":
                if not self.require_user(session, admin=True):
                    return
                store = self.server.store
                self.reply(200, {"orders": [store.public_order(o) for o in store.orders()][:1000],
                                 "batches": store.batches()[:20], "products": store.products(admin=True),
                                 "coupons": store.admin_coupons()})
            elif path == "/api/monitor":
                if not self.require_user(session, admin=True):
                    return
                self.reply(200, self.server.client.monitor())
            elif path == "/api/events":
                if not self.require_user(session):
                    return
                self.events(session)
            elif path.startswith("/api/tasks/"):
                if not self.require_user(session, admin=True):
                    return
                parts = path.strip("/").split("/")
                with session.condition:
                    record = session.tasks.get(parts[2]) if len(parts) >= 3 else None
                    if not record:
                        self.reply(404, {"error": "Задание не найдено в вашем сеансе"})
                    elif len(parts) == 4 and parts[3] == "handle":
                        self.reply(200, record["handle"])
                    elif len(parts) == 3:
                        self.reply(200, self.server.public_task(record))
                    else:
                        self.reply(404, {"error": "Адрес не найден"})
            else:
                self.reply(404, {"error": "Адрес не найден"})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except (OSError, EOFError) as exc:
            self.reply(503, {"error": f"Брокер недоступен: {exc}"})
        except (ValueError, KeyError) as exc:
            self.reply(400, {"error": str(exc)})

    def do_POST(self):
        try:
            path = urlsplit(self.path).path
            if path not in {"/api/tasks", "/api/auth/login", "/api/auth/register", "/api/auth/logout", "/api/cart",
                            "/api/orders", "/api/admin/generate", "/api/admin/stop", "/api/admin/product",
                            "/api/admin/coupon", "/api/admin/analytics", "/api/coupons/check"}:
                self.reply(404, {"error": "Адрес не найден"})
                return
            origin = self.headers.get("Origin")
            if self.headers.get("X-BasketNet-Client") != "browser" or (origin and urlsplit(origin).netloc != self.headers.get("Host")):
                self.reply(403, {"error": "Запрос должен поступать со страницы BasketNet"})
                return
            session, _ = self.session()
            if session is None:
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self.reply(415, {"error": "Ожидается application/json"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                self.reply(413, {"error": "Объём запроса должен быть от 1 до 1 600 000 байт"})
                return
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("Запрос получен не полностью")
            message = json.loads(body, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            if not isinstance(message, dict):
                raise ValueError("Ожидается JSON-объект")
            store = self.server.store
            if path in ("/api/auth/login", "/api/auth/register"):
                address = self.client_address[0]
                with self.server.session_lock:
                    now = time.monotonic()
                    self.server.auth_attempts = {ip: times for ip, times in self.server.auth_attempts.items() if times and now - times[-1] < 60}
                    attempts = [t for t in self.server.auth_attempts.get(address, []) if now - t < 60]
                    if len(attempts) >= 15:
                        self.reply(429, {"error": "Слишком много попыток входа. Подождите минуту"})
                        return
                    self.server.auth_attempts[address] = attempts + [now]
                action = store.register if path.endswith("register") else store.login
                user = action(message.get("login"), message.get("password"))
                token = self.server.rotate_session(session, user)
                self.reply(200, {"user": user}, token)
                return
            if path == "/api/auth/logout":
                token = self.server.rotate_session(session, None)
                self.reply(200, {"user": None}, token)
                return
            if not self.require_user(session, admin=path.startswith("/api/admin/") or path == "/api/tasks"):
                return
            if path == "/api/cart":
                self.reply(200, store.set_cart(session.user["id"], message.get("product_id"), message.get("quantity")))
            elif path == "/api/coupons/check":
                self.reply(200, store.check_coupon(session.user["id"], message.get("code")))
            elif path == "/api/orders":
                order = store.create_order(session.user["id"], message.get("request_key"), message.get("coupon", ""))
                self.reply(201, store.public_order(order))
            elif path == "/api/admin/generate":
                self.reply(201, store.create_batch(session.user["id"], message))
            elif path == "/api/admin/stop":
                store.stop_batch(message.get("batch_id"))
                self.reply(200, {"ok": True})
            elif path == "/api/admin/product":
                store.save_product(message)
                self.reply(200, {"ok": True})
            elif path == "/api/admin/coupon":
                self.reply(200, store.grant_coupon(message))
            elif path == "/api/admin/analytics":
                operation = message.get("operation")
                data = store.analytics_data(operation, message.get("include_generated") is True)
                self.reply(201, self.server.submit(session, operation, data, 3))
            else:
                result = self.server.submit(session, message.get("operation"), message.get("data"), message.get("priority", 0))
                self.reply(201, result)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            self.reply(400, {"error": str(exc)})
        except (OSError, EOFError) as exc:
            self.reply(503, {"error": f"Брокер недоступен: {exc}"})

    def events(self, session):
        # Одно соединение передаёт события всех заданий браузера, не занимая по каналу на каждое.
        with session.condition:
            if session.streams >= 4:
                self.reply(429, {"error": "Открыто слишком много вкладок этого сеанса"})
                return
            session.streams += 1
            cursor = session.sequence
            initial = [self.server.public_task(t) for t in session.tasks.values()]
        try:
            self.headers_for(200, "text/event-stream; charset=utf-8")
            self.wfile.write(b"retry: 2000\n\n")
            self.send_event("snapshot", initial)
            last_orders = None
            while not self.server.stop_event.is_set() and session.valid:
                with session.condition:
                    session.last_seen = time.monotonic()
                    if cursor == session.sequence:
                        session.condition.wait(timeout=1)
                    if session.events and cursor < session.events[0][0] - 1:
                        batch = [("snapshot", [self.server.public_task(t) for t in session.tasks.values()])]
                    else:
                        batch = [("task", task) for sequence, task in session.events if sequence > cursor]
                    cursor = session.sequence
                if not session.valid or self.server.stop_event.is_set():
                    break
                orders = [self.server.store.public_order(o) for o in self.server.store.orders(session.user["id"])
                          if not o["batch_id"]]
                if orders != last_orders:
                    self.send_event("orders", orders)
                    last_orders = orders
                for name, data in batch:
                    self.send_event(name, data)
                if not batch:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except (OSError, EOFError):
            pass
        finally:
            with session.condition:
                session.streams -= 1

    def send_event(self, name, data):
        payload = browser_json(data)
        self.wfile.write(f"event: {name}\ndata: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()


def main():
    parser = argparse.ArgumentParser(description="Веб-клиент BasketNet для браузера")
    parser.add_argument("--host", default="127.0.0.1", help="Для локальной сети: 0.0.0.0")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--broker-host", default="127.0.0.1")
    parser.add_argument("--broker-port", type=int, default=9000)
    parser.add_argument("--notification-port", type=int, default=9002)
    parser.add_argument("--database", default="data/shop.sqlite3", help="Файл SQLite с аккаунтами и заказами")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = WebServer(**vars(args))
    LOG.info("Веб-клиент: http://%s:%s", args.host, server.server_port)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
