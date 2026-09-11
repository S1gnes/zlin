"""Нормализация текста для сравнения — общая для дедупликации и стоп-слов."""
import re
import unicodedata

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)


def fold(text: str | None) -> str:
    """«Prodám KOLO» -> «prodam kolo»: без диакритики и регистра, пробелы сохраняются."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def significant(text: str | None) -> str:
    """Только буквы и цифры из fold(text). Ссылки выкидываются: у одного и того же
    объявления в разных группах разные трекинг-хвосты."""
    return "".join(ch for ch in fold(_URL_RE.sub(" ", text or "")) if ch.isalnum())
