"""
Дешёвый отсев по стоп-словам — до обращения к Gemini, чтобы не жечь квоту на заведомом мусоре.

Сравнение без учёта регистра и диакритики, по подстроке: «prodam» ловит и «Prodám», и
«prodáme». Это важно для чешского, где слово меняет окончание почти в каждом падеже.
Цена подстроки — ложные срабатывания внутри других слов, поэтому слова короче трёх букв
не принимаются.
"""
from __future__ import annotations

from collections.abc import Iterable

from .textnorm import fold

MIN_WORD = 3


def normalize(word: str) -> str:
    return fold(word).strip()


def is_valid(word: str) -> bool:
    return len(normalize(word)) >= MIN_WORD


def match(text: str | None, words: Iterable[str]) -> str | None:
    """Первое стоп-слово, встретившееся в тексте, иначе None. words — нормализованные."""
    haystack = fold(text or "")
    if not haystack:
        return None
    return next((w for w in words if w and w in haystack), None)
