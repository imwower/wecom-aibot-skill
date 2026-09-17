import json
import time

import pytest
from aiohttp.test_utils import TestClient,TestServer

from wecom_bot.automation import Jobs
from wecom_bot.context import ContextAccess
from test_daemon import start_daemon


def fill(jobs):
    for i,chat,owner in [(1,'A','owner'),(2,'B','owner'),(3,'A','other'),(4,'A','owner'),(5,'A','owner')]:
        jobs.enqueue(i,{'msgid':str(i),'aibotid':'bot','chattype':'group','chatid':chat,'from':{'userid':owner}},'history-keyword-'+str(i))
    jobs.attach(1,[{'path':'/tmp/example.txt','kind':'file'}])


def test_scoped_queries_pagination_expiry_and_audit(tmp_path):
    jobs=Jobs(tmp_path/'tasks.sqlite');fill(jobs);api=ContextAccess(jobs);token=api.grant(4,30)
    result=api.query(token,'history',{'limit':1})
    assert [x['message_id'] for x in result['items']]==[4]
    assert result['next_before']==4
    assert [x['message_id'] for x in api.query(token,'history',{'before':4})['items']]==[1]
    assert api.query(token,'message',{'id':2})['items']==[]
    assert api.query(token,'message',{'id':3})['items']==[]
    assert api.query(token,'message',{'id':5})['items']==[]
    assert api.query(token,'history',{'query':'keyword-1'})['items'][0]['message_id']==1
    assert api.query(token,'attachments',{})['items'][0]['attachments'][0]['path']=='/tmp/example.txt'
    assert api.query(token,'summary',{})['message_count']==2
    records=jobs.db.execute('SELECT params,response FROM context_reads').fetchall()
    assert len(records)==8 and token not in str([tuple(r) for r in records])
    api.revoke(token)
    with pytest.raises(PermissionError):api.query(token,'summary',{})
    expired=api.grant(4,-1)
    with pytest.raises(PermissionError):api.query(expired,'summary',{})
    jobs.db.close()


async def test_context_http_uses_only_task_credential(state_env,gateway):
    d=await start_daemon(gateway,auto_enabled=True,owner_userid='owner',http_token='private-http-token')
    fill(d.automation.jobs);api=ContextAccess(d.automation.jobs);token=api.grant(4,30)
    client=TestClient(TestServer(d.build_app()));await client.start_server()
    try:
        assert (await client.get('/context')).status==401
        assert (await client.get('/context',headers={'X-Wecom-Token':'private-http-token'})).status==401
        response=await client.get('/context?action=history',headers={'X-Wecom-Context':token})
        assert response.status==200
        assert [x['message_id'] for x in (await response.json())['items']]==[4,1]
        assert (await client.post('/send',headers={'X-Wecom-Context':token},json={'text':'not authorized'})).status==403
        assert not gateway.sends
    finally:
        await client.close();await d.automation.close();await d.client.stop();d.store.close()
