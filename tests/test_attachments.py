import asyncio
import base64
import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wecom_bot import protocol as P
from wecom_bot.automation import Jobs, accepted_prompt
from wecom_bot.config import Config
from test_daemon import start_daemon


async def until(check):
    for _ in range(150):
        if check(): return
        await asyncio.sleep(.02)
    assert check()


@pytest.mark.parametrize('fail', [False, True])
async def test_encrypted_file_then_instruction(state_env, gateway, monkeypatch, fail):
    key=b'01234567890123456789012345678901'
    data=b'attachment-content-not-command'
    padding=16-len(data)%16
    enc=Cipher(algorithms.AES(key),modes.CBC(key[:16])).encryptor()
    encrypted=enc.update(data+bytes([padding])*padding)+enc.finalize()
    release=asyncio.Event()
    async def handle(request):
        await release.wait()
        return web.Response(status=410 if fail else 200,body=encrypted,
            headers={'Content-Disposition':'attachment; filename="example.txt"'})
    app=web.Application();app.router.add_get('/file',handle)
    server=TestServer(app);await server.start_server()
    d=await start_daemon(gateway,auto_enabled=True,owner_userid='owner')
    calls=[]
    async def run(cfg,jobs,job):
        context=jobs.attachment_context(job);calls.append((job['id'],context))
        if not fail:
            assert Path(context[-1]['files'][0]['path']).read_bytes()==data
        jobs.execution(job['id'],outcome='success')
        return '附件已收到' if len(calls)==1 else '已处理'
    monkeypatch.setattr('wecom_bot.automation.run_ai',run)
    d.automation.start()
    try:
        b={'msgid':'file-1','aibotid':gateway.bot_id,'chattype':'single',
           'from':{'userid':'owner'},'msgtype':'file',
           'file':{'url':str(server.make_url('/file')),'aeskey':base64.b64encode(key).decode()}}
        await gateway.push_callback(b)
        await until(lambda: bool(gateway.responds))
        await gateway.push_text('请读取刚才的文件',userid='owner')
        await asyncio.sleep(.2)
        assert not calls
        assert d.automation.jobs.next()['state']=='downloading'
        release.set()
        await until(lambda:len(gateway.sends)==(2 if fail else 1))
        rows=list(d.automation.jobs.db.execute('SELECT * FROM jobs ORDER BY id'))
        if fail:
            assert rows[0]['outcome']=='blocked'
            assert len(calls)==1 and calls[0][1][-1]['download_failed']
            assert '下载未完成' in gateway.sends[0]['body']['markdown']['content']
        else:
            assert len(calls)==1
            assert rows[0]["state"]=="stored"
            assert d.store.get('1')['media_path']
            assert json.loads(rows[0]['attachments'])
        assert all(x['body']['chatid']=='owner' for x in gateway.sends)
    finally:
        release.set()
        await asyncio.gather(*list(d._attachment_tasks),return_exceptions=True)
        await d.automation.close();await d.client.stop();d.store.close();await server.close()


def test_attachment_context_isolated_and_no_future_files(tmp_path):
    j=Jobs(tmp_path/'tasks.sqlite')
    for ident,chat in [(1,'A'),(2,'B'),(3,'A')]:
        b={'msgid':str(ident),'aibotid':'bot','chattype':'group','chatid':chat,'from':{'userid':'owner'}}
        j.enqueue(ident,b,'task');j.attach(ident,[{'path':f'/tmp/{ident}','kind':'file'}])
    a=j.db.execute('SELECT * FROM jobs WHERE id=1').fetchone()
    assert [x['message_id'] for x in j.attachment_context(a)]==[1]
    b=j.db.execute('SELECT * FROM jobs WHERE id=2').fetchone()
    assert [x['message_id'] for x in j.attachment_context(b)]==[2]
    j.update(3,'downloading');j.recover()
    assert j.db.execute('SELECT outcome FROM jobs WHERE id=3').fetchone()[0]=='blocked'
    j.db.close()


def test_mixed_and_quoted_media_keep_identity_gate():
    b={'aibotid':'bot','msgid':'1','chattype':'single','from':{'userid':'owner'},
       'msgtype':'mixed','mixed':{'msg_item':[{'msgtype':'text','text':{'content':'看图片'}},
       {'msgtype':'image','image':{'url':'https://example.test/image','aeskey':'fake'}}]},
       'quote':{'msgtype':'file','file':{'url':'https://example.test/file','aeskey':'fake'}}}
    cfg=Config(auto_enabled=True,owner_userid='owner',bot_id='bot')
    assert len(P.media_refs(b))==2
    assert accepted_prompt(cfg,b,'message')
    b['from']['userid']='someone-else'
    assert accepted_prompt(cfg,b,'message') is None


def test_corrupted_ciphertext_rejected():
    from wecom_bot.media import decrypt
    with pytest.raises(ValueError,match='长度'):
        decrypt(b'truncated',base64.b64encode(b'1'*32).decode())


@pytest.mark.parametrize('text_first', [True, False])
@pytest.mark.parametrize('fail', [True, False])
async def test_adjacent_callbacks_merge_once_and_wait_for_download(state_env,gateway,monkeypatch,text_first,fail):
    d=await start_daemon(gateway,auto_enabled=True,owner_userid='owner',ai_message_window=.15)
    calls=[]
    async def download(*args,**kwargs):
        await asyncio.sleep(.4)
        if fail:raise ValueError('download failed')
        p=state_env['root']/'sample.txt';p.write_text('fixture-content')
        return p
    async def run(cfg,jobs,job):
        ctx=jobs.attachment_context(job)
        assert job['prompt']=='概括附件内容'
        assert Path(ctx[0]['files'][0]['path']).read_text()=='fixture-content'
        calls.append(job['id']);jobs.execution(job['id'],outcome='success')
        return '已概括附件'
    monkeypatch.setattr('wecom_bot.daemon.download_and_decrypt',download)
    monkeypatch.setattr('wecom_bot.automation.run_ai',run)
    d.automation.start()
    b={'aibotid':gateway.bot_id,'msgid':'attachment-reorder','chattype':'single','from':{'userid':'owner'},
       'msgtype':'file','file':{'url':'https://example.test/attachment','aeskey':'fake'}}
    try:
        if text_first:await gateway.push_text('概括附件内容',userid='owner')
        await gateway.push_callback(b)
        if not text_first:await gateway.push_text('概括附件内容',userid='owner')
        await until(lambda:len(gateway.sends)==1)
        await asyncio.sleep(.2)
        assert len(gateway.sends)==1 and len(calls)==(0 if fail else 1)
        rows=list(d.automation.jobs.db.execute('SELECT * FROM jobs ORDER BY id'))
        if fail or text_first:
            assert rows[0]['state']=='sent' and rows[1]['state']=='merged'
            assert rows[1]['merged_into']==rows[0]['id']
            assert rows[0]['outcome']==('blocked' if fail else 'success')
        else:
            assert rows[0]['state']=='stored' and rows[1]['state']=='sent'
            assert rows[1]['outcome']=='success'
    finally:
        await asyncio.gather(*list(d._attachment_tasks),return_exceptions=True)
        await d.automation.close();await d.client.stop();d.store.close()


def test_context_snapshot_contains_history_and_quote_without_cross_chat(tmp_path):
    j=Jobs(tmp_path/'tasks.sqlite')
    for ident,chat,owner,prompt in [(1,'A','owner','记住项目代号青山'),(2,'B','owner','别的群秘密'),
        (3,'A','other','别人的秘密'),(4,'A','owner','项目代号是什么'),(5,'A','owner','未来任务')]:
        b={'aibotid':'bot','msgid':str(ident),'chattype':'group','chatid':chat,'from':{'userid':owner},'msgtype':'text'}
        if ident==4:b['quote']={'msgtype':'text','text':{'content':'请参考上次回复'}}
        j.enqueue(ident,b,prompt)
    j.update(1,'sent','已记住青山')
    job=j.db.execute('SELECT * FROM jobs WHERE id=4').fetchone()
    ctx=j.context(job)
    assert len(ctx['recent_messages'])==1
    assert ctx['recent_messages'][0]['assistant']=='已记住青山'
    assert ctx['current_quote']=='请参考上次回复'
    assert j.db.execute('SELECT context_snapshot FROM jobs WHERE id=4').fetchone()[0]
    j.db.close()


async def test_file_alone_stored_without_cli_then_later_instruction(state_env,gateway,monkeypatch):
    d=await start_daemon(gateway,auto_enabled=True,owner_userid='owner',ai_message_window=.1)
    calls=[]
    async def download(*args,**kwargs):
        p=state_env['root']/'later.txt';p.write_text('delayed-attachment');return p
    async def run(cfg,jobs,job):
        context=jobs.context(job)
        assert context['recent_messages'][0]['outcome']=='attachment_ready'
        assert Path(context['attachments'][0]['files'][0]['path']).read_text()=='delayed-attachment'
        calls.append(job['id']);return '已读附件'
    monkeypatch.setattr('wecom_bot.daemon.download_and_decrypt',download)
    monkeypatch.setattr('wecom_bot.automation.run_ai',run)
    d.automation.start()
    try:
        await gateway.push_callback({'aibotid':gateway.bot_id,'msgid':'late-file','chattype':'single',
            'from':{'userid':'owner'},'msgtype':'file','file':{'url':'https://example.test/file','aeskey':'fake'}})
        await until(lambda:d.automation.jobs.status().get('stored')==1)
        await asyncio.sleep(.3)
        assert not calls and not gateway.sends
        assert '附件已接收' in gateway.responds[0]['body']['stream']['content']
        await gateway.push_text('读取刚才的附件',userid='owner')
        await until(lambda:len(gateway.sends)==1)
        assert calls==[2]
    finally:
        await asyncio.gather(*list(d._attachment_tasks),return_exceptions=True)
        await d.automation.close();await d.client.stop();d.store.close()
