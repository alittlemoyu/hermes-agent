#!/usr/bin/env python3
"""
Clarify Tool Module - Interactive Clarifying Questions

Allows the agent to present structured multiple-choice questions or open-ended
prompts to the user. In CLI mode, choices are navigable with arrow keys. On
messaging platforms, choices are rendered as a numbered list.

The actual user-interaction logic lives in the platform layer (cli.py for CLI,
gateway/run.py for messaging). This module defines the schema, validation, and
a thin dispatcher that delegates to a platform-provided callback.
"""

import json
from typing import Any, List, Optional, Callable


# Maximum number of predefined choices the agent can offer.
# An extra "Other (type your answer)" option is always appended by the UI.
MAX_CHOICES = 7


def _choice_text(choice: Any) -> str:
    """Normalize string or structured choices into display text.

    Structured choices intentionally degrade to plain text so existing CLI,
    TUI, and gateway transports keep working. Richer transports can render the
    first line as a button title and the following lines as detail text.
    """
    if isinstance(choice, dict):
        title = (
            choice.get("title")
            or choice.get("label")
            or choice.get("name")
            or choice.get("id")
            or choice.get("value")
            or ""
        )
        description = (
            choice.get("description")
            or choice.get("detail")
            or choice.get("details")
            or choice.get("body")
            or choice.get("summary")
            or ""
        )
        title = str(title).strip()
        description = str(description).strip()
        if title and description:
            return f"{title}\n  {description}"
        return title or description
    return str(choice).strip()


def clarify_tool(
    question: str,
    choices: Optional[List[str]] = None,
    callback: Optional[Callable] = None,
) -> str:
    """
    Ask the user a question, optionally with multiple-choice options.

    Args:
        question: The question text to present.
        choices:  Up to 7 predefined answer choices. Choices may be strings
                  or objects with title/label plus description. When omitted
                  the question is purely open-ended.
        callback: Platform-provided function that handles the actual UI
                  interaction. Signature: callback(question, choices) -> str.
                  Injected by the agent runner (cli.py / gateway).

    Returns:
        JSON string with the user's response.
    """
    if not question or not question.strip():
        return tool_error("Question text is required.")

    question = question.strip()

    # Validate and trim choices
    if choices is not None:
        if not isinstance(choices, list):
            return tool_error("choices must be a list of strings or choice objects.")
        choices = [_choice_text(c) for c in choices]
        choices = [c for c in choices if c]
        if len(choices) > MAX_CHOICES:
            choices = choices[:MAX_CHOICES]
        if not choices:
            choices = None  # empty list → open-ended

    if callback is None:
        return json.dumps(
            {"error": "Clarify tool is not available in this execution context."},
            ensure_ascii=False,
        )

    try:
        user_response = callback(question, choices)
    except Exception as exc:
        return json.dumps(
            {"error": f"Failed to get user input: {exc}"},
            ensure_ascii=False,
        )

    return json.dumps({
        "question": question,
        "choices_offered": choices,
        "user_response": str(user_response).strip(),
    }, ensure_ascii=False)


def check_clarify_requirements() -> bool:
    """Clarify tool has no external requirements -- always available."""
    return True


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

CLARIFY_SCHEMA = {
    "name": "clarify",
    "description": (
        "Ask the user a question when you need clarification, feedback, or a "
        "decision before proceeding. Supports two modes:\n\n"
        "1. **Multiple choice** — provide up to 7 choices. Choices may be "
        "strings or objects with title/label and description; rich choices "
        "are rendered as title plus detail text where supported. The user "
        "picks one or types their own answer via a 5th 'Other' option.\n"
        "2. **Open-ended** — omit choices entirely. The user types a free-form "
        "response.\n\n"
        "Use this tool when:\n"
        "- The task is ambiguous and you need the user to choose an approach\n"
        "- You want post-task feedback ('How did that work out?')\n"
        "- You want to offer to save a skill or update memory\n"
        "- A decision has meaningful trade-offs the user should weigh in on\n\n"
        "Do NOT use this tool for simple yes/no confirmation of dangerous "
        "commands (the terminal tool handles that). Prefer making a reasonable "
        "default choice yourself when the decision is low-stakes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to present to the user.",
            },
            "choices": {
                "type": "array",
                "items": {
                    "anyOf": [
                        {"type": "string"},
                        {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "title": {"type": "string"},
                                "label": {"type": "string"},
                                "description": {"type": "string"},
                                "value": {"type": "string"},
                            },
                            "additionalProperties": True,
                        },
                    ],
                },
                "maxItems": MAX_CHOICES,
                "description": (
                    "Up to 7 answer choices. Each choice can be a string or "
                    "an object with title/label and description. Omit this "
                    "parameter entirely to ask an open-ended question. When "
                    "provided, the UI automatically appends an 'Other (type "
                    "your answer)' option."
                ),
            },
        },
        "required": ["question"],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="clarify",
    toolset="clarify",
    schema=CLARIFY_SCHEMA,
    handler=lambda args, **kw: clarify_tool(
        question=args.get("question", ""),
        choices=args.get("choices"),
        callback=kw.get("callback")),
    check_fn=check_clarify_requirements,
    emoji="❓",
)
