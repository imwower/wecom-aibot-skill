"""CLI 协议适配；只构造参数和归一化事件，不经 shell 执行用户文本。"""
import json
from pathlib import Path

SCHEMA = Path(__file__).with_name('task_result.schema.json')


def session_key(provider, chat_key):
    # 保留已有 Codex 会话键；Claude 使用独立命名空间。
    return chat_key if provider == 'codex' else json.dumps([provider, chat_key])


def command(cfg, session_id=None):
    executable = cfg.ai_executable or cfg.ai_provider
    if cfg.ai_provider == 'codex':
        args = [executable, 'exec', '--json', '--skip-git-repo-check',
                '--output-schema', str(SCHEMA)]
        args += (['--dangerously-bypass-approvals-and-sandbox'] if cfg.ai_full_access
                 else ['-s', 'workspace-write', '-c', 'approval_policy="never"'])
        if cfg.ai_model:
            args += ['--model', cfg.ai_model]
        if session_id:
            args += ['resume', session_id]
        return args + ['-']
    if cfg.ai_provider == 'claude':
        args = [executable, '-p', '--verbose', '--output-format', 'stream-json',
                '--json-schema', SCHEMA.read_text()]
        args += (['--dangerously-skip-permissions'] if cfg.ai_full_access
                 else ['--permission-mode', 'dontAsk'])
        if cfg.ai_model:
            args += ['--model', cfg.ai_model]
        if session_id:
            args += ['--resume', session_id]
        return args
    raise ValueError('不支持的 AI CLI')


def events(provider, event):
    """返回统一事件列表：(session/result/completed/failed, 值)。"""
    if not isinstance(event, dict):
        return []
    kind = event.get('type')
    if provider == 'codex':
        if kind == 'thread.started':
            return [('session', event.get('thread_id'))]
        if kind == 'item.completed':
            item = event.get('item') or {}
            if item.get('type') == 'agent_message':
                return [('result', item.get('text'))]
        if kind == 'turn.completed':
            return [('completed', None)]
        if kind == 'turn.failed':
            return [('failed', None)]
    elif provider == 'claude':
        if kind == 'system' and event.get('subtype') == 'init':
            return [('session', event.get('session_id'))]
        if kind == 'result':
            if event.get('is_error') or event.get('subtype') != 'success':
                return [('failed', None)]
            payload = event.get('structured_output')
            result = json.dumps(payload, ensure_ascii=False) if payload is not None else event.get('result')
            return [('session', event.get('session_id')), ('result', result), ('completed', None)]
    return []
