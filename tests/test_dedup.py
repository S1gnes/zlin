"""Второй уровень дедупликации: хеш первых ~100 значащих символов."""
from zlinbot.dedup import HASH_CHARS, MIN_CHARS, post_hash, text_hash
from zlinbot.textnorm import fold, significant

AD = "Prodám kolo Author, 26\", výborný stav, cena 4 500 Kč. Volejte 777 123 456, Zlín-Jižní Svahy."


def test_same_ad_different_formatting_is_same_hash():
    variants = [
        AD,
        AD.upper(),
        "prodam kolo author 26 vyborny stav cena 4500 kc volejte 777123456 zlin jizni svahy",
        "  🚲 " + AD.replace(", ", ",\n") + " 👍",
        AD + " https://www.facebook.com/groups/x/posts/1/?__cft__[0]=AZabc",
    ]
    assert len({text_hash(v) for v in variants}) == 1


def test_different_ads_differ():
    assert text_hash(AD) != text_hash(AD.replace("4 500", "5 500"))


def test_only_prefix_matters():
    # по ТЗ: хеш первых ~100 значащих символов — хвост после них не влияет
    base = "a" * HASH_CHARS
    assert text_hash(base + " konec jeden") == text_hash(base + " úplně jiný konec")


def test_short_text_has_no_hash():
    assert text_hash("Neviděl někdo psa?") is None           # 15 значащих < MIN_CHARS
    assert text_hash("x" * MIN_CHARS) is not None
    assert text_hash("") is None and text_hash(None) is None


def test_post_hash_falls_back_to_shared_text():
    shared = "Oznámení města: odstávka vody 20. září 8:00–14:00 v ulici Tomáše Bati"
    h = post_hash("Sdílím", shared)
    assert h == text_hash(shared)
    assert post_hash("Sdílím, ať to ví všichni", shared) == h          # короткие комментарии — один и тот же репост
    long_comment = "Tohle je důležité pro všechny na Jižních Svazích, sdílejte prosím dál"
    assert post_hash(long_comment, shared) == text_hash(long_comment)  # свой длинный текст — свой хеш


def test_fold_and_significant():
    assert fold("Prodám KOLO, Žluťoučký kůň") == "prodam kolo, zlutoucky kun"
    assert significant("Cena: 1 500 Kč! 🚲 www.bazar.cz/x") == "cena1500kc"
