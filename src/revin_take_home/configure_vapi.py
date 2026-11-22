"""Review/apply the versioned demo assistant config, or restore a saved deployment."""
import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx

from .audit import config_fingerprint
from .evaluation import ALLOWED_TOOLS, ROOT

FAILURE_MESSAGE = (
    "Summit Air's demo assistant is temporarily unavailable. No service request has been saved. "
    "Please try again later. If you smell gas, leave the area with everyone and call 911 once safely away."
)


def private_json(path, value):
    path.parent.mkdir(parents=True,exist_ok=True);path.parent.chmod(0o700)
    path.write_text(json.dumps(value,indent=2));path.chmod(0o600)


def assistant_patch(live, tool_ids, backend, credential):
    model=copy.deepcopy(live['model'])
    model['messages']=[{'role':'system','content':(ROOT/'prompts/summit-air-system.txt').read_text()}]
    model.pop('tools',None);model.pop('toolRefs',None);model['toolIds']=tool_ids
    return {'model':model,'server':{'url':backend+'/vapi/events','credentialId':credential,'timeoutSeconds':5},
            'serverMessages':['end-of-call-report'],
            'firstMessage':"Hi, this is Summit Air's AI assistant for a demo. How can I help?"}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply',action='store_true')
    p.add_argument('--phone-id')
    p.add_argument('--backend-url',default='https://revin-take-home-wt93.onrender.com')
    p.add_argument('--restore',type=Path,help='Restore assistant and phone settings from a prior backup.json; requires --apply')
    args=p.parse_args(argv)
    if not args.apply:
        print(json.dumps({'action':'plan_only','config':config_fingerprint(),'tools':sorted(ALLOWED_TOOLS),
                          'backend':args.backend_url,'phone_fallback':FAILURE_MESSAGE,'restore':str(args.restore) if args.restore else None},indent=2));return 0
    if not args.backend_url.startswith('https://'):p.error('Use HTTPS')
    with httpx.Client(base_url='https://api.vapi.ai',headers={'Authorization':'Bearer '+os.environ['VAPI_API_KEY']},timeout=30) as api:
        aid=os.environ['VAPI_ASSISTANT_ID']
        if args.restore:
            backup=json.loads(args.restore.read_text())
            if backup['assistant']['id']!=aid:p.error('Backup assistant differs from configured assistant')
            old=backup['assistant']
            patch={k:old.get(k) for k in ('model','server','serverMessages','firstMessage')}
            api.patch('/assistant/'+aid,json=patch).raise_for_status()
            phone=backup['phone']
            api.patch('/phone-number/'+phone['id'],json={'hooks':phone.get('hooks',[])}).raise_for_status()
            print('Restored previous assistant and phone hooks; no call started.');return 0
        if not args.phone_id:p.error('--phone-id is required with --apply')
        with httpx.Client(timeout=20) as backend:
            r=backend.get(args.backend_url+'/version');r.raise_for_status()
            if r.json().get('prompt_tools_sha256')!=config_fingerprint()['prompt_tools_sha256']:
                raise RuntimeError('Backend prompt/tool version differs; wait for deployment before activating')
        live=api.get('/assistant/'+aid);live.raise_for_status();live=live.json()
        baseline=json.loads((ROOT/'config/voice-baseline.json').read_text())
        for section,fields in baseline.items():
            if any(live.get(section,{}).get(k)!=v for k,v in fields.items()):
                raise RuntimeError('Live '+section+' differs from the versioned baseline; reconcile before applying')
        phone=api.get('/phone-number/'+args.phone_id);phone.raise_for_status();phone=phone.json()
        if phone.get('assistantId') or phone.get('server',{}).get('url')!=args.backend_url+'/vapi/assistant-request':
            raise RuntimeError('Phone routing is not the expected dynamic assistant backend')
        servers=[]
        for tid in live['model'].get('toolIds',[]):
            t=api.get('/tool/'+tid);t.raise_for_status();t=t.json()
            if t.get('function',{}).get('name') in ALLOWED_TOOLS|{'resolve_membership'}:servers.append(t.get('server',{}))
        creds={s.get('credentialId') for s in servers}
        if not servers or len(creds)!=1 or None in creds:raise RuntimeError('Missing/ambiguous existing server credential')
        credential=creds.pop()
        if any(s.get('url')!=args.backend_url+'/vapi/tools' for s in servers):raise RuntimeError('Unexpected existing tool backend')
        folder=ROOT/'eval-results'/('deployment-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+str(uuid4())[:8])
        private_json(folder/'backup.json',{'assistant':live,'phone':phone})
        ids=[]
        # Create new versions rather than mutating tools used by calls already in progress.
        for name in sorted(ALLOWED_TOOLS):
            tool=json.loads((ROOT/f'tools/{name}.json').read_text())
            tool['server']={'url':args.backend_url+'/vapi/tools','credentialId':credential,'timeoutSeconds':10}
            r=api.post('/tool',json=tool);r.raise_for_status();ids.append(r.json()['id'])
            private_json(folder/'created-tools.json',ids)
        patch=assistant_patch(live,ids,args.backend_url,credential)
        api.patch('/assistant/'+aid,json=patch).raise_for_status()
        hooks=[h for h in phone.get('hooks',[]) if h.get('on')!='call.ending']
        hooks.append({'on':'call.ending','do':{'type':'say','exact':FAILURE_MESSAGE}})
        api.patch('/phone-number/'+args.phone_id,json={'hooks':hooks}).raise_for_status()
        readback=api.get('/assistant/'+aid);readback.raise_for_status();readback=readback.json()
        checked_phone=api.get('/phone-number/'+args.phone_id);checked_phone.raise_for_status();checked_phone=checked_phone.json()
        if readback['model']['toolIds']!=ids or readback['model']['messages']!=patch['model']['messages']:
            raise RuntimeError('Assistant readback mismatch; use saved backup to restore')
        if checked_phone.get('hooks')!=hooks:raise RuntimeError('Phone hook readback mismatch')
        for key in ('voice','transcriber'):
            if readback.get(key)!=live.get(key):raise RuntimeError('Unexpected voice/transcriber change')
        private_json(folder/'deployed.json',{'assistant':readback,'phone':checked_phone,'version':config_fingerprint()})
        print('Activated versioned tools and prompt; STT/TTS preserved. Backup: '+str(folder/'backup.json'))
    return 0


if __name__=='__main__':raise SystemExit(main())
