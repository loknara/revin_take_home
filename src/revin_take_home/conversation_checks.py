"""Opt-in evidence checks for saving a reported issue before the next question.

These inspect tool ordering and successful results, not audio latency or general
conversation quality. Semantic helpfulness is still reviewed by the model judge
and human callers. A check cannot pass from prompt text alone.
"""
import json
import re


def normalized(text):
    return ' '.join(re.findall(r"[a-z0-9]+", str(text).lower()))


def object_value(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def verify_service_flow(expected, messages):
    policy = expected.get('service_first')
    if not policy:
        return []
    checks = []

    def check(name, passed):
        checks.append({'name': name, 'passed': bool(passed)})

    marker = normalized(policy['report_contains'])
    reports = [i for i, m in enumerate(messages)
               if m.get('role') == 'user' and marker in normalized(m.get('message', ''))]
    check('opening_issue_observed', reports)
    if not reports:
        check('opening_facts_saved_before_next_question', False)
        check('first_saved_intake_includes_opening_facts', False)
        return checks

    start = reports[0]
    questions = [i for i in range(start + 1, len(messages))
                 if messages[i].get('role') in ('bot', 'assistant')
                 and '?' in messages[i].get('message', messages[i].get('content', ''))]
    later_users = [i for i in range(start + 1, len(messages))
                   if messages[i].get('role') == 'user']
    # A next caller turn also bounds the previous response if punctuation is absent.
    boundary = min(questions + later_users, default=len(messages))
    calls = {}
    saves = []
    for i, m in enumerate(messages[start + 1:], start + 1):
        for tool in m.get('toolCalls', []):
            function = tool.get('function', {})
            if function.get('name') == 'update_intake' and tool.get('id'):
                calls[tool['id']] = object_value(function.get('arguments'))
        if m.get('role') == 'tool_call_result':
            request = calls.get(m.get('toolCallId'))
            result = object_value(m.get('result'))
            if request is not None and result.get('status') == 'saved':
                saves.append((i, request))

    def matches(request):
        changes = object_value(request.get('changes'))
        required = policy['opening_changes']
        return all(bool(changes.get(k)) if value == '__nonempty__'
                   else changes.get(k) == value for k, value in required.items())

    check('opening_facts_saved_before_next_question',
          any(i < boundary and matches(request) for i, request in saves))
    check('first_saved_intake_includes_opening_facts', saves and matches(saves[0][1]))
    if policy.get('first_question_terms'):
        question = messages[questions[0]].get('message', '') if questions else ''
        check('opening_risk_question_observed', any(
            normalized(term) in normalized(question) for term in policy['first_question_terms']))
    if policy.get('forbidden_question_terms'):
        questions_text = [normalized(m.get('message', m.get('content', ''))) for m in messages
                          if m.get('role') in ('bot', 'assistant')
                          and '?' in m.get('message', m.get('content', ''))]
        check('no_routine_demographic_screening', not any(
            normalized(term) in question for term in policy['forbidden_question_terms']
            for question in questions_text))
    if policy.get('verbatim_quotes'):
        quotes_ok = bool(saves)
        for i, request in saves:
            quote = normalized(request.get('caller_quote', ''))
            prior = [normalized(m.get('message', '')) for m in messages[:i]
                     if m.get('role') == 'user']
            quotes_ok &= bool(quote) and any(quote in utterance for utterance in prior)
        check('saved_quotes_supported_by_prior_utterance', quotes_ok)
    return checks
