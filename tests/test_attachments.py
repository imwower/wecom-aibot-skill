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
        await until(lambda:len(gateway.sends)==2)
        rows=list(d.automation.jobs.db.execute('SELECT * FROM jobs ORDER BY id'))
        if fail:
            assert rows[0]['outcome']=='blocked'
            assert len(calls)==1 and calls[0][1][-1]['download_failed']
            assert '下载未完成' in gateway.sends[0]['body']['markdown']['content']
        else:
            assert len(calls)==2
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
        assert rows[0]['state']=='sent' and rows[1]['state']=='merged'
        assert rows[1]['merged_into']==rows[0]['id']
        assert rows[0]['outcome']==('blocked' if fail else 'success')
    finally:
        await asyncio.gather(*list(d._attachment_tasks),return_exceptions=True)
        await d.automation.close();await d.client.stop();d.store.close()
