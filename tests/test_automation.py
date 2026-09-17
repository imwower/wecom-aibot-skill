"""异步任务：身份门禁、真实子进程协议、超时释放、重启和消息路由。"""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from wecom_bot.automation import Jobs, accepted_prompt, run_ai
from wecom_bot.config import Config
from test_daemon import start_daemon


def body(text='@我的AI 检查', userid='owner', chatid='G1', kind='group'):
    return dict(aibotid='bot', msgid='m1', from_={'userid': userid},
                chatid=chatid, chattype=kind, msgtype='text', text={'content': text})


def message(**kw):
    b = body(**kw)
    b['from'] = b.pop('from_')
    return b


@pytest.mark.parametrize('change', [
    {'userid': 'other'}, {'text': '检查'}, {'text': '@我的AI'},
    {'chatid': None}, {'kind': 'unknown'},
])
def test_owner_gate_rejects(change):
    cfg = Config(auto_enabled=True, owner_userid='owner', bot_id='bot', private_requires_prefix=True)
    assert accepted_prompt(cfg, message(**change), 'message') is None


def test_owner_gate_private_and_bot():
    cfg = Config(auto_enabled=True, owner_userid='owner', bot_id='bot', private_requires_prefix=True)
    assert accepted_prompt(cfg, message(), 'message') == '检查'
    assert accepted_prompt(cfg, message(kind='single', text='检查'), 'message') is None
    cfg.private_requires_prefix = False
    assert accepted_prompt(cfg, message(kind='single', text='检查'), 'message') == '检查'
    assert accepted_prompt(cfg, message(), 'event') is None
    cfg.bot_id = 'otherbot'
    assert accepted_prompt(cfg, message(), 'message') is None
    cfg.owner_userid = ''
    assert accepted_prompt(cfg, message(), 'message') is None


@pytest.mark.parametrize('kind', ['group', 'single'])
@pytest.mark.parametrize('text,expected', [
    ('@我的AI 检查', '检查'),
    ('请 @我的AI 检查', '请  检查'),
    ('检查 @我的AI', '检查'),
    ('@我的AI @我的AI', None),
])
def test_mention_anywhere_preserves_owner_gate(kind, text, expected):
    cfg = Config(auto_enabled=True, owner_userid='owner', bot_id='bot', private_requires_prefix=True)
    assert accepted_prompt(cfg, message(kind=kind, text=text), 'message') == expected
    assert accepted_prompt(cfg, message(kind=kind, text=text, userid='other'), 'message') is None


def fake_cli(tmp_path):
    p = tmp_path / 'codex'
    p.write_text('#!' + sys.executable + '''
import sys,json,time,subprocess
from pathlib import Path
prompt=sys.stdin.read().split(chr(10)+'以下是同一会话',1)[0]
Path('args.json').write_text(json.dumps(sys.argv))
sid=sys.argv[sys.argv.index('resume')+1] if 'resume' in sys.argv else 'test-session'
print(json.dumps({'type':'thread.started','thread_id':sid}),flush=True)
if 'HANG' in prompt:
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
    Path('child.pid').write_text(str(child.pid))
    time.sleep(60)
if 'EOFHANG' in prompt:
    sys.stdout.close()
    time.sleep(60)
if 'FAIL' in prompt:
    print('password=topsecret Bearer abcdefghi',file=sys.stderr,flush=True)
    sys.exit(2)
if 'BUSY' in prompt:
    while True:
        print(json.dumps({'type':'turn.started'}),flush=True)
        time.sleep(.05)
outcome='blocked' if 'BLOCKED' in prompt else 'success'
result='bad json' if 'INVALID' in prompt else json.dumps({'outcome':outcome,'message':'已验证'})
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':result}}),flush=True)
print(json.dumps({'type':'turn.completed'}),flush=True)
''')
    p.chmod(0o755)
    return str(p)


def queued(jobs, ident=1, text='检查', group='G1'):
    b = message(chatid=group)
    b['msgid'] = f'm{ident}'
    jobs.enqueue(ident, b, text)
    jobs.update(ident, 'queued')
    return jobs.db.execute('SELECT * FROM jobs WHERE id=?', (ident,)).fetchone()


async def test_real_subprocess_resume_and_isolation(tmp_path):
    cfg = Config(ai_executable=fake_cli(tmp_path), ai_cwd=str(tmp_path), ai_full_access=True)
    jobs = Jobs(tmp_path / 'tasks.sqlite')
    job = queued(jobs)
    assert await run_ai(cfg, jobs, job) == '已验证'
    assert jobs.session(job['chat_key'])['session_id'] == 'test-session'
    assert await run_ai(cfg, jobs, job) == '已验证'
    args = json.loads((tmp_path / 'args.json').read_text())
    assert args[args.index('resume')+1] == 'test-session'
    assert '--dangerously-bypass-approvals-and-sandbox' in args
    assert '-s' not in args
    row=jobs.db.execute('SELECT * FROM jobs WHERE id=1').fetchone()
    assert row['outcome']=='success' and row['exit_code']==0
    assert row['finished_at'] >= row['started_at']
    other = queued(jobs, 2, group='G2')
    assert jobs.session(other['chat_key']) is None
    jobs.db.close()


async def test_total_deadline_even_with_continuous_output(tmp_path):
    cfg = Config(ai_executable=fake_cli(tmp_path), ai_cwd=str(tmp_path),
                 ai_timeout=1, ai_idle_timeout=5)
    jobs = Jobs(tmp_path / 'tasks.sqlite')
    with pytest.raises(RuntimeError, match='时限'):
        await run_ai(cfg, jobs, queued(jobs, text='BUSY'))
    jobs.db.close()


async def test_final_delivery_timeout_never_retries(state_env, gateway, monkeypatch):
    d = await start_daemon(gateway, auto_enabled=True, owner_userid='owner')
    queued(d.automation.jobs)
    d.automation.jobs.update(1, 'ready', '已完成')
    attempts = []
    async def unknown(*args, **kwargs):
        attempts.append(kwargs)
        raise TimeoutError()
    monkeypatch.setattr(d, 'do_send', unknown)
    d.automation.start()
    try:
        await asyncio.sleep(.6)
        assert len(attempts) == 1
        assert d.automation.jobs.status() == {'delivery_unknown': 1}
    finally:
        await d.automation.close()
        await d.client.stop()
        d.store.close()


async def test_hung_cli_and_child_killed_next_job_runs(tmp_path):
    cfg = Config(ai_executable=fake_cli(tmp_path), ai_cwd=str(tmp_path),
                 ai_timeout=8, ai_idle_timeout=1)
    jobs = Jobs(tmp_path / 'tasks.sqlite')
    with pytest.raises(RuntimeError, match='时限'):
        await run_ai(cfg, jobs, queued(jobs, text='HANG'))
    pid = int((tmp_path / 'child.pid').read_text())
    # macOS may briefly retain a zombie; neither running nor sleeping is allowed.
    import subprocess
    state = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True).stdout.strip()
    assert not state or state.startswith('Z')
    assert await run_ai(cfg, jobs, queued(jobs, 2)) == '已验证'
    jobs.db.close()


def test_recovery_never_reexecutes_running_or_resends_unknown(tmp_path):
    jobs = Jobs(tmp_path / 'tasks.sqlite')
    queued(jobs, 1)
    queued(jobs, 2)
    jobs.update(1, 'running')
    jobs.update(2, 'sending', 'done')
    jobs.recover()
    assert jobs.next()['state'] == 'ready'
    assert jobs.status()['delivery_unknown'] == 1
    assert '中断' in jobs.next()['result']
    jobs.db.close()


async def test_async_ack_owner_only_and_final_target(state_env, gateway, monkeypatch):
    async def fake_run(cfg, jobs, job):
        await asyncio.sleep(.2)
        return '任务完成'
    monkeypatch.setattr('wecom_bot.automation.run_ai', fake_run)
    d = await start_daemon(gateway, auto_enabled=True, owner_userid='owner')
    d.automation.start()
    try:
        await gateway.push_text('@我的AI 检查', userid='other', chatid='G2', chattype='group')
        await gateway.push_text('检查', userid='owner', chatid='G1', chattype='group')
        await asyncio.sleep(.15)
        assert not gateway.responds
        assert d.automation.jobs.status() == {}
        await gateway.push_text('@我的AI 检查', userid='owner', chatid='G1', chattype='group')
        for _ in range(100):
            if gateway.sends:
                break
            await asyncio.sleep(.03)
        assert gateway.responds[0]['body']['stream']['finish'] is True
        assert gateway.sends[0]['body']['chatid'] == 'G1'
        assert '任务完成' in gateway.sends[0]['body']['markdown']['content']
        assert d.automation.jobs.status() == {'sent': 1}
    finally:
        await d.automation.close()
        await d.client.stop()
        d.store.close()

async def test_blocked_and_invalid_results(tmp_path):
    cfg=Config(ai_executable=fake_cli(tmp_path),ai_cwd=str(tmp_path))
    jobs=Jobs(tmp_path/'tasks.sqlite')
    assert await run_ai(cfg,jobs,queued(jobs,text='BLOCKED')) == '已验证'
    assert jobs.outcomes()=={'blocked':1}
    with pytest.raises(RuntimeError,match='分类'):
        await run_ai(cfg,jobs,queued(jobs,2,text='INVALID'))
    assert jobs.outcomes()=={'blocked':1,'failed':1}
    jobs.db.close()

async def test_stderr_saved_redacted_with_exit_code(tmp_path):
    cfg=Config(ai_executable=fake_cli(tmp_path),ai_cwd=str(tmp_path))
    jobs=Jobs(tmp_path/'tasks.sqlite')
    with pytest.raises(RuntimeError):
        await run_ai(cfg,jobs,queued(jobs,text='FAIL'))
    row=jobs.db.execute('SELECT * FROM jobs WHERE id=1').fetchone()
    assert row['exit_code']==2
    assert row['stderr_tail'] and 'topsecret' not in row['stderr_tail'] and 'abcdefghi' not in row['stderr_tail']
    jobs.db.close()

def test_upgrade_old_database_preserves_results(tmp_path):
    import sqlite3
    p=tmp_path/'tasks.sqlite'
    db=sqlite3.connect(p)
    db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY,msgid TEXT,chat_key TEXT,chatid TEXT,chattype TEXT,owner TEXT,prompt TEXT,state TEXT,result TEXT,created_at REAL,updated_at REAL)')
    db.execute("INSERT INTO jobs(id,state,result) VALUES(1,'sent','旧结果')")
    db.commit();db.close()
    jobs=Jobs(p)
    r=jobs.db.execute('SELECT * FROM jobs').fetchone()
    assert r['state']=='sent' and r['result']=='旧结果' and r['outcome']=='unknown'
    jobs.db.close()
    jobs=Jobs(p)
    assert jobs.outcomes()=={'unknown':1}
    jobs.db.close()

async def test_claude_resume_and_codex_sessions_are_separate(tmp_path):
    from wecom_bot.runners import session_key
    p=tmp_path/'claude'
    p.write_text('#!'+sys.executable+'''\nimport sys,json\nfrom pathlib import Path\nprompt=sys.stdin.read().split(chr(10)+'以下是同一会话',1)[0]\nPath('claude-args.json').write_text(json.dumps(sys.argv))\nsid=sys.argv[sys.argv.index('--resume')+1] if '--resume' in sys.argv else 'claude-session'\nprint(json.dumps({'type':'system','subtype':'init','session_id':sid}),flush=True)\nprint(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':sid,'structured_output':{'outcome':'blocked' if 'BLOCKED' in prompt else 'success','message':'Claude 完成'}}),flush=True)\n''')
    p.chmod(0o755)
    cfg=Config(ai_provider='claude',ai_executable=str(p),ai_cwd=str(tmp_path))
    jobs=Jobs(tmp_path/'tasks.sqlite');job=queued(jobs)
    jobs.remember(job['chat_key'],'existing-codex-session',str(tmp_path))
    assert await run_ai(cfg,jobs,job)=='Claude 完成'
    assert jobs.session(job['chat_key'])['session_id']=='existing-codex-session'
    assert jobs.session(session_key('claude',job['chat_key']))['session_id']=='claude-session'
    assert await run_ai(cfg,jobs,job)=='Claude 完成'
    args=json.loads((tmp_path/'claude-args.json').read_text())
    assert args[args.index('--resume')+1]=='claude-session'
    assert '--permission-mode' in args and '--dangerously-skip-permissions' not in args
    jobs.db.close()


def test_runner_permissions_and_bad_events():
    from wecom_bot.runners import command,events
    for provider,flag in [('codex','--dangerously-bypass-approvals-and-sandbox'),('claude','--dangerously-skip-permissions')]:
        cfg=Config(ai_provider=provider,ai_model='example-model')
        assert flag not in command(cfg)
        cfg.ai_full_access=True
        args=command(cfg,'sid');assert flag in args and 'example-model' in args
    assert events('claude',{'type':'result','subtype':'error_during_execution','is_error':True})==[('failed',None)]
    assert events('claude',None)==[]


def test_assistant_prompt_can_be_customized_and_reloaded(tmp_path):
    from wecom_bot.automation import assistant_instructions
    cfg=Config()
    assert '聊天历史与附件' in assistant_instructions(cfg)
    p=tmp_path/'assistant.md';p.write_text('回复风格：简短中文')
    cfg.ai_prompt_file=str(p)
    assert assistant_instructions(cfg)=='回复风格：简短中文'
    p.write_text('回复风格：详细说明')
    assert assistant_instructions(cfg)=='回复风格：详细说明'
    p.unlink()
    with pytest.raises(RuntimeError,match='提示词文件'):
        assistant_instructions(cfg)
