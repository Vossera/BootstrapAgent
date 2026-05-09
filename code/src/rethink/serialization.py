from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def write_json(path: Path, data: BaseModel | dict[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, BaseModel):
        text = data.model_dump_json(indent=2)
    else:
        text = json.dumps(data, indent=2, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")


def to_plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [to_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_plain(item) for key, item in value.items()}
    return value


def dump_yaml(data: Any) -> str:
    return _dump_yaml_value(to_plain(data), 0).rstrip() + "\n"


def write_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_yaml(data), encoding="utf-8")


def _dump_yaml_value(value: Any, indent: int) -> str:
    space = " " * indent
    if isinstance(value, dict):
        if not value:
            return "{}\n"
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(f"{space}{key}:")
                lines.append(_dump_yaml_value(item, indent + 2).rstrip())
            else:
                lines.append(f"{space}{key}: {_yaml_scalar(item)}")
        return "\n".join(lines) + "\n"
    if isinstance(value, list):
        if not value:
            return "[]\n"
        lines = []
        for item in value:
            if isinstance(item, dict):
                lines.append(f"{space}-")
                lines.append(_dump_yaml_value(item, indent + 2).rstrip())
            elif isinstance(item, list):
                lines.append(f"{space}-")
                lines.append(_dump_yaml_value(item, indent + 2).rstrip())
            else:
                lines.append(f"{space}- {_yaml_scalar(item)}")
        return "\n".join(lines) + "\n"
    return f"{space}{_yaml_scalar(value)}\n"


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "":
        return '""'
    if "\n" in text:
        return json.dumps(text, ensure_ascii=False)
    if any(ch in text for ch in [":", "#", "{", "}", "[", "]", ",", "&", "*", "!", "|", ">", "'", '"', "%", "@", "`"]):
        return json.dumps(text, ensure_ascii=False)
    if text.strip() != text or text.lower() in {"null", "true", "false", "yes", "no"}:
        return json.dumps(text, ensure_ascii=False)
    return text
