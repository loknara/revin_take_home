from revin_take_home.configure_vapi import assistant_patch, main


def test_plan_needs_no_credentials_or_network(monkeypatch):
    monkeypatch.delenv("VAPI_API_KEY", raising=False)
    assert main([]) == 0


def test_activation_preserves_model_settings_and_uses_authenticated_events():
    live = {
        "model": {
            "provider": "openai",
            "model": "gpt-4.1",
            "temperature": 0.2,
            "toolIds": ["old"],
        }
    }
    patch = assistant_patch(live, ["new"], "https://demo.invalid", "credential")
    assert patch["model"]["model"] == "gpt-4.1" and patch["model"]["temperature"] == 0.2
    assert live["model"]["toolIds"] == ["old"]
    assert patch["server"]["credentialId"] == "credential"
    assert patch["serverMessages"] == ["end-of-call-report"]


def test_activation_deploys_versioned_voice_settings():
    from revin_take_home.configure_vapi import contains, voice_settings

    live = {
        "model": {"provider": "openai", "model": "gpt-4.1", "temperature": 0.2},
        "voice": {"provider": "vapi", "voiceId": "Elliot", "version": "2"},
    }
    settings = voice_settings()
    patch = assistant_patch(live, ["new"], "https://demo.invalid", "c", settings)
    # Model fields merge (temperature kept); every other section is the versioned one.
    assert patch["model"]["temperature"] == 0.2
    assert contains(patch["model"], settings["model"])
    for section in settings.keys() - {"model"}:
        assert patch[section] == settings[section]
    assert patch["voice"]["chunkPlan"]["minCharacters"] >= 30
    assert patch["transcriber"]["maxEndpointDelayMs"] > 500
    # Readback tolerates provider defaults but not changed values.
    assert contains({"a": 1, "b": {"c": 2, "d": 3}}, {"b": {"c": 2}})
    assert not contains({"b": {"c": 4}}, {"b": {"c": 2}})


def test_plan_shows_voice_settings_and_tool_messages(capsys):
    import json

    assert main([]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["voice_settings"]["transcriber"]["provider"] == "soniox"
    start = plan["tool_messages"]["report_safety_emergency"][0]
    assert start["type"] == "request-start" and start["blocking"] is False
    assert "transfer_call" in plan["tool_messages"]


def test_safety_message_is_non_blocking_and_speaks_911_as_words():
    import json
    import pathlib

    tool = json.loads(pathlib.Path("tools/report_safety_emergency.json").read_text())
    (start,) = tool["messages"]
    assert start["type"] == "request-start" and start["blocking"] is False
    english = next(c["text"] for c in start["contents"] if c["language"] == "en")
    # The evacuation and 911 instruction come first; digits would be read "nine hundred eleven".
    assert english.startswith("Please get everyone out")
    assert "nine one one" in english and "911" not in english


def test_slow_tools_have_own_delay_message_and_transfer_claims_no_callback():
    import json
    import pathlib

    for name in ("search_slots", "propose_booking", "confirm_booking", "update_intake"):
        tool = json.loads(pathlib.Path(f"tools/{name}.json").read_text())
        delayed = [m for m in tool["messages"] if m["type"] == "request-response-delayed"]
        assert delayed and 1000 <= delayed[0]["timingMilliseconds"] < 1700  # Vapi's default fired ~1.7 s in
    raw = pathlib.Path("tools/transfer_call.json").read_text()
    assert "on-call" not in raw.lower()  # D1: there is no on-call person
    transfer = json.loads(raw)
    fallback = transfer["destinations"][0]["transferPlan"]["fallbackPlan"]["message"]
    (failed,) = transfer["messages"]
    # Nothing records a callback when a transfer fails, so nothing spoken may claim one.
    assert "noted" not in fallback.lower()
    assert failed["role"] == "system" and "request_followup" in failed["content"]


def test_tool_creation_reconciliation_requires_exact_config_and_server():
    from revin_take_home.configure_vapi import matching_tool

    desired = {
        "function": {"name": "a"},
        "async": False,
        "messages": [],
        "server": {"url": "https://a", "credentialId": "x"},
    }
    existing = [{"id": "found", **desired}]
    assert matching_tool(existing, desired) == "found"
    assert (
        matching_tool(
            existing, {**desired, "server": {"url": "https://b", "credentialId": "x"}}
        )
        is None
    )


def test_transfer_tool_is_only_built_from_private_env(monkeypatch):
    from revin_take_home.configure_vapi import desired_tools

    monkeypatch.delenv("TRANSFER_NUMBER", raising=False)
    names = [
        t.get("function", {}).get("name", t["type"])
        for t in desired_tools("https://b", "c")
    ]
    assert "transferCall" not in names and "report_safety_emergency" in names
    monkeypatch.setenv("TRANSFER_NUMBER", "(212) 555-0142")
    transfer = desired_tools("https://b", "c")[-1]
    assert transfer["type"] == "transferCall" and "server" not in transfer
    assert transfer["destinations"][0]["number"] == "+12125550142"


def test_repo_transfer_tool_contains_no_real_number():
    import pathlib

    assert "{{TRANSFER_NUMBER}}" in pathlib.Path("tools/transfer_call.json").read_text()
