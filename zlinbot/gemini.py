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
DEFAULT_MODEL = "gemini-2.5-flash"
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
        "facts": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["skip", "post", "post_ru", "facts"],
}

DEFAULT_CRITERIA = (
    "Городская жизнь Злина и окрестностей: новости, происшествия, дороги и перекрытия, "
    "решения города и края, культурные и спортивные события, предупреждения жителям.\n"
    "Отсеивать: барахолку и продажу личных вещей, рекламу бизнесов, просьбы о помощи "
    "частным лицам, поиск работы и жилья, споры и флуд, гадания и сборы денег."
)

PROMPT = """Ты редактор телеграм-канала о жизни города Злин (Чехия). На вход — одна запись из источника.

ЧТО НУЖНО КАНАЛУ:
{criteria}

ПРАВИЛА:
1. Не выдумывай. Чего нет в записи — нет и в пересказе. Не додумывай причины и последствия.
2. Суммы, даты, время, адреса, номера маршрутов и названия переноси дословно.
3. "post" — пересказ по-чешски, 1–3 предложения, без обращений к читателю и без эмодзи.
4. "post_ru" — тот же пересказ по-русски, так же кратко.
5. "facts" — 1–3 коротких факта из записи по-чешски (цифры, даты, места), чтобы можно было
   сверить пересказ с оригиналом.
6. Не называй по имени частных лиц. Названия организаций, должности и публичные лица — можно.
7. Если запись каналу не подходит, "skip": true, а "post", "post_ru" и "facts" — пустые.

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
    return PROMPT.format(criteria=criteria.strip(), source=source or "неизвестен",
                         text=text.strip(), extra=note)


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
        return Verdict(verdict.skip, verdict.post, verdict.post_ru, verdict.facts, self.model, verdict.raw)

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
        if _is_daily_quota(message):
            return GeminiQuotaExhausted(f"суточная квота Gemini исчерпана: {message}", next_quota_reset())
        return GeminiRetryable(f"429: {message}")
    if r.status_code in (500, 502, 503, 504):
        return GeminiRetryable(f"{r.status_code}: {message}")
    if r.status_code == 400:
        return GeminiBadRequest(f"400: {message}")
    if r.status_code in (401, 403):
        return GeminiBadRequest(f"{r.status_code}: ключ не принят — {message}")
    return GeminiError(f"{r.status_code}: {message}")


def _error_message(r: httpx.Response) -> str:
    try:
        return str((r.json().get("error") or {}).get("message") or r.text)[:300]
    except (ValueError, AttributeError):
        return r.text[:300]


def _is_daily_quota(message: str) -> bool:
    low = message.lower()
    return "perday" in low.replace(" ", "") or "per day" in low or "daily" in low


def next_quota_reset(now: float | None = None) -> float:
    """Ближайшая полночь по Тихоокеанскому времени — когда Google сбрасывает суточные квоты."""
    current = datetime.fromtimestamp(now or time.time(), QUOTA_TZ)
    tomorrow = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.timestamp()
