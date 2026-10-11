import hashlib
import unittest

from basketnet.tasks import execute, validate


class TaskTests(unittest.TestCase):
    def test_sum_numeric_and_empty(self):
        self.assertEqual(execute("sum", [1, 2, 3]), 6)
        self.assertEqual(execute("sum", []), 0)
        self.assertAlmostEqual(execute("sum", [0.1, 0.2]), 0.3)

    def test_receipt_rounding_and_sequential_discounts(self):
        result = execute("sum", {"items": [{"price": 3.49, "quantity": 2, "discount_percent": 10},
                                          {"price": 1.85}], "discount_percent": 5})
        self.assertEqual(result, {"subtotal": "8.83", "item_discount": "0.70", "order_discount": "0.41",
                                  "discount": "1.11", "total": "7.72"})
        self.assertEqual(execute("sum", {"items": []})["total"], "0.00")

    def test_sort_stable_by_margin(self):
        items = [{"id": 1, "margin": 2}, {"id": 2, "margin": 5}, {"id": 3, "margin": 2}]
        self.assertEqual([x["id"] for x in execute("sort", {"items": items, "key": "margin", "reverse": True})], [2, 1, 3])
        self.assertEqual(execute("sort", [4, -2, 1]), [-2, 1, 4])

    def test_utf8_hash(self):
        self.assertEqual(execute("hash", "Чек №1")["digest"], hashlib.sha256("Чек №1".encode()).hexdigest())

    def test_unicode_word_counts(self):
        self.assertEqual(execute("count_words", "Молоко, хлеб! молоко."),
                         {"total": 3, "unique": 2, "frequencies": {"молоко": 2, "хлеб": 1}})

    def test_basket_affinity_uses_unique_products_per_receipt(self):
        result = execute("basket_affinity", [["a", "a", "b"], ["a"], ["b", "c"], []])
        pair = result["pairs"][0]
        self.assertEqual(pair["products"], ["a", "b"])
        self.assertEqual(pair["count"], 1)
        self.assertEqual(pair["support"], 0.25)
        self.assertEqual(pair["confidence_a_to_b"], 0.5)
        self.assertEqual(pair["confidence_b_to_a"], 0.5)
        self.assertEqual(pair["lift"], 1)
        self.assertEqual(execute("basket_affinity", [])["pairs"], [])
        self.assertEqual(execute("basket_affinity", {"baskets": [["a", "b"], ["a"]], "min_support": 0.6})["pairs"], [])

    def test_combinatorics_string_precision(self):
        self.assertEqual(execute("fib", 0), "0")
        self.assertEqual(execute("fib", 100), "354224848179261915075")
        self.assertEqual(execute("factorial", 0), "1")
        self.assertEqual(execute("factorial", 20), "2432902008176640000")
        self.assertEqual(len(execute("factorial", 1000)), 2568)

    def test_invalid_data(self):
        cases = [("sum", [True]), ("sum", [float("nan")]), ("sum", [float("inf")]),
                 ("sum", {"items": [{"price": -1}]}), ("sum", {"items": [], "discount_percent": 101}),
                 ("sort", {"items": [{"name": "x"}]}), ("hash", 12), ("count_words", []),
                 ("fib", -1), ("fib", True), ("factorial", 1001), ("basket_affinity", [[""]]),
                 ("basket_affinity", {"baskets": [], "top_k": 0}), ("unknown", [])]
        for operation, data in cases:
            with self.subTest(operation=operation, data=data):
                with self.assertRaises(ValueError):
                    validate(operation, data)


if __name__ == "__main__":
    unittest.main()
