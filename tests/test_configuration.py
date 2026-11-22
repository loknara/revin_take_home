from revin_take_home.configure_vapi import assistant_patch, main


def test_plan_needs_no_credentials_or_network(monkeypatch):
    monkeypatch.delenv('VAPI_API_KEY',raising=False)
    assert main([])==0


def test_activation_preserves_model_settings_and_uses_authenticated_events():
    live={'model':{'provider':'openai','model':'gpt-4.1','temperature':0.2,'toolIds':['old']}}
    patch=assistant_patch(live,['new'],'https://demo.invalid','credential')
    assert patch['model']['model']=='gpt-4.1' and patch['model']['temperature']==0.2
    assert live['model']['toolIds']==['old']
    assert 'voice' not in patch and 'transcriber' not in patch
    assert patch['server']['credentialId']=='credential'
    assert patch['serverMessages']==['end-of-call-report']
