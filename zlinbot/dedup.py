"""
Второй уровень дедупликации: одно и то же объявление, размещённое сразу в нескольких группах.

Первый уровень — post_id (первичный ключ таблицы posts): от повторного показа того же поста.
Второй — хеш первых ~100 значащих символов (буквы и цифры без диакритики и регистра).
"""
import hashlib

from .textnorm import significant

HASH_CHARS = 100  # сколько значащих символов идёт в хеш
MIN_CHARS = 30    # короче — хеш не считаем: «Neviděl někdo psa?» от двух разных людей — не дубль


def text_hash(text: str | None) -> str | None:
    sig = significant(text)
    if len(sig) < MIN_CHARS:
        return None
    return hashlib.sha1(sig[:HASH_CHARS].encode("utf-8")).hexdigest()


def post_hash(text: str | None, shared_text: str | None = None) -> str | None:
    """Хеш поста: по своему тексту, а если он слишком короткий («Sdílím») — по тексту репоста.
    Так один и тот же расшаренный пост в трёх группах склеивается, а длинные разные
    комментарии к нему — нет."""
    return text_hash(text) or text_hash(shared_text)
