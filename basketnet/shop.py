"""Магазин: постоянное хранение, учётные записи и обработка заказов через брокер."""

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import random
import re
import secrets
import sqlite3
import threading
import time
import uuid

FINAL = {"completed", "error"}
LOADS = {"light": 0, "medium": 500000, "heavy": 2000000}
MINSK = timezone(timedelta(hours=3))


def shop_today():
    """Календарный день магазина не зависит от часового пояса компьютера."""
    return datetime.now(MINSK).date().isoformat()


def coupon_code(value):
    if not isinstance(value, str):
        raise ValueError("Введите код купона")
    value = value.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{3,20}", value):
        raise ValueError("Код купона: 3-20 латинских букв и цифр")
    return value


PRODUCTS = [
    ("Авокадо Hass", "Овощи и фрукты", "2 шт.", 690, 10, "🥑", "Спелый и нежный, для любимых тостов."),
    ("Клубника", "Овощи и фрукты", "250 г", 890, 20, "🍓", "Сочная ягода к завтраку и просто так."),
    ("Бананы", "Овощи и фрукты", "1 кг", 549, 0, "🍌", "Немного солнца в каждой корзине."),
    ("Томаты на ветке", "Овощи и фрукты", "500 г", 620, 0, "🍅", "Ароматные томаты для свежего салата."),
    ("Молоко 3,2%", "Молочные продукты", "1 л", 325, 0, "🥛", "К каше, кофе и домашним блинам."),
    ("Сыр Гауда", "Молочные продукты", "200 г", 790, 15, "🧀", "Мягкий сливочный вкус на каждый день."),
    ("Йогурт натуральный", "Молочные продукты", "180 г", 240, 0, "🥣", "Без добавленного сахара. Добавьте ягоды."),
    ("Круассан сливочный", "Выпечка", "1 шт.", 310, 0, "🥐", "Слоистый, хрустящий, к утреннему кофе."),
    ("Хлеб на закваске", "Выпечка", "450 г", 480, 10, "🍞", "Румяная корочка и мягкая середина."),
    ("Кофе в зёрнах", "Бакалея", "250 г", 1890, 15, "☕", "Сбалансированный вкус с нотами шоколада."),
    ("Паста из твёрдых сортов", "Бакалея", "500 г", 560, 0, "🍝", "Простой способ собрать вкусный ужин."),
    ("Шоколад 70%", "Бакалея", "90 г", 450, 0, "🍫", "Насыщенный какао для маленького перерыва."),
]


def integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label}: целое число от {low} до {high}")
    return value


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200000).hex()


class ShopStore:
    def __init__(self, database, admin_password=None):
        self.lock = threading.RLock()
        self.depth = 0
        if str(database) != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(database), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, login TEXT UNIQUE NOT NULL,
                salt TEXT NOT NULL, password TEXT NOT NULL, role TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                category TEXT NOT NULL, unit TEXT NOT NULL, cents INTEGER NOT NULL,
                discount INTEGER NOT NULL, icon TEXT NOT NULL, description TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS cart(user_id INTEGER REFERENCES users(id), product_id INTEGER REFERENCES products(id),
                quantity INTEGER NOT NULL, PRIMARY KEY(user_id, product_id));
            CREATE TABLE IF NOT EXISTS coupons(code TEXT PRIMARY KEY, percent INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS user_coupons(user_id INTEGER REFERENCES users(id), code TEXT REFERENCES coupons(code),
                PRIMARY KEY(user_id, code));
            CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id),
                request_key TEXT NOT NULL, batch_id TEXT, document TEXT NOT NULL, UNIQUE(user_id, request_key));
            CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, document TEXT NOT NULL);
        """)
        with self.transaction() as db:
            # Миграция старой базы: личные одноразовые купоны сохраняют свои правила.
            columns = {r["name"] for r in db.execute("PRAGMA table_info(coupons)")}
            for name, definition in (("public", "INTEGER NOT NULL DEFAULT 0"),
                                     ("max_uses", "INTEGER"), ("per_user_limit", "INTEGER DEFAULT 1"),
                                     ("expires_on", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE coupons ADD COLUMN {name} {definition}")
            if not db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
                password = admin_password or os.environ.get("BASKETNET_ADMIN_PASSWORD") or secrets.token_urlsafe(12)
                self._new_user(db, "admin", password, "admin")
                if str(database) != ":memory:" and not admin_password and not os.environ.get("BASKETNET_ADMIN_PASSWORD"):
                    Path(str(database) + ".admin.txt").write_text(f"Логин: admin\nПароль: {password}\n", encoding="utf-8")
            if not db.execute("SELECT 1 FROM products").fetchone():
                db.executemany("INSERT INTO products(name,category,unit,cents,discount,icon,description) VALUES(?,?,?,?,?,?,?)", PRODUCTS)
            db.execute("INSERT OR IGNORE INTO coupons(code,percent) VALUES('WELCOME10',10)")

    @contextmanager
    def transaction(self):
        with self.lock:
            outer = self.depth == 0
            self.depth += 1
            try:
                yield self.db
                if outer:
                    self.db.commit()
            except Exception:
                if outer:
                    self.db.rollback()
                raise
            finally:
                self.depth -= 1

    def close(self):
        with self.lock:
            self.db.close()

    @staticmethod
    def _new_user(db, login, password, role):
        salt = secrets.token_hex(16)
        cursor = db.execute("INSERT INTO users(login,salt,password,role) VALUES(?,?,?,?)", (login, salt, password_hash(password, salt), role))
        return {"id": cursor.lastrowid, "login": login, "role": role}

    def register(self, login, password):
        if not isinstance(login, str) or not re.fullmatch(r"[A-Za-zА-Яа-яЁё0-9_]{3,32}", login):
            raise ValueError("Логин: 3-32 буквы, цифры или знак подчёркивания")
        if not isinstance(password, str) or not 8 <= len(password) <= 128:
            raise ValueError("Пароль: от 8 до 128 символов")
        with self.transaction() as db:
            try:
                user = self._new_user(db, login.casefold(), password, "customer")
            except sqlite3.IntegrityError:
                raise ValueError("Этот логин уже занят") from None
            db.execute("INSERT INTO user_coupons VALUES(?, 'WELCOME10')", (user["id"],))
            return user

    def login(self, login, password):
        if not isinstance(login, str) or not isinstance(password, str) or len(password) > 128:
            raise ValueError("Неверный логин или пароль")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM users WHERE login=?", (login.casefold(),)).fetchone()
        digest = password_hash(password, row["salt"] if row else "00" * 16)
        if not row or not hmac.compare_digest(row["password"], digest):
            raise ValueError("Неверный логин или пароль")
        return {"id": row["id"], "login": row["login"], "role": row["role"]}

    def products(self, admin=False):
        with self.transaction() as db:
            return [dict(r) for r in db.execute("SELECT * FROM products" + ("" if admin else " WHERE active=1") + " ORDER BY id")]

    def save_product(self, data):
        product_id = integer(data.get("id"), 1, 1000000, "ID товара")
        fields = []
        for key, limit in (("name", 120), ("category", 60), ("unit", 30), ("description", 250)):
            value = data.get(key)
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
                raise ValueError(f"Поле {key}: от 1 до {limit} символов")
            fields.append(value.strip())
        cents = integer(data.get("cents"), 1, 10000000, "Цена в копейках")
        discount = integer(data.get("discount"), 0, 90, "Скидка")
        if type(data.get("active")) is not bool:
            raise ValueError("Доступность должна быть логическим значением")
        with self.transaction() as db:
            cursor = db.execute("UPDATE products SET name=?,category=?,unit=?,description=?,cents=?,discount=?,active=? WHERE id=?",
                                (*fields, cents, discount, int(data["active"]), product_id))
            if not cursor.rowcount:
                raise ValueError("Товар не найден")

    def cart(self, user_id):
        with self.transaction() as db:
            return [dict(r) for r in db.execute("SELECT p.*, c.quantity FROM cart c JOIN products p ON p.id=c.product_id WHERE c.user_id=? ORDER BY p.id", (user_id,))]

    def set_cart(self, user_id, product_id, quantity):
        integer(product_id, 1, 1000000, "ID товара")
        integer(quantity, 0, 99, "Количество")
        with self.transaction() as db:
            if quantity and not db.execute("SELECT 1 FROM products WHERE id=? AND active=1", (product_id,)).fetchone():
                raise ValueError("Товар недоступен")
            if quantity:
                db.execute("INSERT INTO cart VALUES(?,?,?) ON CONFLICT(user_id,product_id) DO UPDATE SET quantity=excluded.quantity", (user_id, product_id, quantity))
            else:
                db.execute("DELETE FROM cart WHERE user_id=? AND product_id=?", (user_id, product_id))
        return self.cart(user_id)

    def coupons(self, user_id):
        with self.transaction() as db:
            orders = self.orders()
            return [self._coupon_state(dict(r), orders, user_id) for r in db.execute(
                "SELECT c.* FROM coupons c JOIN user_coupons u ON c.code=u.code WHERE u.user_id=?", (user_id,))]

    @staticmethod
    def _coupon_state(coupon, orders, user_id=None):
        uses = [o for o in orders if o.get("coupon") == coupon["code"] and o["status"] != "error"]
        used_count = len(uses)
        own_count = sum(o["user_id"] == user_id for o in uses)
        reason = None
        if not coupon["active"]:
            reason = "Купон отключён"
        elif coupon["expires_on"] and shop_today() > coupon["expires_on"]:
            reason = "Срок действия купона истёк"
        elif coupon["max_uses"] is not None and used_count >= coupon["max_uses"]:
            reason = "Общий лимит использований купона исчерпан"
        elif user_id is not None and coupon["per_user_limit"] is not None and own_count >= coupon["per_user_limit"]:
            reason = "Вы уже использовали доступное количество применений купона"
        return dict(coupon, used_count=used_count, user_uses=own_count,
                    reserved_count=sum(o["status"] != "completed" for o in uses),
                    remaining=None if coupon["max_uses"] is None else max(0, coupon["max_uses"] - used_count),
                    used=coupon["per_user_limit"] is not None and own_count >= coupon["per_user_limit"],
                    available=reason is None, reason=reason)

    def check_coupon(self, user_id, code):
        code = coupon_code(code)
        with self.transaction() as db:
            row = db.execute("SELECT * FROM coupons WHERE code=?", (code,)).fetchone()
            if not row or (not row["public"] and not db.execute(
                    "SELECT 1 FROM user_coupons WHERE user_id=? AND code=?", (user_id, code)).fetchone()):
                raise ValueError("Купон не найден или недоступен вашему аккаунту")
            coupon = self._coupon_state(dict(row), self.orders(), user_id)
            if not coupon["available"]:
                raise ValueError(coupon["reason"])
            return coupon

    def admin_coupons(self):
        with self.transaction() as db:
            orders = self.orders()
            result = []
            for row in db.execute("SELECT * FROM coupons ORDER BY rowid DESC"):
                coupon = self._coupon_state(dict(row), orders)
                coupon["recipients"] = [r[0] for r in db.execute(
                    "SELECT u.login FROM users u JOIN user_coupons c ON u.id=c.user_id WHERE c.code=? ORDER BY u.login", (row["code"],))]
                result.append(coupon)
            return result

    def grant_coupon(self, data):
        code = coupon_code(data.get("code"))
        login = data.get("login", "")
        if not isinstance(login, str):
            raise ValueError("Логин получателя должен быть строкой")
        login = login.strip().casefold()
        percent = integer(data.get("percent"), 1, 90, "Скидка купона")
        public = data.get("public", not bool(login))
        active = data.get("active", True)
        if type(public) is not bool or type(active) is not bool:
            raise ValueError("Доступность и включение купона должны быть логическими значениями")
        limits = []
        for key, label in (("max_uses", "Общий лимит"), ("per_user_limit", "Лимит на покупателя")):
            value = data.get(key, 1 if key == "per_user_limit" else None)
            limits.append(None if value is None else integer(value, 1, 1000000, label))
        expires = data.get("expires_on") or None
        if expires is not None:
            if not isinstance(expires, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", expires):
                raise ValueError("Срок действия: дата в формате ГГГГ-ММ-ДД")
            try:
                date.fromisoformat(expires)
            except ValueError:
                raise ValueError("Указана несуществующая дата") from None
        original = data.get("original_code")
        if original is not None and coupon_code(original) != code:
            raise ValueError("Код существующего купона менять нельзя; создайте новый купон")
        with self.transaction() as db:
            old = db.execute("SELECT * FROM coupons WHERE code=?", (code,)).fetchone()
            if original is not None and not old:
                raise ValueError("Купон для редактирования не найден")
            if original is None and old:
                raise ValueError("Такой код уже существует. Откройте его для редактирования")
            user = db.execute("SELECT id FROM users WHERE login=?", (login,)).fetchone() if login else None
            if login and not user:
                raise ValueError("Пользователь не найден")
            if not public and not user and not (old and db.execute("SELECT 1 FROM user_coupons WHERE code=?", (code,)).fetchone()):
                raise ValueError("Для личного купона укажите логин получателя")
            if public and login:
                raise ValueError("Для общего купона логин получателя не требуется")
            db.execute("""INSERT INTO coupons(code,percent,active,public,max_uses,per_user_limit,expires_on)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET percent=excluded.percent,
                active=excluded.active, public=excluded.public, max_uses=excluded.max_uses,
                per_user_limit=excluded.per_user_limit, expires_on=excluded.expires_on""",
                (code, percent, int(active), int(public), *limits, expires))
            if user:
                db.execute("INSERT OR IGNORE INTO user_coupons VALUES(?,?)", (user["id"], code))
            return next(c for c in self.admin_coupons() if c["code"] == code)

    def orders(self, user_id=None):
        with self.transaction() as db:
            rows = db.execute("SELECT document FROM orders" + (" WHERE user_id=?" if user_id is not None else "") + " ORDER BY rowid DESC", (user_id,) if user_id is not None else ())
            return [json.loads(r[0]) for r in rows]

    def order(self, order_id):
        with self.transaction() as db:
            row = db.execute("SELECT document FROM orders WHERE id=?", (order_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def update_order(self, order_id, **changes):
        with self.transaction() as db:
            order = self.order(order_id)
            order.update(changes)
            db.execute("UPDATE orders SET document=? WHERE id=?", (encode(order), order_id))
            return order

    @staticmethod
    def public_order(order):
        return {k: v for k, v in order.items() if k not in {"handle", "payload"}}

    def create_order(self, user_id, key, coupon="", items=None, priority=3, load="light", batch_id=None):
        if not isinstance(key, str) or not 8 <= len(key) <= 100:
            raise ValueError("Нужен ключ запроса длиной 8-100 символов")
        if not isinstance(coupon, str) or len(coupon) > 20:
            raise ValueError("Некорректный купон")
        with self.transaction() as db:
            existing = db.execute("SELECT document FROM orders WHERE user_id=? AND request_key=?", (user_id, key)).fetchone()
            if existing:
                return json.loads(existing[0])
            if sum(o["status"] not in FINAL for o in self.orders(user_id) if not o["batch_id"]) >= 20 and not batch_id:
                raise ValueError("Дождитесь завершения предыдущих заказов")
            selected = self.cart(user_id) if items is None else items
            if not selected:
                raise ValueError("Корзина пуста")
            if any(not p["active"] for p in selected):
                raise ValueError("В корзине есть недоступный товар. Удалите его перед оформлением")
            discount = 0
            if coupon:
                # Проверка и создание заказа идут под одной блокировкой и в одной транзакции.
                found = self.check_coupon(user_id, coupon)
                coupon = found["code"]
                discount = found["percent"]
            snapshot = [{"id": p["id"], "name": p["name"], "unit": p["unit"], "price": p["cents"] / 100,
                         "quantity": p["quantity"], "discount_percent": p["discount"]} for p in selected]
            order_id = uuid.uuid4().hex
            login = db.execute("SELECT login FROM users WHERE id=?", (user_id,)).fetchone()[0]
            order = {"id": order_id, "user_id": user_id, "login": login, "batch_id": batch_id,
                     "items": snapshot, "coupon": coupon, "coupon_percent": discount, "priority": priority,
                     "load": load, "status": "pending", "phase": "sum", "created_at": time.time(),
                     "finished_at": None, "result": None, "digest": None, "error": None, "history": [],
                     "handle": None, "payload": {"items": snapshot, "discount_percent": discount, "work_iterations": LOADS[load]}}
            db.execute("INSERT INTO orders VALUES(?,?,?,?,?)", (order_id, user_id, key, batch_id, encode(order)))
            if items is None:
                db.execute("DELETE FROM cart WHERE user_id=?", (user_id,))
            return order

    def batches(self):
        with self.transaction() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT document FROM batches ORDER BY rowid DESC")]

    def create_batch(self, user_id, data):
        count = integer(data.get("count"), 1, 200, "Всего заказов")
        concurrency = integer(data.get("concurrency"), 1, 30, "Одновременно в обработке")
        low = integer(data.get("priority_min"), 0, 9, "Минимальный приоритет")
        high = integer(data.get("priority_max"), low, 9, "Максимальный приоритет")
        load = data.get("load")
        if load not in (*LOADS, "mixed"):
            raise ValueError("Нагрузка: light, medium, heavy или mixed")
        with self.transaction() as db:
            if any(o["batch_id"] and o["status"] not in FINAL for o in self.orders()):
                raise ValueError("Дождитесь завершения текущей серии или остановите её")
            products = self.products()
            if not products:
                raise ValueError("В каталоге нет доступных товаров")
            batch = {"id": uuid.uuid4().hex, "count": count, "concurrency": min(count, concurrency),
                     "priority_min": low, "priority_max": high, "load": load, "created_at": time.time(), "stopped": False}
            db.execute("INSERT INTO batches VALUES(?,?)", (batch["id"], encode(batch)))
            for i in range(count):
                selected = [dict(p, quantity=random.randint(1, 5)) for p in random.sample(products, random.randint(1, min(8, len(products))))]
                self.create_order(user_id, f"{batch['id']}-{i}", items=selected,
                                  priority=low + i % (high - low + 1), load=(tuple(LOADS)[i % 3] if load == "mixed" else load), batch_id=batch["id"])
            return batch

    def stop_batch(self, batch_id):
        with self.transaction() as db:
            row = db.execute("SELECT document FROM batches WHERE id=?", (batch_id,)).fetchone()
            if not row:
                raise ValueError("Серия не найдена")
            batch = json.loads(row[0])
            batch["stopped"] = True
            db.execute("UPDATE batches SET document=? WHERE id=?", (encode(batch), batch_id))
            for order in self.orders():
                if order["batch_id"] == batch_id and order["status"] == "pending":
                    self.update_order(order["id"], status="error", error="Серия остановлена администратором", finished_at=time.time())

    def analytics_data(self, operation, include_generated=False):
        orders = [o for o in self.orders() if o["status"] == "completed" and (include_generated or not o["batch_id"])][:1000]
        if operation == "basket_affinity":
            return {"baskets": [[p["name"] for p in o["items"]] for o in orders], "top_k": 20}
        if operation == "count_words":
            return " ".join(p["name"] for o in orders for p in o["items"])[:100000]
        if operation == "sort":
            return {"items": [{"name": p["name"], "price": p["cents"] / 100} for p in self.products()], "key": "price", "reverse": True}
        raise ValueError("Неизвестный вид аналитики")


class OrderProcessor:
    """Окно параллельных заказов; каждый заказ проходит sum, затем hash по TCP."""
    def __init__(self, store, client):
        self.store, self.client = store, client
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.active = {}
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=12)
        with self.lock:
            threads = list(self.active.values())
        for thread in threads:
            thread.join(timeout=12)

    def run(self):
        while not self.stop.is_set():
            with self.lock:
                self.active = {key: thread for key, thread in self.active.items() if thread.is_alive()}
                # Та же блокировка защищает остановку серии от выдачи нового заказа.
                with self.store.lock:
                    orders = self.store.orders()
                    batches = {b["id"]: b for b in self.store.batches()}
                    counts = {}
                    for order in orders:
                        if order["id"] in self.active:
                            counts[order["batch_id"]] = counts.get(order["batch_id"], 0) + 1
                    for order in reversed(orders):
                        if len(self.active) >= 40:
                            break
                        if order["status"] in FINAL or order["id"] in self.active:
                            continue
                        batch_id = order["batch_id"]
                        if batch_id and counts.get(batch_id, 0) >= batches[batch_id]["concurrency"]:
                            continue
                        self.store.update_order(order["id"], status="queued")
                        thread = threading.Thread(target=self.process, args=(order["id"],), daemon=True)
                        self.active[order["id"]] = thread
                        counts[batch_id] = counts.get(batch_id, 0) + 1
                        thread.start()
            self.stop.wait(0.1)

    def process(self, order_id):
        try:
            while not self.stop.is_set():
                order = self.store.order(order_id)
                phase = order["phase"]
                handle = order.get("handle")
                if not handle:
                    data = order["payload"] if phase == "sum" else order["receipt_text"]
                    handle = self.client.submit(phase, data, order["priority"])
                    self.store.update_order(order_id, handle=handle)
                final = None
                version = max((e["version"] for e in order["history"] if e["task_id"] == handle["task_id"]), default=0)
                try:
                    for event in self.client.subscribe(handle, after_version=version, stop=self.stop):
                        order = self.store.order(order_id)
                        history = order["history"]
                        if not any(e["task_id"] == event["task_id"] and e["version"] == event["version"] for e in history):
                            history = history + [dict(event, phase=phase)]
                        # Финальный статус заказа записывается ниже вместе с временем и результатом.
                        self.store.update_order(order_id, history=history[-100:], status=("running" if event["status"] in FINAL else event["status"]), error=event.get("error"))
                        if event["status"] in FINAL:
                            final = event
                            break
                except (OSError, EOFError):
                    # Токен сохранён: переподписываемся без повторной отправки задания.
                    self.stop.wait(1)
                    continue
                if final is None:
                    return
                if final["status"] == "error":
                    self.store.update_order(order_id, status="error", error=final.get("error"), finished_at=time.time())
                    return
                if phase == "sum":
                    receipt = encode({"order_id": order_id, "items": order["items"], "coupon": order["coupon"], "totals": final["result"]})
                    self.store.update_order(order_id, result=final["result"], phase="hash", handle=None, receipt_text=receipt)
                else:
                    self.store.update_order(order_id, status="completed", digest=final["result"]["digest"], finished_at=time.time(), error=None)
                    return
        except (OSError, EOFError, ValueError) as exc:
            if not self.stop.is_set():
                self.store.update_order(order_id, status="error", error=f"Не удалось обработать заказ: {exc}", finished_at=time.time())
