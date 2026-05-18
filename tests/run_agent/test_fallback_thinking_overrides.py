"""Fallback request overrides for providers with non-portable thinking state."""

from run_agent import AIAgent


def test_xiaomi_fallback_disables_thinking_and_strips_reasoning_fields():
    agent = object.__new__(AIAgent)
    agent.provider = "xiaomi"
    agent._fallback_activated = True

    api_kwargs = {
        "extra_body": {"foo": "bar"},
        "messages": [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": "tool call",
                "reasoning": "old provider reasoning",
                "reasoning_content": "old provider reasoning_content",
                "reasoning_details": [{"type": "summary", "text": "old"}],
            },
        ],
    }

    result = agent._apply_fallback_request_overrides(api_kwargs)

    assert result["extra_body"] == {
        "foo": "bar",
        "thinking": {"type": "disabled"},
    }
    assistant_msg = result["messages"][1]
    assert "reasoning" not in assistant_msg
    assert "reasoning_content" not in assistant_msg
    assert "reasoning_details" not in assistant_msg


def test_fallback_override_is_noop_for_primary_or_generic_provider():
    agent = object.__new__(AIAgent)
    agent.provider = "custom"
    agent._fallback_activated = True

    api_kwargs = {
        "messages": [
            {
                "role": "assistant",
                "content": "ok",
                "reasoning_content": "keep me",
            },
        ],
    }

    assert agent._apply_fallback_request_overrides(api_kwargs) is api_kwargs
    assert api_kwargs["messages"][0]["reasoning_content"] == "keep me"

    agent.provider = "xiaomi"
    agent._fallback_activated = False
    assert agent._apply_fallback_request_overrides(api_kwargs) is api_kwargs
    assert api_kwargs["messages"][0]["reasoning_content"] == "keep me"
