import json

from agent.factmemory_clarify_bridge import maybe_bridge_factmemory_clarify


def test_factmemory_plan_confirmation_options_trigger_clarify():
    seen = {}
    payload = {
        "items": [
            {
                "text": "门板防撞钉工艺筹备",
                "needs_confirmation": True,
                "clarify_options": [
                    {
                        "id": "task/door-bumper",
                        "label": "门板防撞钉任务",
                        "description": "关联到已有任务。",
                    },
                    {
                        "id": "create_new_entity",
                        "label": "新建条目",
                        "description": "确认这是新计划项。",
                    },
                ],
            }
        ],
    }

    def callback(question, choices):
        seen["question"] = question
        seen["choices"] = choices
        return "create_new_entity"

    result = maybe_bridge_factmemory_clarify("factmemory_project", json.dumps(payload), callback)
    bridged = json.loads(result)

    assert "门板防撞钉工艺筹备" in seen["question"]
    assert seen["choices"][1]["id"] == "create_new_entity"
    assert bridged["clarify_bridge"]["asked"] is True
    assert bridged["clarify_bridge"]["user_response"] == "create_new_entity"


def test_factmemory_confirmation_questions_trigger_open_ended_clarify():
    payload = {
        "confirmation_questions": [
            "这个计划项应关联到 task/a 还是新建？",
            "是否补充遗漏任务？",
        ]
    }

    result = maybe_bridge_factmemory_clarify(
        "factmemory_project",
        json.dumps(payload, ensure_ascii=False),
        lambda question, choices: "关联 task/a，不补遗漏",
    )
    bridged = json.loads(result)

    assert "这个计划项应关联到 task/a" in bridged["clarify_bridge"]["question"]
    assert bridged["clarify_bridge"]["choices_offered"] is None
    assert bridged["clarify_bridge"]["user_response"] == "关联 task/a，不补遗漏"


def test_factmemory_confirmation_without_callback_is_preserved():
    payload = {"requires_user_confirmation": True}

    result = maybe_bridge_factmemory_clarify("factmemory_entity", json.dumps(payload), None)
    bridged = json.loads(result)

    assert bridged["clarify_bridge"]["needed"] is True
    assert bridged["clarify_bridge"]["asked"] is False
    assert bridged["clarify_bridge"]["reason"] == "clarify_callback_unavailable"


def test_non_factmemory_results_are_unchanged():
    result = '{"needs_confirmation": true}'

    assert maybe_bridge_factmemory_clarify("web_search", result, lambda q, c: "x") == result

