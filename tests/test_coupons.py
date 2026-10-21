"""Лимиты, сроки, миграция и одновременное применение купонов."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from basketnet.shop import ShopStore
from tests import test_shop


class CouponHTTPTests(unittest.TestCase):
    setUp = test_shop.ShopTests.setUp
    tearDown = test_shop.ShopTests.tearDown
    request = test_shop.ShopTests.request
    new_session = test_shop.ShopTests.new_session
    register = test_shop.ShopTests.register
    cart = test_shop.ShopTests.cart
    checkout = test_shop.ShopTests.checkout

    def save(self, **changes):
        data = dict(code="SALE15", percent=15, public=True, active=True, max_uses=10,
                    per_user_limit=2, expires_on="2099-12-31")
        data.update(changes)
        status, result, _ = self.request("POST", "/api/admin/coupon", data)
        self.assertEqual(status, 200, result)
        return result

    def test_public_code_check_edit_and_historical_discount(self):
        customer = self.register()
        self.save()
        status, coupon, _ = self.request("POST", "/api/coupons/check", {"code": " sale15 "}, cookie=customer)
        self.assertEqual(status, 200)
        self.assertEqual(coupon["percent"], 15)
        self.assertEqual(coupon["used_count"], 0)
        self.cart(customer)
        order = self.checkout(customer, coupon="sale15")
        self.assertEqual(order["coupon"], "SALE15")
        self.save(original_code="SALE15", percent=25, max_uses=20)
        self.assertEqual(self.web.store.order(order["id"])["coupon_percent"], 15)
        self.assertEqual(self.request("POST", "/api/coupons/check", {"code": "SALE15"}, cookie=customer)[1]["percent"], 25)
        admin = self.request("GET", "/api/admin/state")[1]
        saved = next(c for c in admin["coupons"] if c["code"] == "SALE15")
        self.assertEqual(saved["used_count"], 1)
        self.assertEqual(saved["max_uses"], 20)

    def test_final_global_slot_cannot_be_spent_twice_and_error_releases_it(self):
        first, second = self.register(), self.register("buyer_two")
        self.save(max_uses=1, per_user_limit=None)
        self.cart(first)
        self.cart(second)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda cookie: self.request("POST", "/api/orders", {"request_key": "last-slot-key", "coupon": "SALE15"}, cookie=cookie), (first, second)))
        self.assertEqual(sorted(r[0] for r in results), [201, 400])
        order = next(r[1] for r in results if r[0] == 201)
        loser = second if results[0][0] == 201 else first
        self.web.processor.close()
        self.web.store.update_order(order["id"], status="error", error="Тестовый отказ")
        self.assertEqual(self.request("POST", "/api/coupons/check", {"code": "SALE15"}, cookie=loser)[0], 200)
        self.checkout(loser, key="retry-slot-key", coupon="SALE15")

    def test_per_customer_limit_and_checkout_revalidates(self):
        customer = self.register()
        self.save(max_uses=None, per_user_limit=2)
        for i in range(2):
            self.cart(customer)
            self.checkout(customer, key=f"checkout-key-{i}", coupon="SALE15")
        self.cart(customer)
        self.assertEqual(self.request("POST", "/api/orders", {"request_key": "third-attempt", "coupon": "SALE15"}, cookie=customer)[0], 400)
        self.save(original_code="SALE15", max_uses=None, per_user_limit=3)
        self.assertEqual(self.request("POST", "/api/coupons/check", {"code": "SALE15"}, cookie=customer)[0], 200)
        self.save(original_code="SALE15", active=False)
        self.assertEqual(self.request("POST", "/api/orders", {"request_key": "disabled-code", "coupon": "SALE15"}, cookie=customer)[0], 400)
        self.assertTrue(self.request("GET", "/api/cart", cookie=customer)[1])

    def test_expiry_inclusive_and_existing_request_is_idempotent(self):
        customer = self.register()
        self.save(expires_on="2030-05-10")
        self.cart(customer)
        with patch("basketnet.shop.shop_today", return_value="2030-05-10"):
            self.assertEqual(self.request("POST", "/api/coupons/check", {"code": "SALE15"}, cookie=customer)[0], 200)
            order = self.checkout(customer, coupon="SALE15")
        with patch("basketnet.shop.shop_today", return_value="2030-05-11"):
            self.assertEqual(self.request("POST", "/api/coupons/check", {"code": "SALE15"}, cookie=customer)[0], 400)
            self.assertEqual(self.checkout(customer, coupon="SALE15")["id"], order["id"])
            self.cart(customer)
            status, body, _ = self.request("POST", "/api/orders", {"request_key": "expired-attempt", "coupon": "SALE15"}, cookie=customer)
            self.assertEqual(status, 400)
            self.assertIn("истёк", body["error"])

    def test_private_code_does_not_become_public_and_permissions(self):
        first, second = self.register(), self.register("buyer_two")
        self.save(public=False, login="buyer_one")
        self.assertEqual(self.request("POST", "/api/coupons/check", {"code": "SALE15"}, cookie=first)[0], 200)
        self.assertEqual(self.request("POST", "/api/coupons/check", {"code": "SALE15"}, cookie=second)[0], 400)
        self.assertEqual(self.request("POST", "/api/admin/coupon", {"code": "FAKE", "percent": 90}, cookie=second)[0], 403)
        self.assertEqual(self.request("POST", "/api/coupons/check", {"code": "SALE15"}, cookie=self.new_session())[0], 401)

    def test_invalid_rules_duplicates_and_missing_edit_are_rejected(self):
        base = dict(code="RULES10", percent=10)
        for changes in ({"max_uses": 0}, {"per_user_limit": True}, {"expires_on": "2030-02-30"},
                        {"expires_on": "tomorrow"}, {"public": "yes"}, {"percent": 100},
                        {"original_code": "RULES10"}, {"public": False}):
            self.assertEqual(self.request("POST", "/api/admin/coupon", dict(base, **changes))[0], 400)
        self.save()
        self.assertEqual(self.request("POST", "/api/admin/coupon", {"code": "SALE15", "percent": 5})[0], 400)
        self.assertEqual(self.request("POST", "/api/admin/coupon", {"code": "NEW15", "original_code": "SALE15", "percent": 15})[0], 400)


class CouponStorageTests(unittest.TestCase):
    def test_legacy_migration_preserves_personal_uses_and_new_rules_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shop.sqlite3"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE coupons(code TEXT PRIMARY KEY, percent INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1)")
                db.execute("INSERT INTO coupons VALUES('LEGACY10',10,1)")
            db.close()
            store = ShopStore(path, admin_password="test-password")
            user = store.register("buyer", "buyer-password")
            with store.transaction() as db:
                db.execute("INSERT INTO user_coupons VALUES(?, 'LEGACY10')", (user["id"],))
            store.set_cart(user["id"], 1, 1)
            order = store.create_order(user["id"], "legacy-order", "LEGACY10")
            store.grant_coupon(dict(code="NEW20", percent=20, public=True, max_uses=8, per_user_limit=3, expires_on="2099-10-01"))
            store.close()
            store = ShopStore(path)
            try:
                legacy = next(c for c in store.admin_coupons() if c["code"] == "LEGACY10")
                self.assertFalse(legacy["public"])
                self.assertEqual(legacy["per_user_limit"], 1)
                self.assertEqual(legacy["used_count"], 1)
                self.assertEqual(store.order(order["id"])["coupon_percent"], 10)
                with self.assertRaises(ValueError):
                    store.check_coupon(user["id"], "LEGACY10")
                new = store.check_coupon(user["id"], "NEW20")
                self.assertEqual((new["max_uses"], new["per_user_limit"], new["expires_on"]), (8, 3, "2099-10-01"))
            finally:
                store.close()
