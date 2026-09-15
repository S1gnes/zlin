"""Разбор ответа Gemini и поведение на ошибках API — без сети."""
import json
import time

import httpx
import pytest

from zlinbot.gemini import (DEFAULT_CRITERIA, Gemini, GeminiBadRequest, GeminiBlocked, GeminiError,
                            GeminiQuotaExhausted, GeminiRetryable, build_prompt, parse_response)

ANSWER = {"skip": False, "post": "Od 20. září bude uzavřena třída Tomáše Bati.",
          "post_ru": "С 20 сентября улица Томаша Бати будет закрыта.",
          "facts": ["uzavírka od 20. září", "třída Tomáše Bati"]}


def reply(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]}


def error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": status, "message": message, "status": "ERROR"}})


# -- разбор ответа -------------------------------------------------------------

def test_clean_json():
    v = parse_response(json.dumps(ANSWER, ensure_ascii=False))
    assert v.skip is False
    assert v.post == ANSWER["post"] and v.post_ru == ANSWER["post_ru"]
    assert v.facts == tuple(ANSWER["facts"])


def test_json_wrapped_in_markdown():
    text = "```json\n" + json.dumps(ANSWER, ensure_ascii=False) + "\n```"
    assert parse_response(text).post == ANSWER["post"]
    assert parse_response("```\n" + json.dumps(ANSWER) + "\n```").post == ANSWER["post"]


def test_json_with_explanations_around():
    text = f"Конечно, вот результат:\n{json.dumps(ANSWER, ensure_ascii=False)}\nНадеюсь, подойдёт."
    assert parse_response(text).post == ANSWER["post"]


def test_garbage_instead_of_json():
    for text in ("извини, не могу", "", "{сломано", "[1, 2, 3]"):
        with pytest.raises(GeminiError):
            parse_response(text)


def test_sloppy_field_types_are_survived():
    v = parse_response('{"skip": "true", "post": "x", "post_ru": "х", "facts": "один факт"}')
    assert v.skip is True and v.facts == ("один факт",)
    v = parse_response('{"skip": false, "post": " a \\n b ", "facts": ["1", "2", "3", "4"], "post_ru": null}')
    assert v.post == "a b" and len(v.facts) == 3 and v.post_ru == ""


def test_prompt_carries_rules_and_content():
    prompt = build_prompt("Uzavírka od 20. září", source="ZLIN.CZ", criteria="Только новости города")
    assert "Только новости города" in prompt and "ZLIN.CZ" in prompt and "Uzavírka od 20. září" in prompt
    for rule in ("Не выдумывай", "дословно", "по-чешски", "post_ru", "facts", "частных лиц"):
        assert rule in prompt
    assert "Отсеивать" in build_prompt("x", source="s")  # критерии по умолчанию на месте
    assert DEFAULT_CRITERIA.splitlines()[0][:20] in build_prompt("x", source="s")


# -- поведение с API -----------------------------------------------------------

def gemini(handler, **kw) -> Gemini:
    """Поддельный sleep двигает и поддельные часы — иначе пауза между вызовами
    накладывалась бы на задержку ретрая, чего в бою не бывает."""
    sleeps: list[float] = []
    now = [0.0]

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    kw.setdefault("clock", lambda: now[0])
    g = Gemini("test-key", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
               sleep=sleep, **kw)
    g.sleeps = sleeps  # type: ignore[attr-defined]
    return g


async def test_successful_call_sends_key_and_json_contract():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("x-goog-api-key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=reply(json.dumps(ANSWER, ensure_ascii=False)))

    async with gemini(handler) as g:
        v = await g.summarize("Uzavírka", source="ZLIN.CZ")
    assert v.post == ANSWER["post"] and v.model == "gemini-2.5-flash"
    assert seen["key"] == "test-key"
    cfg = seen["body"]["generationConfig"]
    assert cfg["responseMimeType"] == "application/json"
    assert cfg["responseSchema"]["required"] == ["skip", "post", "post_ru", "facts"]


async def test_429_per_minute_is_retried_then_succeeds():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return error(429, "Resource has been exhausted (per minute quota)")
        return httpx.Response(200, json=reply(json.dumps(ANSWER)))

    async with gemini(handler) as g:
        v = await g.summarize("x")
    assert v.post == ANSWER["post"] and len(calls) == 3
    assert g.sleeps[:2] == [8.0, 20.0]          # задержка нарастает


async def test_429_daily_quota_is_not_retried():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return error(429, "Quota exceeded for quota metric 'GenerateRequestsPerDayPerProject'")

    async with gemini(handler) as g:
        with pytest.raises(GeminiQuotaExhausted) as e:
            await g.summarize("x")
    assert len(calls) == 1                       # ретраи бессмысленны — квота суточная
    assert e.value.reset_at > time.time()


async def test_429_is_retried_only_three_times():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return error(429, "rate limit per minute")

    async with gemini(handler) as g:
        with pytest.raises(GeminiRetryable):
            await g.summarize("x")
    assert len(calls) == 4                       # первая попытка + три ретрая
    assert g.sleeps == [8.0, 20.0, 45.0]


async def test_400_and_403_are_not_retried():
    for status, message in ((400, "models/gemini-9 is not found"), (403, "API key not valid")):
        calls = []

        def handler(request: httpx.Request, calls=calls, status=status, message=message) -> httpx.Response:
            calls.append(1)
            return error(status, message)

        async with gemini(handler) as g:
            with pytest.raises(GeminiBadRequest) as e:
                await g.summarize("x")
        assert len(calls) == 1 and message[:10] in str(e.value)


async def test_500_is_retryable():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, text="overloaded")

    async with gemini(handler) as g:
        with pytest.raises(GeminiRetryable):
            await g.summarize("x")
    assert len(calls) == 4


@pytest.mark.parametrize("payload, expect", [
    ({"promptFeedback": {"blockReason": "SAFETY"}}, "SAFETY"),
    ({"candidates": []}, "ни одного варианта"),
    ({"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]}, "SAFETY"),
    ({"candidates": [{"content": {"parts": [{"text": " "}]}, "finishReason": "MAX_TOKENS"}]}, "MAX_TOKENS"),
])
async def test_blocked_or_empty_answers(payload, expect):
    async with gemini(lambda request: httpx.Response(200, json=payload)) as g:
        with pytest.raises(GeminiBlocked) as e:
            await g.summarize("x")
    assert expect in str(e.value)


async def test_network_error_is_retryable():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет сети", request=request)

    async with gemini(boom) as g:
        with pytest.raises(GeminiRetryable):
            await g.summarize("x")


async def test_pause_between_calls():
    async with gemini(lambda r: httpx.Response(200, json=reply(json.dumps(ANSWER))),
                      min_interval=8.0) as g:
        await g.summarize("x")
        await g.summarize("y")
    assert g.sleeps and 7.5 < g.sleeps[0] <= 8.0   # второй вызов подождал


async def test_list_models_returns_only_generating_ones():
    payload = {"models": [
        {"name": "models/gemini-3-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
    ]}
    async with gemini(lambda r: httpx.Response(200, json=payload)) as g:
        assert await g.list_models() == ["gemini-2.5-flash", "gemini-3-flash"]


async def test_list_models_reports_bad_key():
    async with gemini(lambda r: error(403, "API key not valid")) as g:
        with pytest.raises(GeminiBadRequest):
            await g.list_models()
