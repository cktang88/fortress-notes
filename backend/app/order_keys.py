"""Fractional-index sibling order keys (docs/block-model.md §8).

A key is a base-62 string made of an *integer part* (a head letter that says how
many digits follow: ``a``-``z`` for 1-26 digits of a growing positive range,
``A``-``Z`` for a shrinking negative one) and an optional *fraction*. Keys
compare by plain byte order, so SQLite's default collation sorts them. Appending
after the last key increments the integer part, which keeps keys short however
many blocks are appended; inserting between two keys extends the fraction.

This is the algorithm of David Greenspan's ``fractional-indexing`` (used by
Figma and Logseq).
"""

from __future__ import annotations

DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_ZERO = DIGITS[0]
_SMALLEST_INTEGER = "A" + _ZERO * 26
FIRST_KEY = "a" + _ZERO


class OrderKeyError(ValueError):
    pass


def _midpoint(a: str, b: str | None) -> str:
    """A fraction strictly between fractions ``a`` and ``b`` (None: 1)."""

    if b is not None and a >= b:
        raise OrderKeyError(f"{a!r} is not below {b!r}")
    if a[-1:] == _ZERO or (b is not None and b[-1:] == _ZERO):
        raise OrderKeyError("a fraction must not end in zero")
    if b is not None:
        n = 0
        while (a[n] if n < len(a) else _ZERO) == b[n]:
            n += 1
        if n > 0:
            return b[:n] + _midpoint(a[n:], b[n:])
    digit_a = DIGITS.index(a[0]) if a else 0
    digit_b = DIGITS.index(b[0]) if b is not None else len(DIGITS)
    if digit_b - digit_a > 1:
        return DIGITS[(digit_a + digit_b + 1) // 2]
    if b is not None and len(b) > 1:
        return b[:1]
    return DIGITS[digit_a] + _midpoint(a[1:], None)


def _integer_length(head: str) -> int:
    if "a" <= head <= "z":
        return ord(head) - ord("a") + 2
    if "A" <= head <= "Z":
        return ord("Z") - ord(head) + 2
    raise OrderKeyError(f"invalid order key head {head!r}")


def _integer_part(key: str) -> str:
    length = _integer_length(key[0])
    if length > len(key):
        raise OrderKeyError(f"invalid order key {key!r}")
    return key[:length]


def validate(key: str) -> None:
    if not isinstance(key, str) or not key or key == _SMALLEST_INTEGER:
        raise OrderKeyError(f"invalid order key {key!r}")
    integer = _integer_part(key)
    if any(char not in DIGITS for char in key[1:]):
        raise OrderKeyError(f"invalid order key {key!r}")
    if key[len(integer):][-1:] == _ZERO:
        raise OrderKeyError(f"invalid order key {key!r}")


def _increment_integer(value: str) -> str | None:
    head, digits = value[0], list(value[1:])
    for index in reversed(range(len(digits))):
        position = DIGITS.index(digits[index]) + 1
        if position < len(DIGITS):
            digits[index] = DIGITS[position]
            return head + "".join(digits)
        digits[index] = _ZERO
    if head == "Z":
        return "a" + _ZERO
    if head == "z":
        return None
    new_head = chr(ord(head) + 1)
    if new_head > "a":
        digits.append(_ZERO)
    else:
        digits.pop()
    return new_head + "".join(digits)


def _decrement_integer(value: str) -> str | None:
    head, digits = value[0], list(value[1:])
    for index in reversed(range(len(digits))):
        position = DIGITS.index(digits[index]) - 1
        if position >= 0:
            digits[index] = DIGITS[position]
            return head + "".join(digits)
        digits[index] = DIGITS[-1]
    if head == "a":
        return "Z" + DIGITS[-1]
    if head == "A":
        return None
    new_head = chr(ord(head) - 1)
    if new_head < "Z":
        digits.append(DIGITS[-1])
    else:
        digits.pop()
    return new_head + "".join(digits)


def key_between(a: str | None, b: str | None) -> str:
    """A key strictly between ``a`` and ``b`` (None: open end)."""

    if a is not None:
        validate(a)
    if b is not None:
        validate(b)
    if a is not None and b is not None and a >= b:
        raise OrderKeyError(f"{a!r} is not below {b!r}")
    if a is None:
        if b is None:
            return FIRST_KEY
        integer_b = _integer_part(b)
        fraction_b = b[len(integer_b):]
        if integer_b == _SMALLEST_INTEGER:
            return integer_b + _midpoint("", fraction_b)
        if integer_b < b:
            return integer_b
        result = _decrement_integer(integer_b)
        if result is None:
            raise OrderKeyError("cannot go below the smallest key")
        return result
    integer_a = _integer_part(a)
    fraction_a = a[len(integer_a):]
    if b is None:
        incremented = _increment_integer(integer_a)
        return integer_a + _midpoint(fraction_a, None) if incremented is None else incremented
    integer_b = _integer_part(b)
    fraction_b = b[len(integer_b):]
    if integer_a == integer_b:
        return integer_a + _midpoint(fraction_a, fraction_b)
    incremented = _increment_integer(integer_a)
    if incremented is None:
        raise OrderKeyError("cannot go above the largest key")
    if incremented < b:
        return incremented
    return integer_a + _midpoint(fraction_a, None)


def keys_between(a: str | None, b: str | None, count: int) -> list[str]:
    """``count`` ascending keys strictly between ``a`` and ``b``, evenly spread."""

    if count <= 0:
        return []
    if count == 1:
        return [key_between(a, b)]
    if b is None:
        keys = [key_between(a, None)]
        for _ in range(count - 1):
            keys.append(key_between(keys[-1], None))
        return keys
    if a is None:
        keys = [key_between(None, b)]
        for _ in range(count - 1):
            keys.append(key_between(None, keys[-1]))
        keys.reverse()
        return keys
    middle = count // 2
    pivot = key_between(a, b)
    return [*keys_between(a, pivot, middle), pivot, *keys_between(pivot, b, count - middle - 1)]
