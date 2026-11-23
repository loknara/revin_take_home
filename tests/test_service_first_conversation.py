"""Regression evidence for delayed intake and omitted outage facts from a phone call."""

import json

import pytest

from revin_take_home.evaluation import verify_conversation


CASE = {
    "expected": {
        "service_first": {
            "report_contains": "my AC is not working",
            "opening_changes": {
                "cooling_unavailable": True,
                "service_type": "repair",
                "service_category": "hvac",
                "issue_summary": "__nonempty__",
            },
            "verbatim_quotes": True,
        }
    }
}


def evidence():
    return [
        {"role": "user", "message": "My AC is not working."},
        {
            "role": "tool_calls",
            "toolCalls": [
                {
                    "id": "save-1",
                    "function": {
                        "name": "update_intake",
                        "arguments": json.dumps(
                            {
                                "caller_quote": "My AC is not working.",
                                "changes": {
                                    "cooling_unavailable": True,
                                    "service_type": "repair",
                                    "service_category": "hvac",
                                    "issue_summary": "AC not working",
                                },
                            }
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool_call_result",
            "toolCallId": "save-1",
            "result": json.dumps({"status": "saved"}),
        },
        {"role": "bot", "message": "I can help arrange service. May I have your name?"},
    ]


def results(messages):
    return {
        c["name"]: c["passed"]
        for c in verify_conversation(CASE, {"messages": messages})
    }


def test_successful_early_save_then_relevant_risk_question():
    assert all(results(evidence()).values())


def test_actual_failure_pattern_late_save_after_address_interview():
    messages = evidence()
    messages.insert(1, {"role": "bot", "message": "What is your full address?"})
    messages.insert(2, {"role": "user", "message": "A synthetic address."})
    checked = results(messages)
    assert not checked["opening_facts_saved_before_next_question"]


@pytest.mark.parametrize(
    "status", ["invalid_arguments", "save_unconfirmed", "revision_conflict"]
)
def test_attempted_but_unsuccessful_save_is_not_enough(status):
    messages = evidence()
    messages[2]["result"] = json.dumps({"status": status})
    assert not results(messages)["opening_facts_saved_before_next_question"]


def test_issue_summary_without_outage_fact_cannot_pass():
    messages = evidence()
    args = json.loads(messages[1]["toolCalls"][0]["function"]["arguments"])
    del args["changes"]["cooling_unavailable"]
    messages[1]["toolCalls"][0]["function"]["arguments"] = args
    assert not results(messages)["first_saved_intake_includes_opening_facts"]


def test_tool_result_for_another_operation_cannot_prove_save():
    messages = evidence()
    messages[2]["toolCallId"] = "unrelated"
    assert not results(messages)["opening_facts_saved_before_next_question"]


def test_quote_cannot_invent_unspoken_address():
    messages = evidence()
    args = json.loads(messages[1]["toolCalls"][0]["function"]["arguments"])
    args["caller_quote"] += " My address is 1217 Example Court."
    messages[1]["toolCalls"][0]["function"]["arguments"] = args
    assert not results(messages)["saved_quotes_supported_by_prior_utterance"]


def test_missing_conversation_does_not_pass():
    assert not results([])["opening_facts_saved_before_next_question"]


def test_following_caller_turn_bounds_save_even_without_question_mark():
    messages = evidence()
    messages[3]["message"] = "Is anyone at home elderly"
    messages.insert(1, {"role": "user", "message": "A synthetic address."})
    assert not results(messages)["opening_facts_saved_before_next_question"]


def test_vulnerability_question_required_for_outage_and_forbidden_when_redundant():
    from revin_take_home.conversation_checks import verify_service_flow

    terms = ["higher risk", "older adult"]

    def checks(policy_key, bot):
        policy = {
            "service_first": {
                "report_contains": "my AC is not working",
                "opening_changes": {"cooling_unavailable": True},
                policy_key: terms,
            }
        }
        messages = [
            {"role": "user", "message": "My AC is not working."},
            {"role": "bot", "message": bot},
        ]
        return {c["name"]: c["passed"] for c in verify_service_flow(policy, messages)}

    ask = "Is anyone at home who'd be at higher risk without AC, like an older adult?"
    other = "Would that timing work, or do you need help sooner?"
    assert (
        checks("required_question_terms", ask)["vulnerability_question_asked"] is True
    )
    assert (
        checks("required_question_terms", other)["vulnerability_question_asked"]
        is False
    )
    assert (
        checks("forbidden_question_terms", ask)["no_redundant_vulnerability_question"]
        is False
    )
    assert (
        checks("forbidden_question_terms", other)["no_redundant_vulnerability_question"]
        is True
    )
