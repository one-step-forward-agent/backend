"""Conservative zero-LLM commands and optional semantic tool-family retrieval."""
import json
import logging
import re

from services.embeddings import embeddings

logger = logging.getLogger(__name__)
APPS = {
    "notepad": "notepad", "блокнот": "notepad", "calculator": "calculator", "калькулятор": "calculator",
    "paint": "paint", "пэйнт": "paint", "explorer": "explorer", "проводник": "explorer",
    "word": "word", "ворд": "word", "excel": "excel", "эксель": "excel",
    "powerpoint": "powerpoint", "power point": "powerpoint", "settings": "settings", "параметры": "settings",
    "task manager": "task_manager", "диспетчер задач": "task_manager", "browser": "browser", "браузер": "browser",
}
FOLDERS = {"desktop": "Desktop", "рабочий стол": "Desktop", "documents": "Documents", "документы": "Documents",
           "downloads": "Downloads", "загрузки": "Downloads", "pictures": "Pictures", "изображения": "Pictures"}


def direct_command(text):
    """Full matches only: no negation, chained instructions, guessed paths or arguments."""
    text = text.strip().rstrip(".!?").strip()
    match = re.fullmatch(r"(?:открой|запусти|open|launch|start)\s+(.+)", text, re.I)
    if match:
        argument = match[1].lower()
        if argument in APPS:
            return {"name": "open_app", "arguments": {"app": APPS[argument]}}
    if re.fullmatch(r"(?:system info|system information|сведения о компьютере|информация о системе)", text, re.I):
        return {"name": "system_info", "arguments": {}}
    match = re.fullmatch(r"(?:покажи файлы в|покажи папку|list files in|list folder)\s+(.+)", text, re.I)
    if match and match[1].lower() in FOLDERS:
        return {"name": "list_folder", "arguments": {"path": FOLDERS[match[1].lower()]}}
    return None


def direct_turn(history, allowed):
    current = max((i for i, message in enumerate(history) if message["role"] == "user"), default=-1)
    if current < 0:
        return None
    command = direct_command(history[current]["content"])
    if command is None or command["name"] not in allowed:
        return None
    tail = history[current + 1:]
    if not tail:
        return {"action": command}
    if len(tail) != 2 or tail[0].get("function_call") != command or tail[1]["role"] != "function":
        return None
    english = not bool(re.search("[а-яё]", history[current]["content"], re.I))
    try:
        result = json.loads(tail[1]["content"])
    except (ValueError, TypeError):
        return None
    if not isinstance(result, dict):
        return None
    if not result.get("ok"):
        return {"content": ("Action was not completed: " if english else "Действие не выполнено: ") + str(result.get("error", "ошибка"))[:500]}
    if command["name"] == "open_app":
        return {"content": ("Launch requested: " if english else "Запуск запрошен: ") + command["arguments"]["app"]}
    return {"content": json.dumps(result, ensure_ascii=False)[:3500]}


def family(name):
    if name.startswith("browser_"):
        return "browser"
    if name.startswith("windows_") or name == "open_app":
        return "windows"
    if name in {"list_folder", "find_files", "read_text_file", "write_text_file", "create_folder", "move_file", "open_path", "write_spreadsheet", "write_document", "export_document", "convert_document"}:
        return "files"
    return name


async def relevant_tools(history, tools):
    """Retrieve whole families, retaining tools already used. Low confidence uses all tools."""
    query = next((message["content"] for message in reversed(history) if message["role"] == "user"), "")
    if not embeddings.enabled or not query or len(query) > 2000:
        return tools
    try:
        ranked = await embeddings.rank(query, [tool["name"] + ": " + tool["description"] for tool in tools])
    except Exception:
        # Do not log provider error bodies or user queries.
        logger.info("Desktop semantic retrieval unavailable; using full tool catalog")
        return tools
    if not ranked or ranked[0][0] < 0.45:
        return tools
    keep = {family(tools[index]["name"]) for score, index in ranked[:5] if score >= ranked[0][0] - 0.16}
    keep.update(family(message["function_call"]["name"]) for message in history if message.get("function_call"))
    keep.add("files")  # Website/program tasks often need local files and a final output.
    selected = [tool for tool in tools if family(tool["name"]) in keep]
    logger.info("Desktop semantic tool retrieval: %d of %d tools", len(selected), len(tools))
    return selected or tools


def valid_arguments(value, schema):
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict) or any(key not in value for key in schema.get("required", [])):
            return False
        properties = schema.get("properties", {})
        return all(key in properties and valid_arguments(item, properties[key]) for key, item in value.items())
    if kind == "array":
        return isinstance(value, list) and len(value) <= 1000 and all(valid_arguments(item, schema.get("items", {})) for item in value)
    if kind == "string":
        return isinstance(value, str) and len(value) <= schema.get("maxLength", 12000) and ("enum" not in schema or value in schema["enum"])
    if kind == "integer":
        return type(value) is int and schema.get("minimum", -10**9) <= value <= schema.get("maximum", 10**9)
    if kind == "boolean":
        return type(value) is bool
    return True
