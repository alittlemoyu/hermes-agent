"""Bridge factmemory confirmation payloads into the interactive clarify tool."""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


ConfirmationCallback = Callable[[str, Optional[List[Any]]], str]


def maybe_bridge_factmemory_clarify(
    tool_name: str,
    result: Any,
    callback: Optional[ConfirmationCallback],
) -> Any:
    """Ask the user when a factmemory tool result explicitly needs confirmation.

    Factmemory returns structured confirmation hints such as
    ``needs_confirmation``, ``clarify_options`` and ``confirmation_questions``.
    The model often treats those as ordinary tool output and continues.  This
    bridge makes the confirmation boundary executable by invoking Hermes'
    existing clarify callback and appending the response to the same JSON
    result for the next model step.
    """
    if not isinstance(tool_name, str) or not tool_name.startswith("factmemory_"):
        return result
    if not isinstance(result, str):
        return result

    try:
        payload = json.loads(result)
    except Exception:
        return result
    if not isinstance(payload, dict) or payload.get("clarify_bridge"):
        return result

    request = _build_confirmation_request(payload)
    if request is None:
        return result

    question, raw_choices = request
    choices = _display_choices(raw_choices)
    bridge: Dict[str, Any] = {
        "needed": True,
        "asked": False,
        "question": question,
        "choices_offered": choices,
        "raw_choices": raw_choices,
    }

    if callback is None:
        bridge["reason"] = "clarify_callback_unavailable"
    else:
        try:
            response = callback(question, choices)
            bridge.update({
                "asked": True,
                "user_response": str(response).strip(),
                "next_instruction": (
                    "Use user_response to choose the explicit factmemory next "
                    "step; do not guess or auto-confirm conflicting candidates."
                ),
            })
        except Exception as exc:
            bridge.update({
                "error": f"clarify_failed: {exc}",
                "next_instruction": "Clarify failed; report the confirmation need to the user.",
            })

    payload["clarify_bridge"] = bridge
    return json.dumps(payload, ensure_ascii=False)


def _build_confirmation_request(payload: Dict[str, Any]) -> Optional[Tuple[str, Optional[List[Any]]]]:
    item_with_choices = _first_item_with_choices(payload)
    if item_with_choices is not None:
        label = _item_label(item_with_choices)
        return (f"factmemory 需要确认计划项「{label}」如何处理：", item_with_choices.get("clarify_options"))

    questions = _string_list(payload.get("confirmation_questions"))
    if questions:
        return (_format_questions(questions), _first_choices(payload))

    lock_questions = _string_list(payload.get("lock_confirmation_questions"))
    if lock_questions:
        return (_format_questions(lock_questions), _first_choices(payload))

    if _contains_truthy_key(payload, "needs_confirmation") or payload.get("requires_user_confirmation"):
        return ("factmemory 需要人工确认后才能继续。请说明你希望我怎么处理：", _first_choices(payload))

    commands = payload.get("confirmation_commands")
    if isinstance(commands, list) and commands:
        return ("factmemory 有待确认候选。请选择确认、拒绝，或说明要继续查看：", _command_choices(commands))

    return None


def _format_questions(questions: List[str]) -> str:
    if len(questions) == 1:
        return questions[0]
    bullets = "\n".join(f"- {q}" for q in questions[:3])
    extra = "" if len(questions) <= 3 else f"\n- 另有 {len(questions) - 3} 个候选先不在本次询问中展开"
    return f"factmemory 需要你先确认一个方向：\n{bullets}{extra}"


def _display_choices(choices: Optional[List[Any]]) -> Optional[List[str]]:
    if not choices:
        return None
    display: List[str] = []
    for choice in choices[:7]:
        display.append(_display_choice(choice))
    return display


def _display_choice(choice: Any) -> str:
    if not isinstance(choice, dict):
        return str(choice).strip()
    label = str(
        choice.get("label")
        or choice.get("title")
        or choice.get("name")
        or choice.get("id")
        or choice.get("value")
        or ""
    ).strip()
    entity_id = str(choice.get("entity_id") or choice.get("id") or "").strip()
    kind = str(choice.get("kind") or "").strip()
    confidence = choice.get("confidence")
    description = str(choice.get("description") or choice.get("detail") or "").strip()

    bits = [label or entity_id or description]
    if entity_id and entity_id not in bits[0] and entity_id != kind:
        bits.append(entity_id)
    if kind and kind not in bits:
        bits.append(kind)
    if confidence is not None:
        bits.append(f"confidence={confidence}")

    first_line = " | ".join(bit for bit in bits if bit)
    if description and description != first_line:
        return f"{first_line}\n  {description}"
    return first_line


def _first_item_with_choices(value: Any) -> Optional[Dict[str, Any]]:
    for item in _walk_dicts(value):
        choices = item.get("clarify_options")
        if item.get("needs_confirmation") and isinstance(choices, list) and choices:
            return item
    for item in _walk_dicts(value):
        choices = item.get("clarify_options")
        if isinstance(choices, list) and choices:
            return item
    return None


def _first_choices(value: Any) -> Optional[List[Any]]:
    item = _first_item_with_choices(value)
    if item is not None:
        choices = item.get("clarify_options")
        if isinstance(choices, list) and choices:
            return choices
    return None


def _command_choices(commands: List[Any]) -> List[Dict[str, str]]:
    choices: List[Dict[str, str]] = []
    seen = set()
    for command in commands:
        if not isinstance(command, dict):
            continue
        for action in ("confirm", "reject", "get_full", "promote", "mark_stale"):
            spec = command.get(action)
            if not isinstance(spec, dict):
                continue
            key = f"{action}:{spec.get('candidate_id') or spec.get('action') or len(choices)}"
            if key in seen:
                continue
            seen.add(key)
            choices.append({
                "id": key,
                "label": action,
                "description": json.dumps(spec, ensure_ascii=False),
            })
            if len(choices) >= 7:
                return choices
    return choices


def _item_label(item: Dict[str, Any]) -> str:
    for key in ("text", "name", "title", "entity_id", "id"):
        value = item.get(key)
        if value:
            return str(value)
    return "这个候选项"


def _contains_truthy_key(value: Any, key: str) -> bool:
    for item in _walk_dicts(value):
        if item.get(key):
            return True
    return False


def _walk_dicts(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
