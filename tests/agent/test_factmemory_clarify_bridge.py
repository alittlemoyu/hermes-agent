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
    assert seen["choices"][0] == "门板防撞钉任务 | task/door-bumper\n  关联到已有任务。"
    assert seen["choices"][1] == "新建条目 | create_new_entity\n  确认这是新计划项。"
    assert bridged["clarify_bridge"]["asked"] is True
    assert bridged["clarify_bridge"]["raw_choices"][1]["id"] == "create_new_entity"
    assert bridged["clarify_bridge"]["user_response"] == "create_new_entity"


def test_factmemory_plan_item_choices_take_priority_over_omission_questions():
    payload = {
        "items": [
            {
                "text": "门板防撞钉后续跟进测试场景",
                "needs_confirmation": True,
                "clarify_options": [
                    {"id": "problem/door_bumper_pin_process", "label": "门板防撞钉工艺打孔位置分歧"},
                    {"id": "create_new_entity", "label": "新建条目"},
                ],
            }
        ],
        "confirmation_questions": [
            "计划项没有确认关联 entity：需要新建，还是关联到已有实体？",
            "`problem/arc_bend_quality` 近期活跃/阻塞但未在计划中，是否遗漏？",
        ]
    }
    seen = {}

    result = maybe_bridge_factmemory_clarify(
        "factmemory_project",
        json.dumps(payload, ensure_ascii=False),
        lambda question, choices: seen.setdefault("request", (question, choices)) or "ignored",
    )
    bridged = json.loads(result)

    question, choices = seen["request"]
    assert question == "factmemory 需要确认计划项「门板防撞钉后续跟进测试场景」如何处理："
    assert "arc_bend_quality" not in question
    assert choices == [
        "门板防撞钉工艺打孔位置分歧 | problem/door_bumper_pin_process",
        "新建条目 | create_new_entity",
    ]
    assert bridged["clarify_bridge"]["raw_choices"][0]["id"] == "problem/door_bumper_pin_process"


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
