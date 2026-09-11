"""
Все селекторы, регулярки и тексты интерфейса Facebook — в одном файле.

Когда Facebook поменяет вёрстку, чинить нужно здесь (и, возможно, в extract.py).
Правило: никаких CSS-классов вида "x1lku1pv" — они генерируются сборкой и
меняются каждую неделю. Только ARIA-роли, data-атрибуты, ссылки и JSON.

Сверено с живой публичной группой без логина: 2026-09-11.
"""
import re

# ---------------------------------------------------------------------------
# Адреса
# ---------------------------------------------------------------------------

# Ссылка на группу в любом виде (www./m./cs-cz., с хвостом ?ref=...) -> slug или числовой ID.
GROUP_URL_RE = re.compile(r"facebook\.com/groups/([^/?#&]+)", re.I)

# Параметр хронологической ленты. На 2026-09-11 без логина FB его ИГНОРИРУЕТ
# (показывает «Doporučené»), но вреда нет — оставляем, вдруг снова заработает.
CHRONO_QUERY = "sorting_setting=CHRONOLOGICAL"

# FB увёл на вход или на проверку «вы не робот». Такую группу без логина не прочитать,
# и обходить это мы не пытаемся.
LOGIN_REDIRECT_RE = re.compile(r"facebook\.com/(?:login|checkpoint)|/login\.php", re.I)
CHECKPOINT_RE = re.compile(r"facebook\.com/checkpoint", re.I)

# ---------------------------------------------------------------------------
# Лента и пост (HTML)
# ---------------------------------------------------------------------------

# Контейнер ленты группы. Нет его -> стена логина или FB сменил вёрстку.
FEED = 'div[role="feed"]'

# Пост в ленте. ВНИМАНИЕ: комментарии тоже role="article" (вложены в пост) —
# extract.py берёт только статьи без предка-статьи. Пустые article — заглушки-скелетоны
# (без логина FB рисует 1 пост и 2 пустые заглушки), их пропускаем.
ARTICLE = 'div[role="article"]'

# Имя автора. Первый такой блок в статье — автор поста, следующие — автор поста,
# которым поделились (репост).
AUTHOR = '[data-ad-rendering-role="profile_name"]'
# Запасной вариант, если data-атрибут пропадёт: первый заголовок в статье.
AUTHOR_FALLBACK = "h2, h3, h4, strong"

# Текст поста. Пробуются по порядку, берётся первый давший результат.
# Первый найденный блок — сам пост, второй — пост, которым поделились.
MESSAGE_SELECTORS = (
    '[data-ad-preview="message"]',
    '[data-ad-comet-preview="message"]',
    '[data-ad-rendering-role="story_message"]',
)

# Все ссылки статьи — среди них ищем permalink (см. регулярки ниже).
# Ссылка-метка времени: <a role="link" aria-label="1 h" href=".../posts/<pid>/">.
LINK = "a[href]"

# Фото поста: картинки внутри ссылок на /photo/. Аватарки — это <svg><image>, сюда не попадают.
PHOTO_IMG = 'a[href*="/photo"] img[src]'

# Видео: <video src>. Почти всегда blob: (сегменты MSE) — такие extract.py отбрасывает.
VIDEO = "video[src]"

# Эмодзи FB рисует картинками; их alt — сам символ.
EMOJI_SRC_RE = re.compile(r"emoji", re.I)

# ---------------------------------------------------------------------------
# ID поста
# ---------------------------------------------------------------------------

# Главный способ (из ТЗ): /groups/<gid>/posts/<pid>/ или /groups/<gid>/permalink/<pid>/.
# gid в ссылке бывает и slug ("Zlin.Udalosti"), и числом — поэтому ключ строим
# на каноническом gid (см. GROUP_NUMERIC_ID_RES), а не на том, что в ссылке.
POST_LINK_RE = re.compile(r"/groups/([^/?#&]+)/(?:posts|permalink)/(\d+)")

# Запасные — только если главный не нашёлся. Осторожно: у репоста ссылки на фото
# содержат set=pcb.<ID ИСХОДНОГО поста>, а не поста в группе — поэтому это последний шанс.
# pfbid… (обфусцированные ID страниц) сюда намеренно не попадают: страницы не поддерживаем.
POST_ID_FALLBACK_RES = (
    re.compile(r"[?&]multi_permalinks=(\d+)"),
    re.compile(r"[?&]story_fbid=(\d+)(?:&|$)"),
    re.compile(r"[?&]set=(?:gm|pcb)\.(\d+)"),
)

# ---------------------------------------------------------------------------
# Встроенный JSON страницы (надёжнее видимого текста)
# ---------------------------------------------------------------------------

# Числовой ID группы. Первое — app-link в <meta property="al:android:url">, самое стабильное.
GROUP_NUMERIC_ID_RES = (
    re.compile(r"fb://group/(\d+)"),
    re.compile(r'"groupID":"(\d+)"'),
)

# Время создания поста (unix). Видимая метка («1 h», «Včera v 14:30») относительная
# и бывает обфусцирована, поэтому дату берём отсюда. Группы: pid, ts.
CREATION_TIME_RES = (
    re.compile(r'"post_id":"(?P<pid>\d+)","creation_time":(?P<ts>\d{9,11})'),
    re.compile(r'"creation_time":(?P<ts>\d{9,11}),[^{}]{0,300}?"url":"https:\\/\\/www\.facebook\.com'
               r'\\/groups\\/[^"\\]+\\/(?:posts|permalink)\\/(?P<pid>\d+)'),
)

# ---------------------------------------------------------------------------
# Страница «Informace» (/about): активность группы — база для метрики покрытия
# ---------------------------------------------------------------------------

# «Dnes 19 nových příspěvků» / «Dnes 1 nový příspěvek» / «12 new posts today»
ACTIVITY_TODAY_RES = (
    re.compile(r"Dnes\s+(\d[\d ]*?)\s+nov\w*\s+příspěv", re.I),
    re.compile(r"(\d[\d,]*?)\s+new posts? today", re.I),
)
# «Dnes žádné nové příspěvky» / «No new posts today» -> 0
ACTIVITY_TODAY_ZERO_RE = re.compile(r"Dnes\s+žádn\w*\s+nov|No new posts today", re.I)
# «577 za poslední měsíc» / «577 in the last month»
ACTIVITY_MONTH_RES = (
    re.compile(r"(\d[\d ]*?)\s+za poslední měsíc", re.I),
    re.compile(r"(\d[\d,]*?)\s+in the last month", re.I),
)

# ---------------------------------------------------------------------------
# Кнопки и оверлеи (для scraper.py)
# ---------------------------------------------------------------------------

DIALOG = 'div[role="dialog"]'
# Признак того, что диалог — это окно входа.
LOGIN_FORM = 'input[name="email"], input[name="pass"], form[action*="login"]'

# Cookie-баннер: жмём «отклонить необязательные» — самый приватный вариант.
COOKIE_DECLINE_RE = re.compile(r"Odmítnout volitelné soubory cookie|Decline optional cookies", re.I)

# Кнопка раскрытия длинного текста. ВНИМАНИЕ: по-чешски на 2026-09-11 это «Zobrazit víc»,
# а не «Zobrazit více» — держим оба.
SEE_MORE_RE = re.compile(r"^\s*(?:Zobrazit víc|Zobrazit více|See more)\s*$", re.I)

# ---------------------------------------------------------------------------
# Очистка текста (для extract.clean_text)
# ---------------------------------------------------------------------------

# «… Zobrazit víc» в конце строки — остаток нераскрытого текста.
SEE_MORE_TAIL_RE = re.compile(r"\s*…\s*(?:Zobrazit víc|Zobrazit více|See more)\s*$", re.I)
# Текст остался обрезанным (раскрыть не удалось) — сигнал для канарейки.
TRUNCATED_RE = re.compile(r"…\s*(?:Zobrazit víc|Zobrazit více|See more)\s*$|^\s*(?:Zobrazit víc|Zobrazit více|See more)\s*$",
                          re.I | re.M)

# Строки «подвала» поста: с первой такой строки (в последних строках текста) всё — служебное.
FOOTER_MARKERS = frozenset({
    "Všechny reakce:", "All reactions:",
    "To se mi líbí", "Like",
    "Komentář", "Komentovat", "Comment",
    "Sdílet", "Share",
    "Napište komentář…", "Napsat komentář…", "Write a comment…",
    "Nejrelevantnější", "Most relevant",
})
# То же, но по началу строки: подпись и счётчик реакций могут слипнуться («Všechny reakce:13»).
FOOTER_PREFIXES = ("Všechny reakce:", "All reactions:")
# Сколько последних строк просматривать в поисках подвала.
FOOTER_WINDOW = 15

# Одиночные строки интерфейса — выкидываются где угодно.
UI_LINES = frozenset({
    "·", "Zobrazit víc", "Zobrazit více", "See more", "Zobrazit méně", "See less",
    "Zobrazit překlad", "See translation", "Ohodnoťte tento překlad", "Rate this translation",
    "Přidat se", "Přidat se ke skupině", "Join", "Join group", "Sledovat", "Follow",
    "Admin", "Autor", "Author", "Moderátor", "Moderator",
    "Nejaktivnější přispěvatel", "Top contributor",
})

# Заглушка FB вместо недоступного расшаренного контента (встречено вживую 2026-09-11):
# «Obsah teď není dostupný Když se to stane, obvykle je to kvůli tomu, že vlastník…».
# Строка, начинающаяся с любого из этих префиксов, выкидывается целиком.
UNAVAILABLE_PREFIXES = (
    "Obsah teď není dostupný", "Když se to stane, obvykle je to kvůli tomu",
    "This content isn't available", "When this happens, it's usually because",
)

# Счётчики реакций/комментариев в хвосте: «13», «1,2 tis.», «12K», «5 komentářů», «2 sdílení».
# Не больше 5 цифр подряд — чтобы не съесть телефон в конце поста.
COUNTER_RE = re.compile(
    r"^(?:\d{1,5}|\d+[,.]\d+\s?(?:tis\.|mil\.|K|M)|\d{1,3}\s?(?:K|M)"
    r"|\d+\s+(?:komentář\w*|comments?|sdílení|shares?|reakc\w*))$",
    re.I,
)

# Метка времени в шапке поста. Применяется ТОЛЬКО в шапке (после имени автора),
# чтобы не выкинуть даты из самого текста — их надо переносить дословно.
RELATIVE_TIME_RE = re.compile(
    r"^(?:\d{1,3}\s?(?:s|min|h|hod\.?|d|dní|t|týd\.?|w|m|měs\.?|r|y)"
    r"|právě teď|just now"
    r"|včera v \d{1,2}:\d{2}|yesterday at .{3,12}"
    r"|\d{1,2}\.\s?\w+(?:\s\d{4})?(?:\s(?:v|at)\s\d{1,2}:\d{2})?"
    r"|[A-Z][a-z]+ \d{1,2}(?:, \d{4})?(?: at .{3,12})?)$",
    re.I,
)
