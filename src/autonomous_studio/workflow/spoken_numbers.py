"""Extract numerical values from approved speech without inventing quantities."""

from __future__ import annotations

import re
from decimal import Decimal

_DIGITS = re.compile(r"(?<!\w)\d[\d,]*(?:\.\d+)?(?!\w)")
_WORDS = re.compile(r"[a-z]+")
_SMALL = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_SCALES = {
    "thousand": 1_000,
    "million": 1_000_000,
    "billion": 1_000_000_000,
    "trillion": 1_000_000_000_000,
    "quadrillion": 1_000_000_000_000_000,
}


def numeric_literals(text: str) -> set[Decimal]:
    """Numbers written as digits, for checking proposed on-screen copy."""
    return {Decimal(match.group().replace(",", "")) for match in _DIGITS.finditer(text)}


def spoken_number_mentions(text: str) -> list[str]:
    """Return distinct spoken number phrases, not the values derived from them.

    ``five billion`` is one measurement, whereas ``five to six billion``
    contains two. Counting values from :func:`spoken_number_values` would
    mistake a coefficient and its scaled value for two measurements.
    """
    tokens = list(re.finditer(r"\d[\d,]*(?:\.\d+)?|[a-z]+", text.casefold()))
    result: list[str] = []
    index = 0
    while index < len(tokens):
        word = tokens[index].group()
        if word[0].isdigit():
            result.append(text[tokens[index].start():tokens[index].end()])
            index += 1
            continue
        if word not in (_SMALL | _TENS) and not (
            word == "a" and index + 1 < len(tokens)
            and tokens[index + 1].group() in (set(_SCALES) | {"hundred"})
        ):
            index += 1
            continue
        start = index
        previous = "small" if word in _SMALL or word == "a" else "tens"
        index += 1
        while index < len(tokens):
            next_word = tokens[index].group()
            gap = text[tokens[index - 1].end():tokens[index].start()]
            if not re.fullmatch(r"[\s-]+", gap):
                break
            if previous == "decimal" and next_word in (set(_SMALL) | {"oh"}):
                pass
            elif next_word in _SMALL:
                if previous not in {"tens", "connector", "scale"} or (
                    previous == "tens" and _SMALL[next_word] >= 10
                ):
                    break
                previous = "small"
            elif next_word in _TENS:
                if previous not in {"scale", "connector"}:
                    break
                previous = "tens"
            elif next_word in _SCALES or next_word == "hundred":
                previous = "scale"
            elif next_word == "and" and previous == "scale" and (
                index + 1 < len(tokens)
                and tokens[index + 1].group() in (_SMALL | _TENS)
            ):
                previous = "connector"
            elif next_word == "point" and previous in {"small", "tens", "scale"}:
                previous = "decimal"
            else:
                break
            index += 1
        result.append(text[tokens[start].start():tokens[index - 1].end()])
    return result


_NON_UNITS = {
    "a", "an", "and", "as", "at", "be", "but", "by", "could", "did", "do", "does",
    "for", "from", "had", "has", "have", "if", "in", "is", "it", "may", "might",
    "not", "of", "or", "should", "than", "that", "the", "then", "there", "these",
    "this", "to", "was", "were", "which", "will", "with", "would",
}


def spoken_unit_candidates(text: str) -> list[str]:
    """Find explicit noun-like unit words attached to spoken quantities.

    This is deliberately permissive: it only proves obvious *absence* of a
    unit. The selected unit must still be checked verbatim against narration.
    """
    candidates = []
    cursor = 0
    lowered = text.casefold()
    for mention in spoken_number_mentions(text):
        start = lowered.find(mention.casefold(), cursor)
        if start < 0:
            continue
        cursor = start + len(mention)
        suffix = re.match(r"[\s-]+([a-z][\w-]*)", lowered[cursor:])
        if not suffix:
            continue
        word = suffix.group(1)
        if word in _SCALES:
            tail = re.match(r"[\s-]+([a-z][\w-]*)", lowered[cursor + suffix.end():])
            word = tail.group(1) if tail else ""
        if word and word not in _NON_UNITS:
            candidates.append(word)
    return candidates


def spoken_number_values(text: str) -> set[Decimal]:
    """Digit and English word-form values present in a spoken passage.

    A scaled phrase also exposes its spoken coefficient: ``five billion years``
    supports a graphic value of 5 with unit ``billion years``.
    """
    values = numeric_literals(text)
    normalized = text.casefold()
    matches = list(_WORDS.finditer(normalized))
    words = [match.group() for match in matches]
    index = 0
    while index < len(words):
        start = index
        total = current = 0
        previous = ""
        coefficients: set[Decimal] = set()
        while index < len(words):
            if index > start and not re.fullmatch(
                r"[\s-]+", normalized[matches[index - 1].end() : matches[index].start()]
            ):
                break
            word = words[index]
            if word in _SMALL:
                if previous == "small":
                    break
                if previous == "tens" and _SMALL[word] >= 10:
                    break
                current += _SMALL[word]
                previous = "small"
            elif word in _TENS:
                if previous in {"small", "tens"}:
                    break
                current += _TENS[word]
                previous = "tens"
            elif (
                word == "a"
                and previous == ""
                and index + 1 < len(words)
                and (words[index + 1] == "hundred" or words[index + 1] in _SCALES)
            ):
                current = 1
                previous = "small"
            elif word == "hundred" and current and previous in {"small", "tens"}:
                current *= 100
                previous = "scale"
            elif word in _SCALES and current and previous in {"small", "tens", "scale"}:
                coefficients.add(Decimal(current))
                total += current * _SCALES[word]
                current = 0
                previous = "scale"
            elif (
                word == "and"
                and previous == "scale"
                and index + 1 < len(words)
                and words[index + 1] in (_SMALL | _TENS)
            ):
                previous = "connector"
            elif word == "point" and previous in {"small", "tens", "scale"}:
                digits = []
                cursor = index + 1
                while cursor < len(words):
                    if not re.fullmatch(
                        r"[\s-]+",
                        normalized[matches[cursor - 1].end() : matches[cursor].start()],
                    ):
                        break
                    digit = 0 if words[cursor] == "oh" else _SMALL.get(words[cursor], -1)
                    if digit < 0 or digit > 9:
                        break
                    digits.append(str(digit))
                    cursor += 1
                if digits:
                    values.add(Decimal(f"{total + current}.{''.join(digits)}"))
                    index = cursor
                    previous = "decimal"
                break
            else:
                break
            index += 1
        if index > start:
            values.update(coefficients)
            if previous not in {"connector", "decimal"} and (
                total or current or words[start] == "zero"
            ):
                values.add(Decimal(total + current))
        else:
            index += 1
    return values
