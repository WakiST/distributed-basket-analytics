"""Проверки магазина, разграничения доступа и генератора с настоящим TCP-брокером."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from basketnet.shop import ShopStore, OrderProcessor, encode
from basketnet.tasks import execute, validate
from basketnet.worker import WorkerClient
from tests import test_web
from tests.test_network import eventually


class ShopTests(unittest.TestCase):
    setUp = test_web.WebTests.setUp
    tearDown = test_web.WebTests.tearDown
    request = test_web.WebTests.request
    new_session = test_web.WebTests.new_session
    worker = test_web.WebTests.worker

    def register(self, login="buyer_one"):
        guest = self.new_session()
        status, body, headers = self.request("POST", "/api/auth/register", {"login": login, "password": "buyer-password", "role": "admin"}, cookie=guest)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["user"]["role"], "customer")
        return headers["Set-Cookie"].split(";")[0]

    def cart(self, cookie, product=1, quantity=2):
        status, body, _ = self.request("POST", "/api/cart", {"product_id": product, "quantity": quantity}, cookie=cookie)
        self.assertEqual(status, 200, body)

    def checkout(self, cookie, key="request-123456", coupon=""):
        status, body, _ = self.request("POST", "/api/orders", {"request_key": key, "coupon": coupon, "priority": 9, "price": 0, "work_iterations": 2000000}, cookie=cookie)
        self.assertEqual(status, 201, body)
        return body

    def finished(self, order_id):
        return eventually(lambda: (o if (o := self.web.store.order(order_id))["status"] in {"completed", "error"} else None), timeout=10)

    def generate(self, **changes):
        data = dict(count=10, concurrency=10, load="light", priority_min=0, priority_max=9)
        data.update(changes)
        status, body, _ = self.request("POST", "/api/admin/generate", data)
        self.assertEqual(status, 201, body)
        return body

    def test_catalog_public_cart_requires_login_and_admin_is_protected(self):
        guest = self.new_session()
        self.assertEqual(len(self.request("GET", "/api/catalog", cookie=guest)[1]["products"]), 12)
        self.assertEqual(self.request("POST", "/api/cart", {"product_id": 1, "quantity": 1}, cookie=guest)[0], 401)
        customer = self.register()
        for path in ("/api/admin/state", "/api/monitor"):
            self.assertEqual(self.request("GET", path, cookie=customer)[0], 403)
        for path in ("/api/tasks", "/api/admin/generate", "/api/admin/product", "/api/admin/coupon", "/api/admin/stop", "/api/admin/analytics"):
            self.assertEqual(self.request("POST", path, {}, cookie=customer)[0], 403)
        self.assertEqual(self.request("GET", "/api/session", cookie=customer)[1]["operations"], [])

    def test_registration_login_rotation_logout_and_password_hash(self):
        customer = self.register()
        self.assertEqual(self.request("POST", "/api/auth/login", {"login": "buyer_one", "password": "wrong"}, cookie=customer)[0], 400)
        self.cart(customer)
        status, _, headers = self.request("POST", "/api/auth/logout", {}, cookie=customer)
        self.assertEqual(status, 200)
        self.assertEqual(self.request("GET", "/api/cart", cookie=customer)[0], 401)
        guest = headers["Set-Cookie"].split(";")[0]
        status, _, headers = self.request("POST", "/api/auth/login", {"login": "BUYER_ONE", "password": "buyer-password"}, cookie=guest)
        self.assertEqual(status, 200)
        new_cookie = headers["Set-Cookie"].split(";")[0]
        self.assertNotEqual(new_cookie, guest)
        self.assertEqual(self.request("GET", "/api/cart", cookie=new_cookie)[1][0]["quantity"], 2)
        with self.web.store.transaction() as db:
            row = db.execute("SELECT password,salt FROM users WHERE login='buyer_one'").fetchone()
            self.assertNotEqual(row["password"], "buyer-password")
            self.assertEqual(len(row["salt"]), 32)

    def test_order_server_prices_discounts_hash_and_coupon_single_use(self):
        self.worker()
        customer = self.register()
        self.cart(customer)
        order = self.checkout(customer, coupon="WELCOME10")
        self.assertEqual(order["priority"], 3)
        self.assertEqual(order["load"], "light")
        self.assertNotIn("handle", order)
        final = self.finished(order["id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["result"]["subtotal"], "13.80")
        self.assertEqual(final["result"]["item_discount"], "1.38")
        self.assertEqual(final["result"]["order_discount"], "1.24")
        self.assertEqual(final["result"]["total"], "11.18")
        self.assertEqual(final["digest"], hashlib.sha256(final["receipt_text"].encode()).hexdigest())
        self.assertEqual({e["phase"] for e in final["history"]}, {"sum", "hash"})
        self.assertEqual(self.request("GET", "/api/cart", cookie=customer)[1], [])
        self.cart(customer)
        status, _, _ = self.request("POST", "/api/orders", {"request_key": "second-request", "coupon": "WELCOME10"}, cookie=customer)
        self.assertEqual(status, 400)
        self.assertEqual(self.request("GET", "/api/coupons", cookie=customer)[1][0]["used"], True)

    def test_concurrent_checkout_idempotency_and_order_isolation(self):
        customer = self.register()
        other = self.register("buyer_two")
        self.cart(customer)
        with ThreadPoolExecutor(max_workers=5) as pool:
            orders = list(pool.map(lambda _: self.checkout(customer), range(5)))
        self.assertEqual(len({o["id"] for o in orders}), 1)
        self.assertEqual(len(self.web.store.orders()), 1)
        self.assertEqual(self.request("GET", "/api/orders", cookie=other)[1], [])
        self.assertEqual(self.request("GET", "/api/cart", cookie=other)[1], [])

    def test_admin_product_changes_do_not_rewrite_receipts_and_personal_coupons(self):
        customer = self.register()
        self.cart(customer)
        order = self.checkout(customer)
        product = self.web.store.products()[0]
        product.update(cents=999, discount=50, active=True)
        self.assertEqual(self.request("POST", "/api/admin/product", product)[0], 200)
        self.assertEqual(self.web.store.order(order["id"])["items"][0]["price"], 6.9)
        self.assertEqual(self.request("GET", "/api/catalog")[1]["products"][0]["cents"], 999)
        self.assertEqual(self.request("POST", "/api/admin/coupon", {"login": "buyer_one", "code": "GIFT15", "percent": 15})[0], 200)
        self.assertIn("GIFT15", [c["code"] for c in self.request("GET", "/api/coupons", cookie=customer)[1]])

    def test_generator_priorities_are_executed_by_real_worker(self):
        batch = self.generate()
        eventually(lambda: self.broker.monitor()["tasks"]["queued"] == 10)
        self.worker()
        eventually(lambda: all(o["status"] == "completed" for o in self.web.store.orders()), timeout=12)
        events = [e for o in self.web.store.orders() for e in o["history"] if e["phase"] == "sum" and e["status"] == "running"]
        self.assertEqual([e["priority"] for e in sorted(events, key=lambda e: e["timestamp"])], list(range(9, -1, -1)))
        self.assertEqual(len(self.web.store.orders()), batch["count"])
        self.assertEqual(self.web.store.analytics_data("basket_affinity")["baskets"], [])
        self.assertEqual(len(self.web.store.analytics_data("basket_affinity", True)["baskets"]), 10)

    def test_generator_concurrency_limits_and_stop_only_pending(self):
        self.generate(count=8, concurrency=2, load="mixed")
        eventually(lambda: self.broker.monitor()["tasks"]["queued"] == 2)
        orders = self.web.store.orders()
        self.assertEqual(sum(o["status"] == "pending" for o in orders), 6)
        self.assertEqual({o["load"] for o in orders}, {"light", "medium", "heavy"})
        self.assertEqual(self.request("POST", "/api/admin/stop", {"batch_id": orders[0]["batch_id"]})[0], 200)
        self.assertEqual(sum(o["status"] == "error" for o in self.web.store.orders()), 6)
        self.worker()
        eventually(lambda: sum(o["status"] == "completed" for o in self.web.store.orders()) == 2, timeout=12)

    def test_orders_resume_after_processor_restart_and_failover(self):
        customer = self.register()
        self.cart(customer)
        order = self.checkout(customer)
        eventually(lambda: self.web.store.order(order["id"]).get("handle"))
        old_handle = self.web.store.order(order["id"])["handle"]
        self.web.processor.close()
        self.web.processor = OrderProcessor(self.web.store, self.web.client)
        worker = WorkerClient(port=self.broker.worker_port, worker_id="slow", delay=1, reconnect=False)
        thread = threading.Thread(target=worker.run, daemon=True)
        self.workers.append((worker, thread))
        thread.start()
        eventually(lambda: any(e["status"] == "running" for e in self.web.store.order(order["id"])["history"]))
        worker.close()
        self.worker()
        final = self.finished(order["id"])
        self.assertEqual(final["status"], "completed")
        sum_events = [e for e in final["history"] if e["phase"] == "sum"]
        self.assertTrue(all(e["task_id"] == old_handle["task_id"] for e in sum_events))
        self.assertEqual(max(e["attempt"] for e in sum_events), 2)

    def test_sse_delivers_customer_order_completion(self):
        customer = self.register()
        self.cart(customer)
        self.checkout(customer)
        connection = http.client.HTTPConnection("127.0.0.1", self.web.server_port, timeout=7)
        self.connections.append(connection)
        connection.request("GET", "/api/events", headers={"Cookie": customer})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.worker()
        event_type = ""
        while True:
            line = response.readline().decode()
            self.assertTrue(line)
            if line.startswith("event: "):
                event_type = line[7:].strip()
            if line.startswith("data: ") and event_type == "orders":
                orders = json.loads(line[6:])
                if orders and orders[0]["status"] == "completed":
                    self.assertNotIn("handle", orders[0])
                    break
        response.close()

    def test_generator_input_validation(self):
        for patch in ({"count": 201}, {"concurrency": 0}, {"priority_min": 10}, {"priority_min": 8, "priority_max": 2}, {"load": "unknown"}, {"count": True}):
            data = dict(count=3, concurrency=2, priority_min=0, priority_max=9, load="light")
            data.update(patch)
            self.assertEqual(self.request("POST", "/api/admin/generate", data)[0], 400)
        self.assertEqual(self.web.store.orders(), [])


class StorageTests(unittest.TestCase):
    def test_data_survives_reopening_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shop.sqlite3"
            store = ShopStore(path, admin_password="test-password")
            user = store.register("buyer", "buyer-password")
            store.set_cart(user["id"], 1, 3)
            order = store.create_order(user["id"], "persistent-key", "WELCOME10")
            store.set_cart(user["id"], 2, 1)
            store.close()
            store = ShopStore(path)
            try:
                self.assertEqual(store.login("buyer", "buyer-password"), user)
                self.assertEqual(store.cart(user["id"])[0]["id"], 2)
                self.assertEqual(store.order(order["id"]), order)
                self.assertTrue(store.coupons(user["id"])[0]["used"])
                self.assertFalse(Path(str(path) + ".admin.txt").exists())
            finally:
                store.close()

    def test_cpu_load_does_not_change_receipt_totals(self):
        data = {"items": [{"price": 6.9, "quantity": 2, "discount_percent": 10}], "discount_percent": 10}
        simple = execute("sum", data)
        loaded = execute("sum", dict(data, work_iterations=1000))
        self.assertEqual({k: loaded[k] for k in simple}, simple)
        self.assertEqual(len(loaded["work_digest"]), 64)
        for invalid in (-1, 2000001, True, 1.5):
            with self.assertRaises(ValueError):
                validate("sum", dict(data, work_iterations=invalid))


if __name__ == "__main__":
    unittest.main()
