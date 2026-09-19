"""
Gemini: решает skip/keep и пишет пересказ.

Ответ просим строго JSON (responseMimeType + схема), но разбор всё равно снисходительный:
модель периодически заворачивает JSON в ```json ... ``` или добавляет пояснение вокруг.

Лимиты бесплатного тарифа: пауза между вызовами, три ретрая на 429 с растущей задержкой.
Суточная квота отличается от минутной: на неё ретраи бессмысленны — поднимаем отдельное
исключение, чтобы вызывающий встал до сброса квоты и сказал об этом в личку.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.5-flash-lite"

# Суточная квота бесплатного тарифа считается ОТДЕЛЬНО для каждой модели, и у «больших»
# flash она смехотворная: у gemini-3.6-flash — 20 запросов в сутки (проверено по quotaValue
# в ответе 429), у flash-lite — сотни. Поэтому при исчерпании квоты переходим к следующей
# модели списка, а не ложимся до полуночи: у неё свой счётчик.
FALLBACK_MODELS = ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.6-flash")
MIN_INTERVAL = 8.0                      # секунд между вызовами: бесплатный тариф ~10–15/мин
RETRY_DELAYS = (8.0, 20.0, 45.0)        # три ретрая с нарастающей задержкой
TIMEOUT = 90.0
MAX_OUTPUT_TOKENS = 2048
QUOTA_TZ = ZoneInfo("America/Los_Angeles")  # суточные квоты Google сбрасываются в полночь по Тихоокеанскому
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S | re.I)
_BLOCKED_REASONS = frozenset({"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"})

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "skip": {"type": "boolean"},
        "post": {"type": "string"},
        "post_ru": {"type": "string"},
        "post_ua": {"type": "string"},
        "post_en": {"type": "string"},
        "emoji": {"type": "string"},
        "facts": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["skip", "post", "post_ru", "post_ua", "post_en", "emoji", "facts"],
}

# Эмодзи к теме записи выбирает модель, но только из этого списка: свободный выбор даёт
# то смайлики-лица, то флаги стран. Порядок здесь же задаёт порядок в посте.
ALLOWED_EMOJI: tuple[tuple[str, str], ...] = (
    ("🚧", "дорожные работы, перекрытия, объезды"),
    ("🚌", "общественный транспорт, поезда, остановки"),
    ("🚗", "движение, парковки, автомобили"),
    ("💧", "вода: отключения, аварии, водоёмы"),
    ("⚡", "электричество: отключения, аварии"),
    ("🔥", "пожар"),
    ("🚑", "происшествие, пострадавшие, медпомощь"),
    ("👮", "полиция, розыск, правонарушения"),
    ("🏛", "решения города и края, бюджет, выборы"),
    ("🏗", "стройка, ремонт зданий, новые объекты"),
    ("🎭", "культура: концерты, спектакли, выставки"),
    ("🎉", "праздники, ярмарки, городские события"),
    ("🌳", "парки, деревья, природа, погода"),
    ("🏥", "здравоохранение, больницы"),
    ("🏫", "школы, детсады, образование"),
    ("🛒", "магазины, рынки, цены"),
    ("🐾", "животные, зоопарк"),
    ("⚠️", "предупреждение жителям"),
    ("📰", "если ничего из списка не подходит"),
)
DEFAULT_EMOJI = "📰"
_VS16 = "️"        # вариационный селектор: модель отдаёт ⚠ то с ним, то без

DEFAULT_CRITERIA = (
    "Городская жизнь Злина и окрестностей: новости и происшествия, дороги, перекрытия и "
    "транспорт, отключения воды и электричества, решения города и края, культурные события, "
    "предупреждения жителям.\n"
    "Отсеивать: спорт — матчи, результаты, таблицы, трансферы, разборы игр, интервью с "
    "тренерами и игроками, в том числе про местные клубы; барахолку и продажу личных вещей; "
    "рекламу бизнесов; просьбы о помощи частным лицам; поиск работы и жилья; споры и флуд; "
    "гадания и сборы денег.\n"
    "Исключение: если спортивное событие меняет жизнь города — перекрыты улицы из-за забега, "
    "закрыт бассейн или стадион, ограничено движение, — это оставлять."
)

PROMPT = """Ты редактор телеграм-канала о жизни города Злин (Чехия). На вход — одна запись из источника.

ЧТО НУЖНО КАНАЛУ:
{criteria}

ПРАВИЛА:
1. Не выдумывай. Чего нет в записи — нет и в пересказе. Не додумывай причины и последствия.
2. Суммы, даты, время, адреса, номера маршрутов и названия переноси дословно.
3. "post" — пересказ по-чешски, 1–3 предложения, без обращений к читателю и без эмодзи.
4. "post_ru" — тот же пересказ по-русски, так же кратко.
5. "post_ua" — тот же пересказ по-украински, так же кратко. Это отдельный язык, а не
   переделка русского текста: пиши естественной украинской лексикой.
6. "post_en" — тот же пересказ по-английски, так же кратко.
7. СОБСТВЕННЫЕ ИМЕНА В ПЕРЕВОДАХ (post_ru, post_ua, post_en) НЕ ПЕРЕВОДИ И НЕ
   ТРАНСЛИТЕРИРУЙ. В чешском написании остаются: города и районы (Zlín, Otrokovice,
   Malenovice), улицы, площади и остановки (třída Tomáše Bati, Kvítková, náměstí Míru),
   реки и парки (Dřevnice, Svit), больницы, школы, театры, музеи, спортивные и культурные
   объекты (Krajská nemocnice T. Bati, Baťova vila, Městské divadlo Zlín, Velké kino),
   фирмы и организации (ZAKO Turčín, Zlínský kraj, DSZO), названия мероприятий и имена
   людей. Читатель должен узнать название на табличке, в карте и в расписании — поэтому
   оно одинаковое на всех языках.
   Как писать, чтобы фраза не ломалась:
   - название бери в ПЕРВОМ падеже, как в словаре, и больше не склоняй. А родовое слово
     (улица, площадь, река, больница, район, театр, край, остановка, кинотеатр) переводи
     и склоняй по правилам своего языка:
       ВЕРНО «через реку Dřevnice» — НЕВЕРНО «через река Dřevnice», «через реку Dřevnici»
       ВЕРНО «на улице Kvítková» — НЕВЕРНО «на улице Kvítkovou»
       ВЕРНО «в кинотеатре Velké kino», «в больнице Krajská nemocnice T. Bati»
   - чешские предлоги и падежные формы в перевод не переноси, бери только само название:
       ВЕРНО «Velké kino в городе Zlín ждёт реконструкция»
       НЕВЕРНО «Velké kino ve Zlíně ждёт реконструкция»
   - город и район в русском и украинском — всегда с родовым словом, иначе предлог не
     встаёт: ВЕРНО «в городе Zlín», «в районе Malenovice» — НЕВЕРНО «Во Zlín», «У Zlín».
     В английском родовое слово не нужно: «in Zlín»;
   - но не дублируй родовое слово, если оно уже внутри названия:
       ВЕРНО «на náměstí Práce», «Městské divadlo Zlín покажет…»
       НЕВЕРНО «на площади náměstí Práce», «Театр Městské divadlo Zlín в городе Zlín».
   НА ЧЕШСКИЙ ПЕРЕСКАЗ "post" ЭТО ПРАВИЛО НЕ РАСПРОСТРАНЯЕТСЯ: там обычный живой чешский
   язык со своими падежами — «ve Zlíně», «přes Dřevnici», «na Kvítkové». Никаких
   «ve městě Zlín».
8. "emoji" — один-два эмодзи к теме записи, СТРОГО из списка ниже, без флагов стран и
   смайликов-лиц. Два бери только если запись правда о двух темах.
{emoji_list}
9. "facts" — 1–3 коротких факта из записи по-чешски (цифры, даты, места), чтобы можно было
   сверить пересказ с оригиналом.
10. Не называй по имени частных лиц. Названия организаций, должности и публичные лица — можно.
11. Если запись каналу не подходит, "skip": true, а все пересказы и "facts" — пустые.

{extra}ИСТОЧНИК: {source}
ЗАПИСЬ:
\"\"\"
{text}
\"\"\"
"""


class GeminiError(Exception):
    """Базовая: всё, что пошло не так при обращении к модели."""


class GeminiRetryable(GeminiError):
    """Временное: перегрузка, минутный лимит, сетевой сбой."""


class GeminiQuotaExhausted(GeminiError):
    """Суточная квота кончилась — ретраи не помогут, нужно ждать сброса."""

    def __init__(self, message: str, reset_at: float) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class GeminiBadRequest(GeminiError):
    """400: неверная модель, схема или ключ — само не пройдёт, нужно чинить настройки."""


class GeminiBlocked(GeminiError):
    """Ответ заблокирован фильтрами безопасности (ДТП, криминал) или пуст."""


@dataclass(frozen=True, slots=True)
class Verdict:
    skip: bool
    post: str = ""
    post_ru: str = ""
    post_ua: str = ""
    post_en: str = ""
    emoji: str = ""
    facts: tuple[str, ...] = ()
    model: str = ""
    raw: str = field(default="", repr=False)


def parse_response(text: str) -> Verdict:
    """Текст ответа модели -> Verdict. Терпит ```json-обёртку и мусор вокруг JSON."""
    data = _load_json(text)
    if data is None:
        raise GeminiError(f"в ответе нет JSON: {text.strip()[:200]!r}")
    if not isinstance(data, dict):
        raise GeminiError(f"ожидался объект JSON, пришло {type(data).__name__}")
    skip = data.get("skip")
    if not isinstance(skip, bool):
        skip = str(skip).strip().lower() in ("true", "1", "yes", "да")
    facts = data.get("facts") or []
    if isinstance(facts, str):
        facts = [facts]
    return Verdict(
        skip=skip,
        post=_clean(data.get("post")),
        post_ru=_clean(data.get("post_ru")),
        post_ua=_clean(data.get("post_ua")),
        post_en=_clean(data.get("post_en")),
        emoji=pick_emoji(_clean(data.get("emoji"))) if not skip else "",
        facts=tuple(_clean(f) for f in facts if _clean(f))[:3],
        raw=text,
    )


def _load_json(text: str) -> object | None:
    candidates = [m.group(1) for m in _FENCE_RE.finditer(text or "")]
    candidates.append(text or "")
    for candidate in candidates:
        candidate = candidate.strip()
        for attempt in (candidate, _first_object(candidate)):
            if not attempt:
                continue
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                continue
    return None


def _first_object(text: str) -> str | None:
    """Первый сбалансированный {...} в тексте — на случай пояснений вокруг JSON."""
    start = text.find("{")
    if start < 0:
        return None
    depth, in_string, escaped = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _clean(value: object) -> str:
    return " ".join(str(value).split()) if isinstance(value, (str, int, float)) else ""


def build_prompt(text: str, *, source: str, criteria: str = DEFAULT_CRITERIA, extra: str = "") -> str:
    """extra — разовое указание редактора при переписывании («короче», «убери цены»)."""
    note = (f"ОТДЕЛЬНОЕ УКАЗАНИЕ РЕДАКТОРА (важнее общих правил стиля): {extra.strip()}\n\n"
            if extra.strip() else "")
    listing = "\n".join(f"   {mark} — {about}" for mark, about in ALLOWED_EMOJI)
    return PROMPT.format(criteria=criteria.strip(), source=source or "неизвестен",
                         text=text.strip(), extra=note, emoji_list=listing)


def pick_emoji(raw: str, limit: int = 2) -> str:
    """Оставить из ответа модели только эмодзи из списка, в порядке появления.

    Модель иногда добавляет своё поверх списка или теряет вариационный селектор,
    поэтому сверяем «голые» формы, а в пост кладём канонические."""
    bare = (raw or "").replace(_VS16, "")
    found = sorted(((bare.index(mark.replace(_VS16, "")), mark) for mark, _ in ALLOWED_EMOJI
                    if mark.replace(_VS16, "") in bare))
    return " ".join(mark for _, mark in found[:limit]) or DEFAULT_EMOJI


class Gemini:
    def __init__(self, api_key: str, *, model: str = DEFAULT_MODEL, client: httpx.AsyncClient | None = None,
                 min_interval: float = MIN_INTERVAL, sleep=asyncio.sleep, clock=time.monotonic) -> None:
        self.api_key = api_key
        self.model = model
        self._own = client is None
        self._client = client or httpx.AsyncClient(timeout=TIMEOUT)
        self._min_interval = min_interval
        self._sleep = sleep
        self._clock = clock
        self._last_call: float | None = None

    async def __aenter__(self) -> Gemini:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._own:
            await self._client.aclose()

    async def list_models(self) -> list[str]:
        """Реальный список моделей у API: строки меняются, зашивать их нельзя."""
        r = await self._client.get(f"{API_ROOT}/models", headers=self._headers(), params={"pageSize": 200})
        if r.status_code >= 400:
            raise _error_for(r)
        return sorted(m["name"].removeprefix("models/") for m in r.json().get("models", [])
                      if "generateContent" in m.get("supportedGenerationMethods", []))

    async def summarize(self, text: str, *, source: str = "", criteria: str = DEFAULT_CRITERIA,
                        extra: str = "") -> Verdict:
        prompt = build_prompt(text, source=source, criteria=criteria, extra=extra)
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": RESPONSE_SCHEMA,
                "temperature": 0.3,
                "maxOutputTokens": MAX_OUTPUT_TOKENS,
            },
        }
        data = await self._post(f"{API_ROOT}/models/{self.model}:generateContent", body)
        verdict = parse_response(_answer_text(data))
        return Verdict(verdict.skip, verdict.post, verdict.post_ru, verdict.post_ua, verdict.post_en,
                       verdict.emoji, verdict.facts, self.model, verdict.raw)

    # -- внутреннее ----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

    async def _pace(self) -> None:
        if self._last_call is not None:
            wait = self._min_interval - (self._clock() - self._last_call)
            if wait > 0:
                await self._sleep(wait)

    async def _post(self, url: str, body: dict) -> dict:
        last: GeminiError | None = None
        for attempt, delay in enumerate((*RETRY_DELAYS, None)):
            await self._pace()
            try:
                r = await self._client.post(url, headers=self._headers(), json=body)
            except httpx.HTTPError as e:
                self._last_call = self._clock()
                last = GeminiRetryable(f"{type(e).__name__}: {e}")
            else:
                self._last_call = self._clock()
                if r.status_code < 400:
                    return r.json()
                error = _error_for(r)
                if not isinstance(error, GeminiRetryable):
                    raise error
                last = error
            if delay is None:
                break
            log.warning("Gemini: %s — повтор через %.0f с (попытка %d из %d)",
                        last, delay, attempt + 1, len(RETRY_DELAYS))
            await self._sleep(delay)
        raise last or GeminiRetryable("не удалось получить ответ")


def _answer_text(data: dict) -> str:
    """Текст из ответа API. Заблокированный или пустой ответ — отдельное исключение,
    иначе пост молча уехал бы в «сломано»."""
    if reason := (data.get("promptFeedback") or {}).get("blockReason"):
        raise GeminiBlocked(f"запрос заблокирован фильтром: {reason}")
    candidates = data.get("candidates") or []
    if not candidates:
        raise GeminiBlocked("модель не вернула ни одного варианта ответа")
    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        reason = candidate.get("finishReason", "неизвестно")
        if reason in _BLOCKED_REASONS:
            raise GeminiBlocked(f"ответ заблокирован фильтром: {reason}")
        raise GeminiBlocked(f"пустой ответ модели (finishReason: {reason})")
    return text


def _error_for(r: httpx.Response) -> GeminiError:
    message = _error_message(r)
    if r.status_code == 429:
        if quota := _daily_quota_violation(r):
            limit = quota.get("quotaValue") or "?"
            model = (quota.get("quotaDimensions") or {}).get("model", "")
            return GeminiQuotaExhausted(
                f"суточная квота Gemini исчерпана: модель {model or 'неизвестна'}, "
                f"лимит {limit} запросов в сутки.", next_quota_reset())
        if _is_daily_quota(message):
            return GeminiQuotaExhausted(f"суточная квота Gemini исчерпана: {message}", next_quota_reset())
        return GeminiRetryable(f"429: {message}")
    if r.status_code in (500, 502, 503, 504):
        return GeminiRetryable(f"{r.status_code}: {message}")
    if r.status_code in (400, 404):
        # 404 — модель снята или недоступна этому ключу. Ретраи и пометка записей
        # «сломано» тут вредны: виновата настройка, а не запись.
        return GeminiBadRequest(f"{r.status_code}: {message}")
    if r.status_code in (401, 403):
        return GeminiBadRequest(f"{r.status_code}: ключ не принят — {message}")
    return GeminiError(f"{r.status_code}: {message}")


def _error_message(r: httpx.Response) -> str:
    try:
        return str((r.json().get("error") or {}).get("message") or r.text)[:300]
    except (ValueError, AttributeError):
        return r.text[:300]


def _daily_quota_violation(r: httpx.Response) -> dict | None:
    """QuotaFailure из тела ответа: там quotaId и настоящий лимит.

    Разбирать по тексту message нельзя — он длинный, обрезается, и название метрики
    («...RequestsPerDayPerProjectPerModel-FreeTier») в обрезку не попадает. Из-за этого
    суточная квота два дня подряд выглядела как временная ошибка, и записи уходили в «сломано».
    """
    try:
        details = (r.json().get("error") or {}).get("details") or []
    except (ValueError, AttributeError):
        return None
    for detail in details:
        if not isinstance(detail, dict) or "QuotaFailure" not in str(detail.get("@type", "")):
            continue
        for violation in detail.get("violations") or []:
            if "perday" in str(violation.get("quotaId", "")).lower():
                return violation
    return None


def _is_daily_quota(message: str) -> bool:
    low = message.lower()
    return "perday" in low.replace(" ", "") or "per day" in low or "daily" in low


def next_model(current: str, chain: tuple[str, ...] = FALLBACK_MODELS) -> str | None:
    """Следующая модель после исчерпания суточной квоты текущей. None — список кончился.

    Модель не из списка (выбрана вручную) — начинаем список с начала."""
    if current in chain:
        index = chain.index(current) + 1
        return chain[index] if index < len(chain) else None
    return chain[0] if chain else None


def next_quota_reset(now: float | None = None) -> float:
    """Ближайшая полночь по Тихоокеанскому времени — когда Google сбрасывает суточные квоты."""
    current = datetime.fromtimestamp(now or time.time(), QUOTA_TZ)
    tomorrow = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.timestamp()
