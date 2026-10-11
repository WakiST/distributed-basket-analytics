"""Валидация и чистые вычисления для семи типов заданий."""

from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from itertools import combinations
import hashlib
import math
import re

OPERATIONS = ("sum", "sort", "hash", "count_words", "basket_affinity", "fib", "factorial")
MAX_ITEMS = 10000


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Ожидается число")
    if abs(value) > 1e12 or not math.isfinite(value):
        raise ValueError("Число должно быть конечным и по модулю не больше 10¹²")
    return value


def array(value):
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise ValueError(f"Ожидается массив длиной до {MAX_ITEMS}")
    return value


def percent(value):
    if not 0 <= number(value) <= 100:
        raise ValueError("Скидка должна быть от 0 до 100 процентов")


def validate(operation, data):
    if operation not in OPERATIONS:
        raise ValueError("Неизвестный тип задания")
    if operation == "sum":
        if isinstance(data, list):
            for value in array(data):
                number(value)
        elif isinstance(data, dict):
            percent(data.get("discount_percent", 0))
            for item in array(data.get("items")):
                if not isinstance(item, dict):
                    raise ValueError("Позиция чека должна быть объектом")
                if number(item.get("price")) < 0 or number(item.get("quantity", 1)) <= 0:
                    raise ValueError("Цена неотрицательная, количество положительное")
                if item.get("price", 0) > 1e9 or item.get("quantity", 1) > 1e6:
                    raise ValueError("Цена до 10⁹, количество до 10⁶")
                percent(item.get("discount_percent", 0))
        else:
            raise ValueError("sum принимает массив чисел или объект с items")
    elif operation == "sort":
        values = array(data.get("items")) if isinstance(data, dict) else array(data)
        if isinstance(data, dict):
            key = data.get("key", "price")
            if not isinstance(key, str) or not isinstance(data.get("reverse", False), bool):
                raise ValueError("key — строка, reverse — логическое значение")
            for item in values:
                if not isinstance(item, dict):
                    raise ValueError("items должен содержать объекты")
                number(item.get(key))
        else:
            for value in values:
                number(value)
    elif operation in ("hash", "count_words"):
        if not isinstance(data, str) or len(data.encode("utf-8")) > 500000:
            raise ValueError("Ожидается текст до 500 000 байт UTF-8")
        if operation == "count_words" and len(data) > 100000:
            raise ValueError("Частотный анализ: до 100 000 символов")
    elif operation in ("fib", "factorial"):
        limit = 10000 if operation == "fib" else 1000
        if type(data) is not int or not 0 <= data <= limit:
            raise ValueError(f"N должно быть целым числом от 0 до {limit}")
    else:
        baskets = array(data.get("baskets")) if isinstance(data, dict) else array(data)
        total = 0
        for basket in baskets:
            if not isinstance(basket, list) or len(basket) > 100:
                raise ValueError("В чеке допускается до 100 товаров")
            for item in basket:
                if not isinstance(item, str) or not item.strip() or len(item) > 120:
                    raise ValueError("Название товара — непустая строка до 120 символов")
            total += len(basket)
        if total > 20000:
            raise ValueError("Допускается до 20 000 позиций во всех чеках")
        if isinstance(data, dict):
            if not 0 <= number(data.get("min_support", 0)) <= 1:
                raise ValueError("min_support должен быть от 0 до 1")
            if type(data.get("top_k", 20)) is not int or not 1 <= data.get("top_k", 20) <= 1000:
                raise ValueError("top_k должен быть целым числом от 1 до 1000")


def execute(operation, data):
    validate(operation, data)
    if operation == "sum":
        if isinstance(data, list):
            return sum(data) if all(type(x) is int for x in data) else math.fsum(data)
        cent = Decimal("0.01")
        hundred = Decimal(100)
        gross = Decimal(0)
        item_discounts = Decimal(0)
        for item in data["items"]:
            line = (Decimal(str(item["price"])) * Decimal(str(item.get("quantity", 1)))).quantize(cent, ROUND_HALF_UP)
            gross += line
            item_discounts += (line * Decimal(str(item.get("discount_percent", 0))) / hundred).quantize(cent, ROUND_HALF_UP)
        order_discount = ((gross - item_discounts) * Decimal(str(data.get("discount_percent", 0))) / hundred).quantize(cent, ROUND_HALF_UP)
        return {"subtotal": str(gross.quantize(cent)), "item_discount": str(item_discounts.quantize(cent)),
                "order_discount": str(order_discount), "discount": str((item_discounts + order_discount).quantize(cent)),
                "total": str((gross - item_discounts - order_discount).quantize(cent))}
    if operation == "sort":
        if isinstance(data, list):
            return sorted(data)
        return sorted(data["items"], key=lambda item: item[data.get("key", "price")], reverse=data.get("reverse", False))
    if operation == "hash":
        return {"algorithm": "sha256", "digest": hashlib.sha256(data.encode("utf-8")).hexdigest()}
    if operation == "count_words":
        counts = Counter(re.findall(r"[^\W_]+(?:[-'][^\W_]+)*", data.casefold(), re.UNICODE))
        return {"total": sum(counts.values()), "unique": len(counts),
                "frequencies": dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))}
    if operation == "fib":
        a, b = 0, 1
        for _ in range(data):
            a, b = b, a + b
        return str(a)
    if operation == "factorial":
        return str(math.factorial(data))
    baskets = data["baskets"] if isinstance(data, dict) else data
    minimum = data.get("min_support", 0) if isinstance(data, dict) else 0
    top_k = data.get("top_k", 20) if isinstance(data, dict) else 20
    singles, pairs = Counter(), Counter()
    for basket in baskets:
        distinct = sorted(set(basket))
        singles.update(distinct)
        pairs.update(combinations(distinct, 2))
    n = len(baskets)
    ranked = sorted(((pair, count) for pair, count in pairs.items() if count / n >= minimum),
                    key=lambda item: (-item[1], item[0]))[:top_k]
    return {"basket_count": n, "unique_products": len(singles), "pairs": [
        {"products": [a, b], "count": count, "support": round(count / n, 6),
         "confidence_a_to_b": round(count / singles[a], 6),
         "confidence_b_to_a": round(count / singles[b], 6),
         "lift": round(count * n / (singles[a] * singles[b]), 6)}
        for (a, b), count in ranked]}
