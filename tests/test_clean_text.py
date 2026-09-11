"""Очистка текста поста от служебного мусора FB."""
import pytest

from zlinbot.fb.extract import clean_text


def test_real_page_innertext_sample():
    # innerText поста страницы Zlin.cz, 2026-09-11 (имя человека заменено)
    raw = ("Zlin.cz\n58 min\n \n·\n Když se na konci července rozběhlo pátrání po pohřešovaném seniorovi "
           "na Zlínsku, neváhal Jan Novák přiložit ruku k dílu. Díky znalosti místního terénu a pohotové "
           "… Zobrazit víc\nVšechny reakce:\n13\n1\nTo se mi líbí\nKomentář")
    assert clean_text(raw, names=["Zlin.cz"]) == (
        "Když se na konci července rozběhlo pátrání po pohřešovaném seniorovi na Zlínsku, neváhal "
        "Jan Novák přiložit ruku k dílu. Díky znalosti místního terénu a pohotové")


def test_english_ui():
    raw = "John Doe\n2h\n·\nSelling my bike\nAll reactions:\n5\nLike\nComment\nShare"
    assert clean_text(raw, names=["John Doe"]) == "Selling my bike"


def test_numbers_in_body_survive_counters_in_tail_dont():
    raw = "Cena: 1 500 Kč\nTel. 777123456\n2 komentáře\nTo se mi líbí\nKomentář"
    assert clean_text(raw) == "Cena: 1 500 Kč\nTel. 777123456"


@pytest.mark.parametrize("phone", ["777 123 456", "777123456"])
def test_phone_at_end_is_not_a_counter(phone):
    assert clean_text(f"Volejte\n{phone}") == f"Volejte\n{phone}"


def test_counters_formats():
    assert clean_text("Text\n1,2 tis.\n12K\n3 sdílení\nTo se mi líbí") == "Text"


def test_reactions_label_glued_to_count():
    assert clean_text("Text\nVšechny reakce:12\nTo se mi líbí") == "Text"


def test_dates_in_body_are_kept_without_header():
    raw = "15. září\nKoncert na náměstí\n16. září\nDivadlo"
    assert clean_text(raw, names=["Jan"]) == raw


def test_header_time_removed_but_body_date_kept():
    raw = "Jan\n15. září v 10:00\n·\n16. září bude uzavírka"
    assert clean_text(raw, names=["Jan"]) == "16. září bude uzavírka"


def test_shared_story_header_in_the_middle():
    raw = "Eva\n3 h\n·\nSdílím\nMěsto Zlín\n5 h\n·\nOdstávka vody\nTo se mi líbí"
    assert clean_text(raw, names=["Eva", "Město Zlín"]) == "Sdílím\nOdstávka vody"


def test_name_in_body_without_time_is_kept():
    raw = "Eva\n1 h\nPíše Město Zlín:\nMěsto Zlín\nje super"
    assert clean_text(raw, names=["Eva", "Město Zlín"]) == "Píše Město Zlín:\nMěsto Zlín\nje super"


def test_group_name_duplicate_at_start():
    raw = "Události Zlín a okolí\nJan\n1 h\n·\nText"
    assert clean_text(raw, names=["Jan"], group_name="Události Zlín a okolí") == "Text"


def test_unavailable_shared_content_placeholder():
    # вживую 2026-09-11: репост недоступного контента
    raw = ("Stavební Práce\n4 min\n·\nObsah teď není dostupný Když se to stane, obvykle je to kvůli tomu, "
           "že vlastník obsah sdílel jen s malou skupinou lidí, změnil nastavení soukromí, nebo byl obsah odebrán.")
    assert clean_text(raw, names=["Stavební Práce"]) == ""
    assert clean_text("Můj komentář\nThis content isn't available right now\n"
                      "When this happens, it's usually because the owner…") == "Můj komentář"


def test_ui_lines_removed_anywhere():
    assert clean_text("Text\nZobrazit překlad\nDalší text") == "Text\nDalší text"


def test_see_more_tail_removed():
    assert clean_text("Dlouhý text … Zobrazit víc") == "Dlouhý text"
    assert clean_text("Long text … See more") == "Long text"


def test_like_far_from_the_end_is_body():
    body = ["Řádek"] * 20
    body[1] = "Like"
    assert clean_text("\n".join(body)).split("\n")[1] == "Like"


def test_whitespace_and_blank_lines():
    assert clean_text("") == ""
    assert clean_text("  \n \n") == ""
    assert clean_text("A\n\n\n\nB") == "A\n\nB"
    assert clean_text("A   B\u00a0\u00a0C") == "A B C"
