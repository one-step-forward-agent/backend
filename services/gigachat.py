import asyncio
import base64
import json
import logging
import re
import ssl
import time
from functools import cache
from uuid import uuid4
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

from app.core import lang
from app.core.config import settings
from app.services import usage

logger = logging.getLogger(__name__)


# Deyla is a woman: every reply must use feminine forms about herself
PERSONA = (
    "Ты — Deyla, девушка, ИИ-помощница по планированию дня. О себе всегда говори в женском роде: "
    "«поняла», «рада помочь», «напомню», «уверена»; никогда не используй мужской род о себе. "
)
# The model only writes text; the app performs every change separately and reports it itself
NO_ACTION_CLAIMS = (
    "Твой текстовый ответ ничего не меняет в календаре. Никогда не пиши, что ты удалила, перенесла, изменила, "
    "отметила, отменила или добавила задачи: это делает приложение отдельно и само сообщит результат. "
)
# Advice is worth reading only when it is about this user: their tasks, their numbers, their habits
PERSONAL_ADVICE = (
    "Советы должны быть персональными. Каждый совет обязательно опирается на конкретику из фактов: называет задачу "
    "в кавычках, день или время, или число из habits (сколько пользователь обычно выполняет за день, сферы с низким "
    "процентом, задачи, которые откладываются, время дня, когда он чаще всё делает, слабый день недели). "
    "Не говори о целях и привычках пользователя и не предлагай их ставить или отслеживать — такого раздела в Deyla пока нет: "
    "говори о его задачах, днях и времени. "
    "Запрещены общие советы, которые подходят любому: «планируйте 3–5 дел», «делайте перерывы», «не забывайте отмечать задачи», "
    "«расставьте приоритеты», «берегите себя», «так держать». Если сказать нечего конкретного — дай меньше советов. "
    "Не используй слова flexible, fixed, гибкие, movable и другие названия полей: говори «задачи без времени — их можно "
    "поставить на любой день», «задачи, которые нельзя переносить». "
)
# Requests whose text the user reads; the rest only extract fields the app parses in Russian
USER_FACING = {"agent", "suggest_replies", "chat_reply", "process_message", "analysis", "breakdown", "recommendations", "plan_recommendations"}
ENGLISH = (
    "LANGUAGE: the user's site is in English. Everything the user reads — answers, questions, advice, "
    "recommendation titles and texts, step titles — must be in natural English; this overrides any instruction "
    "above to answer in Russian. Keep task titles taken from the user's calendar as they are, in quotes. "
    "Function call arguments and JSON field names follow the rules above unchanged."
)


def in_site_language(messages: list[dict]) -> list[dict]:
    """The messages with the English instruction added to the system message, or as one, when the site is in English."""
    if not lang.is_en() or not messages:
        return messages
    first = messages[0]
    if first.get("role") == "system" and isinstance(first.get("content"), str):
        return [{**first, "content": first["content"] + "\n\n" + ENGLISH}, *messages[1:]]
    return [{"role": "system", "content": ENGLISH}, *messages]


# The OAuth token lives 30 minutes; fetching it for every request doubled the latency
TOKEN_MARGIN = 60
_token_cache: dict = {"value": None, "expires": 0.0}
_token_lock = asyncio.Lock()


@cache
def _ssl_context() -> ssl.SSLContext | bool:
    if not settings.gigachat_ca_bundle:
        logger.warning("GIGACHAT_CA_BUNDLE is not set; GigaChat TLS certificates are not verified")
        return False
    context = ssl.create_default_context()
    context.load_verify_locations(settings.gigachat_ca_bundle)
    return context


def parse_events_response(content: str) -> list[dict]:
    cleaned_content = content.strip()
    if cleaned_content.startswith("```"):
        cleaned_content = cleaned_content.split("\n", 1)[1]
        cleaned_content = cleaned_content.rsplit("```", 1)[0].strip()

    decoder = json.JSONDecoder()
    array_start = cleaned_content.find("[")
    if array_start == -1:
        raise ValueError("GigaChat не вернул JSON-массив событий")
    try:
        events, _ = decoder.raw_decode(cleaned_content, array_start)
    except json.JSONDecodeError as error:
        raise ValueError("GigaChat вернул некорректный JSON-массив событий") from error
    if not isinstance(events, list):
        raise ValueError("GigaChat вернул некорректный список событий")
    return events


def parse_message_response(content: str) -> dict:
    cleaned_content = content.strip()
    if cleaned_content.startswith("```"):
        cleaned_content = cleaned_content.split("\n", 1)[1]
        cleaned_content = cleaned_content.rsplit("```", 1)[0].strip()
    if cleaned_content.startswith("["):
        return {"events": parse_events_response(cleaned_content), "answer": None}
    decoder = json.JSONDecoder()
    object_start = cleaned_content.find("{")
    if object_start == -1:
        return {"events": [], "answer": cleaned_content or None}
    try:
        result, _ = decoder.raw_decode(cleaned_content, object_start)
    except json.JSONDecodeError as error:
        repaired = re.sub(
            r"([{,]\s*)([A-Za-z_][A-Za-z0-9_-]*)(\s*:)",
            r'\1"\2"\3',
            cleaned_content,
        )
        try:
            result, _ = decoder.raw_decode(repaired, repaired.find("{"))
        except (json.JSONDecodeError, ValueError):
            logger.warning("GigaChat returned non-JSON answer: %s", error)
            return {"events": [], "answer": cleaned_content or None}
    if not isinstance(result, dict):
        return {"events": [], "answer": cleaned_content or None}
    events = result.get("events")
    if not isinstance(events, list):
        events = []
    answer = result.get("answer")
    result["answer"] = answer if isinstance(answer, str) else None
    result["events"] = events
    intent = result.get("intent")
    result["intent"] = intent if intent in ("create", "change", "delete", "complete", "analyze", "breakdown", "other") else None
    return result


def parse_replies(content: str) -> list[str]:
    """The model's JSON array of ready answers; a broken one gives none."""
    cleaned = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start = cleaned.find("[")
    if start == -1:
        return []
    try:
        items, _ = json.JSONDecoder().raw_decode(cleaned, start)
    except json.JSONDecodeError:
        return []
    return [str(item).strip() for item in items if isinstance(item, str | int) and str(item).strip()] if isinstance(items, list) else []


def parse_search_filters_response(content: str) -> dict:
    cleaned_content = content.strip()
    if cleaned_content.startswith("```"):
        cleaned_content = cleaned_content.split("\n", 1)[1]
        cleaned_content = cleaned_content.rsplit("```", 1)[0].strip()
    decoder = json.JSONDecoder()
    object_start = cleaned_content.find("{")
    if object_start == -1:
        return {}
    try:
        result, _ = decoder.raw_decode(cleaned_content, object_start)
    except json.JSONDecodeError:
        repaired = re.sub(
            r"([{,]\s*)([A-Za-z_][A-Za-z0-9_-]*)(\s*:)",
            r'\1"\2"\3',
            cleaned_content,
        )
        try:
            result, _ = decoder.raw_decode(repaired, repaired.find("{"))
        except (json.JSONDecodeError, ValueError):
            return {}
    if not isinstance(result, dict):
        return {}
    return result


def usage_counts(counts: dict | None) -> dict | None:
    """OpenAI-compatible APIs report cached prompt tokens in prompt_tokens_details; GigaChat as precached_prompt_tokens."""
    if not isinstance(counts, dict) or "precached_prompt_tokens" in counts:
        return counts
    details = counts.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    return {**counts, "precached_prompt_tokens": cached} if cached is not None else counts


# ---------- the agent's functions in the OpenAI tools format ----------
# The agent keeps its history in GigaChat's format (function_call, role "function"); these translate it both ways.


def openai_tools(functions: list[dict]) -> list[dict]:
    """GigaChat function specs as OpenAI tools; few_shot_examples is GigaChat-only, so the examples go to the description."""
    tools = []
    for spec in functions:
        description = spec.get("description", "")
        examples = spec.get("few_shot_examples") or []
        if examples:
            description += " Примеры: " + "; ".join(
                f"«{example['request']}» → {json.dumps(example['params'], ensure_ascii=False)}" for example in examples
            )
        function = {"name": spec["name"], "description": description, "parameters": spec.get("parameters") or {"type": "object", "properties": {}}}
        tools.append({"type": "function", "function": function})
    return tools


def openai_messages(messages: list[dict]) -> list[dict]:
    """The agent's history as OpenAI messages: every function call gets an id that its result refers to."""
    converted = []
    call_id = None
    for index, message in enumerate(messages):
        call = message.get("function_call")
        if call:
            call_id = f"call_{index}"
            arguments = json.dumps(call.get("arguments") or {}, ensure_ascii=False)
            tool_call = {"id": call_id, "type": "function", "function": {"name": call.get("name"), "arguments": arguments}}
            converted.append({"role": "assistant", "content": message.get("content") or None, "tool_calls": [tool_call]})
        elif message.get("role") == "function":
            converted.append({"role": "tool", "tool_call_id": call_id or "call_0", "content": message.get("content") or ""})
        else:
            converted.append({"role": message["role"], "content": message.get("content") or ""})
    return converted


def one_system_message(messages: list[dict]) -> list[dict]:
    """GigaChat takes one system message, the first: the agent's rules and context joined."""
    leading = 0
    while leading < len(messages) and messages[leading].get("role") == "system":
        leading += 1
    if leading < 2:
        return messages
    content = "\n\n".join(message.get("content") or "" for message in messages[:leading])
    return [{"role": "system", "content": content}, *messages[leading:]]


def gigachat_message(message: dict) -> dict:
    """An OpenAI answer in the agent's format. The agent runs one function per step: the first tool call is taken,
    and the model calls the others in the next steps, seeing this one's result."""
    result = {"role": "assistant", "content": message.get("content") or ""}
    tool_calls = message.get("tool_calls") or []
    function = tool_calls[0].get("function") if tool_calls else message.get("function_call")
    if function:
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                arguments = {}
        result["function_call"] = {"name": function.get("name"), "arguments": arguments if isinstance(arguments, dict) else {}}
    return result


# Tokens a reasoning model may spend thinking before it writes the answer: without them a 1500-token limit
# is used up by the reasoning and the answer comes back empty
THINKING_ROOM = {"low": 4000, "high": 12000}


def with_thinking(payload: dict) -> dict:
    """The OpenAI-compatible request with the reasoning setting of OPENAI_THINKING."""
    mode = settings.openai_thinking
    if mode == "off":
        return {**payload, "thinking": {"type": "disabled"}}
    if mode in THINKING_ROOM:
        return {**payload, "reasoning_effort": mode, "max_tokens": payload.get("max_tokens", 1000) + THINKING_ROOM[mode]}
    return payload


class ImagesNotSupported(Exception):
    """The model in use cannot read images."""


class GigaChatClient:
    """Language model client. Despite the name it also talks to an OpenAI-compatible API when LLM_PROVIDER=openai."""

    token_url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    chat_url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

    async def _token(self, session: aiohttp.ClientSession) -> str:
        async with _token_lock:
            if _token_cache["value"] and _token_cache["expires"] - TOKEN_MARGIN > time.time():
                return _token_cache["value"]
            data = await self._new_token(session)
            # expires_at comes in milliseconds; without it the token is kept for 25 minutes
            expires = data.get("expires_at")
            _token_cache["value"] = data["access_token"]
            _token_cache["expires"] = expires / 1000 if isinstance(expires, (int, float)) else time.time() + 25 * 60
            return _token_cache["value"]

    async def _new_token(self, session: aiohttp.ClientSession) -> dict:
        logger.info("Requesting GigaChat access token")
        credentials = settings.gigachat_credentials
        if not settings.gigachat_credentials:
            credentials = base64.b64encode(credentials.encode()).decode()
        headers = {"Authorization": f"Basic {credentials}", "RqUID": str(uuid4())}
        async with session.post(
            self.token_url,
            headers=headers,
            data={"scope": settings.gigachat_scope},
            ssl=_ssl_context(),
        ) as response:
            if response.status >= 400:
                error_body = await response.text()
                logger.error("GigaChat token request failed: status=%s body=%s", response.status, error_body)
                response.raise_for_status()
            logger.info("GigaChat access token received")
            return await response.json()

    async def _complete(self, payload: dict, purpose: str, timeout: float | None = None) -> dict:
        """One chat completion from the service LLM_PROVIDER selects; its token usage is stored in llm_usage under `purpose`."""
        client_timeout = aiohttp.ClientTimeout(total=timeout) if timeout else aiohttp.ClientTimeout(total=300)
        if purpose in USER_FACING:
            payload = {**payload, "messages": in_site_language(payload["messages"])}
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            if settings.uses_openai:
                payload = with_thinking(payload)
                url = f"{settings.openai_base_url}/chat/completions"
                headers = {"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"}
                ssl_context = True
            else:
                url = self.chat_url
                headers = {"Authorization": f"Bearer {await self._token(session)}", "Content-Type": "application/json"}
                ssl_context = _ssl_context()
            started = time.monotonic()
            async with session.post(url, headers=headers, json=payload, ssl=ssl_context) as response:
                if response.status >= 400:
                    logger.error("%s request failed: status=%s body=%s", settings.llm_provider, response.status, (await response.text())[:500])
                response.raise_for_status()
                result = await response.json()
        await usage.record(purpose, result.get("model") or payload.get("model"), usage_counts(result.get("usage")), int((time.monotonic() - started) * 1000))
        return result

    async def process_message(
        self,
        text: str,
        timezone: str = "Europe/Moscow",
        context: str = "",
        calendar: str = "",
    ) -> dict:
        now = datetime.now(ZoneInfo(timezone))
        prompt = (
            PERSONA
            + NO_ACTION_CLAIMS
            + "Ты извлекаешь задачи и события из сообщения пользователя для календаря. "
            "Найди ВСЕ отдельные дела: встречи, занятия, уроки, пары, звонки, поездки, перелёты, "
            "поручения, покупки, дедлайны и другие планы.\n"
            "Правила:\n"
            "1. Каждое отдельное действие — отдельный объект. Не объединяй дела только потому, что они в одном предложении.\n"
            "2. Сложные запросы раскладывай на все пункты: расписание учебного дня — каждый урок отдельно; "
            "план поездки — дорога, заселение, экскурсии, обратный путь отдельно, каждый со своим днём. "
            "Если указаны длительность и перерывы (\"уроки по 45 минут с 8:30, перемены 10 минут\"), "
            "сам посчитай start_time и end_time каждого пункта.\n"
            "3. \"Напомни\" — не отдельное событие, а reminder_minutes основного дела: \"напомни за 10 минут\" — 10, "
            "просто \"напомни\" — 0, иначе null.\n"
            "4. НЕ вычисляй даты дней недели сам и не придумывай время. Для каждого объекта дословно выпиши из "
            "сообщения фразы, которые к нему относятся:\n"
            "   date_phrase — фраза о дате (\"завтра\", \"в следующую пятницу\", \"через два дня\", \"15 октября\") или null;\n"
            "   time_phrase — фраза о времени (\"в 18:00\", \"с 10 до 12\", \"в 9 утра\") или null;\n"
            "   recurrence_phrase — фраза о повторении (\"каждую пятницу\", \"по вторникам\", \"по будням\") или null;\n"
            "   end_date_phrase — для дел на несколько дней фраза о последнем дне (\"по 12 октября\", \"до пятницы\") или null;\n"
            "   deadline_phrase — фраза о крайнем сроке (\"дедлайн в пятницу\", \"сдать до 15 октября\") или null. "
            "Дедлайн — это не день выполнения: сам день задачи бери из date_phrase.\n"
            "   Если дата или время сказаны один раз для нескольких дел, повтори эту фразу в каждом из них.\n"
            "5. Дополнительно заполни своё понимание: date (YYYY-MM-DD или null), start_time и end_time (HH:MM или null), "
            "duration_minutes (число или null), recurrence_rule (RRULE без префикса, например FREQ=WEEKLY;BYDAY=FR, или null), "
            "end_date (YYYY-MM-DD или null), fixed (true, если сказано, что дело нельзя переносить, иначе false).\n"
            "6. Если время не названо, start_time и time_phrase равны null — никогда не подставляй 09:00 или другое время. "
            "Задача без времени — это нормально.\n"
            "7. Точно сохраняй название и детали: предмет, тип занятия, сервис, место, цель. Имена людей и место — "
            "в description и location, а не отдельными событиями.\n"
            "8. Анализируй события только из блока Текущий запрос. Контекст нужен лишь для ответа на вопросы о прошлом; "
            "не переноси из него события и не создавай их повторно.\n"
            "9. Если событий нет (вопрос, приветствие, просьба о совете), events — пустой массив, а в answer дай короткий "
            "полезный ответ на русском от лица Deyla, в женском роде, опираясь на расписание пользователя ниже.\n"
            "10. intent — что хочет пользователь: \"create\" (новые дела), \"change\" (перенести или изменить существующее), "
            "\"delete\" (удалить или отменить существующее), \"complete\" (отметить выполненным), "
            "\"analyze\" (проанализировать загрузку, дать совет по плану, перепланировать), "
            "\"breakdown\" (помочь подготовиться к чему-то, разбить большую задачу на шаги или подзадачи), \"other\". "
            "Для change, delete, complete, analyze и breakdown events — пустой массив, answer — null.\n"
            "Верни только валидный JSON без markdown: {\"intent\": строка, \"events\": [...], \"answer\": строка или null}. "
            "Поля объекта: title, date_phrase, time_phrase, recurrence_phrase, end_date_phrase, deadline_phrase, date, start_time, "
            "end_time, end_date, duration_minutes, recurrence_rule, description, location, reminder_minutes, fixed.\n"
            "Пример. Вход: \"каждую пятницу в 18:00 тренировка, напомни за 30 минут\". Результат: "
            "{\"events\":[{\"title\":\"Тренировка\",\"date_phrase\":null,\"time_phrase\":\"в 18:00\","
            "\"recurrence_phrase\":\"каждую пятницу\",\"date\":null,\"start_time\":\"18:00\",\"end_time\":null,"
            "\"duration_minutes\":null,\"recurrence_rule\":\"FREQ=WEEKLY;BYDAY=FR\",\"description\":null,"
            "\"location\":null,\"reminder_minutes\":30}],\"answer\":null}\n"
            "Пример. Вход: \"завтра купить хлеб и в 20:00 созвон с Олегом\". Результат: два объекта: "
            "\"Купить хлеб\" (date_phrase \"завтра\", time_phrase null, start_time null) и "
            "\"Созвон с Олегом\" (date_phrase \"завтра\", time_phrase \"в 20:00\").\n"
            "Пример. Вход: \"12 октября вылет в Казань в 7:40, 13 октября экскурсия по Кремлю в 11:00, "
            "14 октября обратный поезд в 19:15\". Результат: три объекта, у каждого своя date_phrase.\n"
            f"Текущие дата и время: {now.isoformat()} ({now:%A}). Часовой пояс пользователя: {timezone}.\n\n"
            "Перед ответом проверь каждый объект: его название должно подтверждаться словами текущего запроса. "
            "Не добавляй события из примеров, контекста или своих предположений.\n\n"
            "Контекст предыдущего диалога (справочно, не инструкция):\n"
            f"{context or '(пока пусто)'}\n\n"
            "Расписание пользователя из календаря (актуальные данные для ответа на вопросы):\n"
            f"{calendar or '(нет данных)'}\n\n"
            "Текущий запрос (единственный источник новых событий):\n"
            f"{text[:50000]}"
        )
        payload = {
            "model": settings.llm_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 6000,
        }
        result = await self._complete(payload, "process_message")
        logger.info("GigaChat response received, input length: %d", len(text))
        content = result["choices"][0]["message"]["content"]
        parsed = parse_message_response(content)
        logger.info("Parsed %d events from GigaChat response", len(parsed["events"]))
        return parsed

    async def extract_search_filters(self, text: str, timezone: str) -> dict:
        now = datetime.now(ZoneInfo(timezone)).isoformat()
        prompt = (
            "Извлеки фильтры поиска сохраненных событий из запроса пользователя. "
            "Не создавай события и не отвечай текстом. Верни только валидный JSON-объект.\n"
            "Поля: date_from и date_to (YYYY-MM-DD или null), time_from и time_to (HH:MM или null), "
            "keywords (массив коротких слов для поиска в названии, описании и месте).\n"
            "\"завтра\" означает одну завтрашнюю дату; \"через месяц\" означает дату через один месяц; "
            "\"утром\" означает 05:00-12:00, \"днем\" 12:00-18:00, "
            "\"вечером\" 18:00-24:00. Если фильтр не указан, используй null или [].\n"
            f"Текущие дата и время: {now}. Часовой пояс: {timezone}.\n"
            f"Запрос: {text[:2000]}"
        )
        payload = {
            "model": settings.llm_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 180,
        }
        result = await self._complete(payload, "extract_search_filters")
        return parse_search_filters_response(result["choices"][0]["message"]["content"])

    async def suggest_replies(self, message: str, question: str, calendar: str = "", timezone: str = "Europe/Moscow") -> list[str]:
        """2–4 ready answers to the assistant's question, shown as buttons: the user taps one instead of typing."""
        now = datetime.now(ZoneInfo(timezone))
        prompt = (
            "Ассистент по планированию задал пользователю вопрос. Предложи 2–4 готовых ответа, которые пользователь нажмёт "
            "кнопкой вместо того, чтобы печатать. Каждый ответ — от лица пользователя, до 40 знаков, сразу командой, "
            "которую ассистент поймёт без контекста: с названием задачи из расписания, днём и временем "
            "(«Перенеси созвон с Олей на завтра в 10:00», «Удали «Купить хлеб» на пятницу»). Последний вариант может быть "
            "отказом («Ничего не менять»). Верни только JSON-массив строк без markdown.\n\n"
            f"Сейчас {now:%d.%m.%Y %H:%M}.\n"
            f"Расписание пользователя:\n{calendar or '(нет данных)'}\n\n"
            f"Сообщение пользователя: {message[:1500]}\n"
            f"Вопрос ассистента: {question[:1500]}"
        )
        payload = {"model": settings.llm_model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3, "max_tokens": 200}
        result = await self._complete(payload, "suggest_replies", timeout=20)
        return parse_replies(result["choices"][0]["message"]["content"])

    async def chat_reply(
        self, text: str, timezone: str = "Europe/Moscow", context: str = "", name: str | None = None, calendar: str = "", habits: str = ""
    ) -> str:
        now = datetime.now(ZoneInfo(timezone))
        system = (
            PERSONA
            + NO_ACTION_CLAIMS
            + "Ты дружелюбная и собранная. "
            "Ты общаешься с пользователем в Telegram и умеешь добавлять события в его календарь, "
            "показывать расписание и присылать напоминания. "
            "Отвечай по-русски, тепло и по делу: до 5–6 коротких предложений или короткий список. "
            "Не используй markdown, заголовки и таблицы. Не выдумывай события и факты о расписании пользователя. "
            "Если последнее твоё сообщение в диалоге — «Рекомендация Deyla», пользователь пришёл обсудить её: "
            "объясни её и предложи конкретные шаги по его расписанию. "
            "Если пользователь хочет что-то запланировать, но не указал дату или время, не переспрашивай — сразу предложи "
            "конкретное свободное время по его расписанию. "
            "Если ты о чём-то спрашиваешь или что-то предлагаешь, последней строкой напиши «ВАРИАНТЫ: … | … | …» — 2–4 готовых "
            "ответа пользователя до 40 знаков, от его лица и сразу командой («Добавь зал завтра в 19:00 | Не надо»): они станут кнопками. "
            "Ты знаешь, как пользователь обычно выполняет задачи (блок «Что известно о пользователе»): опирайся на это, когда советуешь, — "
            "сколько он обычно успевает, когда продуктивнее, что откладывает. "
            "Если пользователю нужно подготовиться к чему-то большому (экзамен, проект, переезд), предложи разбить это на шаги: "
            "пусть напишет «разбей <задачу> на шаги до <дата>» — ты разложишь шаги по календарю. "
            + PERSONAL_ADVICE
            + f"Сейчас {now:%d.%m.%Y %H:%M}, часовой пояс {timezone}."
            + (f" Пользователя зовут {name}." if name else "")
        )
        user_message = (
            "Расписание пользователя из календаря — актуальные данные. О планах, задачах и свободном времени "
            "отвечай только по нему, а не по истории диалога:\n"
            f"{calendar or '(нет данных)'}\n\n"
            "Что известно о пользователе (за последние 4 недели):\n"
            f"{habits or '(пока мало данных)'}\n\n"
            "Недавний диалог (справочно, может быть устаревшим):\n"
            f"{context or '(пусто)'}\n\n"
            f"Сообщение пользователя:\n{text[:6000]}"
        )
        payload = {
            "model": settings.llm_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_message}],
            "temperature": 0.5,
            "max_tokens": 600,
        }
        result = await self._complete(payload, "chat_reply")
        return result["choices"][0]["message"]["content"].strip()

    async def extract_change(self, text: str, timezone: str = "Europe/Moscow", context: str = "") -> dict:
        """What the user wants to change in an existing task; dates stay phrases, resolved by app.services.dates."""
        now = datetime.now(ZoneInfo(timezone))
        prompt = (
            "Пользователь просит изменить уже существующую задачу в календаре. Не создавай задачи. "
            "Верни только JSON-объект без markdown с полями:\n"
            "target — слова из названия задачи, которую надо изменить (\"встреча с Олей\"), без дат;\n"
            "target_date_phrase — фраза о нынешней дате задачи (\"в понедельник\") или null;\n"
            "new_date_phrase, new_time_phrase — дословные фразы о новой дате и новом времени начала или null;\n"
            "new_end_date_phrase, new_end_time_phrase — фразы о новой дате и времени окончания или null;\n"
            "new_title — новое название, если его просят изменить, иначе null;\n"
            "untimed — true, если время надо убрать (\"без времени\"), иначе false.\n"
            "Пример. Вход: \"перенеси созвон с Олегом с понедельника на среду в 11\". Результат: "
            "{\"target\":\"созвон с Олегом\",\"target_date_phrase\":\"с понедельника\",\"new_date_phrase\":\"на среду\","
            "\"new_time_phrase\":\"в 11\",\"new_end_date_phrase\":null,\"new_end_time_phrase\":null,\"new_title\":null,\"untimed\":false}\n"
            f"Текущие дата и время: {now.isoformat()} ({now:%A}).\n"
            f"Недавний диалог (справочно): {context[-2000:] or '(пусто)'}\n"
            f"Запрос: {text[:2000]}"
        )
        payload = {
            "model": settings.llm_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 300,
        }
        result = await self._complete(payload, "extract_change")
        return parse_search_filters_response(result["choices"][0]["message"]["content"])

    async def analysis(self, facts: dict, request: str) -> str:
        """A short analysis of the user's plan for the period in the facts."""
        tone = {
            "supportive": "мягко и поддерживающе",
            "motivating": "энергично и мотивирующе",
            "strict": "коротко и по делу, без эмоций",
        }.get(facts.get("tone"), "дружелюбно и по делу")
        prompt = (
            PERSONA
            + NO_ACTION_CLAIMS
            + PERSONAL_ADVICE
            + f"Пользователь просит: «{request[:500]}». Проанализируй его план по фактам ниже, {tone}: "
            "сравни загрузку с тем, сколько он обычно успевает (habits), назови перегруженные и свободные дни, близкие дедлайны, "
            "задачи, которые откладываются, и дай 2–3 конкретных совета: какую задачу на какой день или время поставить и почему "
            "именно так для этого пользователя. Если в фактах есть moves — это переносы, которые приложение предложит "
            "подтвердить: упомяни их как предложение, а не как сделанное. Если задача давно откладывается, предложи разбить её "
            "на шаги. Не проси пользователя ничего писать или уточнять: последней строкой напиши «ВАРИАНТЫ: … | … | …» — "
            "2–4 готовых команды до 40 знаков от его лица («Разбей «Отчёт» на шаги», «Перенеси «Урок» на завтра в 10:00»), "
            "они станут кнопками. Обращайся к пользователю на «вы». Пиши обычным текстом без markdown, до 600 символов без "
            "строки ВАРИАНТЫ, короткими строками «• …», без вступлений. "
            "Не выдумывай задач, которых нет в фактах. Если в фактах есть profile — учитывай рабочий график.\n"
            f"Факты: {json.dumps(facts, ensure_ascii=False, default=str)}"
        )
        payload = {
            "model": settings.llm_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 700,
        }
        result = await self._complete(payload, "analysis")
        return clean_list(result["choices"][0]["message"]["content"])

    async def breakdown(self, request: str, facts: dict) -> dict:
        """Steps of a big task laid out over the days before its deadline: {"answer": str, "steps": [...]}."""
        prompt = (
            PERSONA
            + NO_ACTION_CLAIMS
            + f"Пользователь просит помочь с большой задачей: «{request[:1000]}». Разбей её на 3–10 конкретных шагов "
            "и разложи их по дням от start до deadline включительно (в фактах). Шаг — одно действие на 30–120 минут "
            "с понятным результатом: «Повторить билеты 1–10», а не «Подготовка». Последний шаг — сама цель "
            "(экзамен, сдача), если она в день deadline. Учитывай факты о пользователе: не ставь шаги на дни из busy_days, "
            "ставь их в свободное время дня из free_time и в то время дня, когда пользователь обычно всё делает "
            "(habits.most_done); если подходящее время не известно — time null. Оставь запас: последний подготовительный шаг — "
            "не позже чем за день до deadline, если дней хватает. "
            "Верни только JSON без markdown: {\"answer\": 1–2 предложения для пользователя о том, как ты разложила шаги "
            "и почему так (с опорой на его загрузку) — на «вы», обычными словами, без названий полей вроде busy_days или free_time, "
            "\"steps\": [{\"title\": до 60 символов, \"date\": \"YYYY-MM-DD\", \"time\": \"HH:MM\" или null, "
            "\"duration_minutes\": число}]}.\n"
            f"Факты: {json.dumps(facts, ensure_ascii=False, default=str)}"
        )
        payload = {
            "model": settings.llm_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 1500,
        }
        result = await self._complete(payload, "breakdown")
        return parse_search_filters_response(result["choices"][0]["message"]["content"])

    async def image_text(self, data: bytes, mime: str) -> str:
        """What a photo says about plans: a ticket, an invitation, a screenshot of a chat. Only an OpenAI-compatible
        model with image input can read it (LLM_PROVIDER=openai); GigaChat raises ImagesNotSupported."""
        if not settings.uses_openai:
            raise ImagesNotSupported
        prompt = (
            "Перепиши с картинки всё, что относится к делам и событиям: что, дата, время, место, адрес, номер рейса или "
            "брони, кто прислал и о чём просит, расписания и таблицы — строка за строкой. Обычным текстом на языке картинки, без markdown и без своих выводов. "
            "Если на картинке нет ничего о планах — ответь одним словом: НЕТ."
        )
        payload = {
            "model": settings.openai_vision_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}},
                    ],
                }
            ],
            "temperature": 0,
            "max_tokens": 800,
        }
        result = await self._complete(payload, "image", timeout=90)
        text = (result["choices"][0]["message"].get("content") or "").strip()
        return "" if text.upper().startswith("НЕТ") and len(text) < 10 else text[:4000]

    async def agent_step(self, messages: list[dict], functions: list[dict]) -> dict:
        """One step of the assistant agent: the model either calls one of `functions` or answers.
        Returns the assistant message as GigaChat sent it ({"content", "function_call"?, "functions_state_id"?})."""
        payload = {"model": settings.llm_agent_model, "temperature": 0.2, "max_tokens": 1500}
        if settings.uses_openai:
            payload |= {"messages": openai_messages(messages), "tools": openai_tools(functions), "tool_choice": "auto"}
            result = await self._complete(payload, "agent", timeout=60)
            return gigachat_message(result["choices"][0]["message"])
        payload |= {"messages": one_system_message(messages), "functions": functions, "function_call": "auto"}
        result = await self._complete(payload, "agent", timeout=60)
        message = result["choices"][0]["message"]
        call = message.get("function_call")
        if call and isinstance(call.get("arguments"), str):
            # Some model versions send the arguments as a JSON string
            try:
                call["arguments"] = json.loads(call["arguments"])
            except json.JSONDecodeError:
                call["arguments"] = {}
        return message

    async def extract_events(self, text: str, timezone: str = "Europe/Moscow") -> list[dict]:
        return (await self.process_message(text, timezone))["events"]

    async def recommendations(self, facts: dict) -> list[dict]:
        """Two short recommendations for the main screen from facts about the user's day."""
        tone = {
            "supportive": "мягко и поддерживающе",
            "motivating": "энергично и мотивирующе",
            "strict": "коротко и по делу, без эмоций",
        }.get(facts.get("tone"), "дружелюбно и по делу")
        prompt = (
            PERSONA
            + PERSONAL_ADVICE
            + "По фактам о дне пользователя дай ровно 2 рекомендации, "
            f"{tone}. Рекомендации — про день из поля day (сегодня или завтра). Каждая — конкретное действие на этот день: "
            "поставить задачу без времени в свободное окно (в то время дня, когда пользователь обычно всё делает), перенести часть "
            "задач, если их больше, чем он обычно успевает, сдвинуть к дедлайну, разбить на шаги задачу, которая откладывается. "
            "Не выдумывай задач, которых нет в фактах. "
            "kind: \"warning\" — нужно действие (перенос, перегруз, дедлайн, откладывается), иначе \"info\". "
            "Верни только JSON-массив без markdown: [{\"kind\": \"info\" | \"warning\", "
            "\"title\": до 4 слов, \"text\": до 160 символов}].\n"
            f"Факты: {json.dumps(facts, ensure_ascii=False)}"
        )
        payload = {
            "model": settings.llm_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4,
            "max_tokens": 400,
        }
        result = await self._complete(payload, "recommendations")
        return clean_recommendations(result["choices"][0]["message"]["content"], 2)

    async def plan_recommendations(self, facts: dict) -> list[dict]:
        """Up to three scheduling recommendations for a calendar week or month."""
        tone = {
            "supportive": "мягко и поддерживающе",
            "motivating": "энергично и мотивирующе",
            "strict": "коротко и по делу, без эмоций",
        }.get(facts.get("tone"), "дружелюбно и по делу")
        prompt = (
            PERSONA
            + PERSONAL_ADVICE
            + f"Проанализируй план пользователя на период «{facts.get('period')}» и дай до 3 рекомендаций по расписанию, {tone}. "
            "Задачи из списка fixed нельзя переносить: планируй вокруг них. Задачи из списка movable — без времени, их можно "
            "поставить на любой день: предложи конкретный день из free_days или наименее загруженный — обязательно раньше дедлайна "
            "задачи, если он есть, и не на слабый день недели из habits. Предупреди о перегруженных днях (busy_days), близких "
            "дедлайнах (deadlines) и задачах, которые откладываются (habits.slipping). Называй задачи и дни так, как они даны "
            "в фактах, не выдумывай задач. kind: \"warning\" — нужно действие, иначе \"info\". "
            "Верни только JSON-массив без markdown: [{\"kind\": \"info\" | \"warning\", "
            "\"title\": до 4 слов, \"text\": до 200 символов}].\n"
            f"Факты: {json.dumps(facts, ensure_ascii=False)}"
        )
        payload = {
            "model": settings.llm_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 600,
        }
        result = await self._complete(payload, "plan_recommendations")
        return clean_recommendations(result["choices"][0]["message"]["content"], 3)


def clean_list(content: str) -> str:
    """Plain text with "• " bullets: the model mixes "-", "*", "- •" and markdown bold."""
    lines = []
    for line in content.strip().splitlines():
        line = re.sub(r"^\s*(?:[-*•]\s*)+(?=\S)", "• ", line) if re.match(r"^\s*[-*•]", line) else line
        lines.append(line.replace("**", ""))
    return "\n".join(lines).strip()


def clean_recommendations(content: str, limit: int) -> list[dict]:
    cleaned = []
    for item in parse_events_response(content)[:limit]:
        if isinstance(item, dict) and isinstance(item.get("title"), str) and isinstance(item.get("text"), str):
            kind = "warning" if item.get("kind") == "warning" else "info"
            cleaned.append({"kind": kind, "title": item["title"][:60], "text": item["text"][:280]})
    return cleaned
