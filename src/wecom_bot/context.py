"""任务级只读聊天上下文 API 与无凭据配置依赖的 CLI 客户端。"""
import argparse
import hashlib
import json
import os
import secrets
import time
import urllib.parse
import urllib.request


class ContextAccess:
    def __init__(self, jobs):
        self.jobs = jobs
        jobs.db.executescript('''
        CREATE TABLE IF NOT EXISTS context_grants(token_hash TEXT PRIMARY KEY,job_id INTEGER,expires_at REAL);
        CREATE TABLE IF NOT EXISTS context_reads(id INTEGER PRIMARY KEY,job_id INTEGER,read_at REAL,action TEXT,params TEXT,response TEXT);
        ''')

    def grant(self, job_id, seconds):
        token=secrets.token_urlsafe(32)
        with self.jobs.db:
            self.jobs.db.execute('DELETE FROM context_grants WHERE expires_at<?',(time.time(),))
            self.jobs.db.execute('INSERT INTO context_grants VALUES(?,?,?)',(self.digest(token),job_id,time.time()+seconds))
        return token

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def revoke(self, token):
        with self.jobs.db:
            self.jobs.db.execute('DELETE FROM context_grants WHERE token_hash=?',(self.digest(token),))

    def query(self, token, action, params):
        grant=self.jobs.db.execute('SELECT job_id FROM context_grants WHERE token_hash=? AND expires_at>?',(self.digest(token),time.time())).fetchone()
        if grant is None:raise PermissionError('任务查询凭证无效或已过期')
        job=self.jobs.db.execute('SELECT * FROM jobs WHERE id=?',(grant['job_id'],)).fetchone()
        if action not in ('summary','history','message','attachments'):raise ValueError('未知查询动作')
        limit=max(1,min(20,int(params.get('limit',10))))
        before=int(params.get('before',job['id']+1))
        query=str(params.get('query',''))[:200]
        sql='SELECT * FROM jobs WHERE chat_key=? AND owner=? AND id<=? AND id<? AND merged_into IS NULL'
        values=[job['chat_key'],job['owner'],job['id'],before]
        if action=='message':
            sql+=' AND id=?';values.append(int(params.get('id',0)))
        if action=='attachments':sql+=" AND (attachments!='[]' OR error_kind='attachment_download')"
        if query:
            sql+=' AND (instr(prompt,?)>0 OR instr(COALESCE(result,\'\'),?)>0 OR instr(attachments,?)>0)'
            values += [query,query,query]
        rows=self.jobs.db.execute(sql+' ORDER BY id DESC LIMIT ?',(*values,limit+1)).fetchall()
        more=len(rows)>limit;rows=rows[:limit]
        if action=='summary':
            counts=self.jobs.db.execute("SELECT COUNT(*),SUM(attachments!='[]') FROM jobs WHERE chat_key=? AND owner=? AND id<=? AND merged_into IS NULL",values[:3]).fetchone()
            result={'task_id':job['id'],'message_count':counts[0],'attachment_message_count':counts[1] or 0}
        else:
            items=[]
            for r in rows:
                item={'message_id':r['id'],'time':r['created_at'],'type':r['input_kind'],'state':r['state'],'outcome':r['outcome']}
                n=20000 if action=='message' else 1200
                if action!='attachments':
                    item.update(user=r['prompt'][:n],assistant=(r['result'] or '')[:n],quote=(r['quote_text'] or '')[:n],
                                truncated=any(len(r[k] or '')>n for k in ('prompt','result','quote_text')))
                item['attachments']=json.loads(r['attachments'])
                item['download_failed']=r['error_kind']=='attachment_download'
                items.append(item)
            result={'items':items,'next_before':rows[-1]['id'] if rows and more else None}
        with self.jobs.db:
            self.jobs.db.execute('INSERT INTO context_reads(job_id,read_at,action,params,response) VALUES(?,?,?,?,?)',
                (job['id'],time.time(),action,json.dumps(dict(params),ensure_ascii=False),json.dumps(result,ensure_ascii=False)))
        return result


def main(argv=None):
    p=argparse.ArgumentParser(description='只读查询当前 AI 任务的会话历史与附件')
    p.add_argument('action',choices=['summary','history','message','attachments'])
    p.add_argument('--query');p.add_argument('--id',type=int);p.add_argument('--before',type=int);p.add_argument('--limit',type=int,default=10)
    a=p.parse_args(argv)
    token=os.environ.get('WECOM_CONTEXT_TOKEN');url=os.environ.get('WECOM_CONTEXT_URL')
    if not token or not url:
        p.exit(2,'当前进程没有 AI 任务上下文；请由任务执行器调用。\n')
    params={k:v for k,v in vars(a).items() if v is not None}
    req=urllib.request.Request(url+'?'+urllib.parse.urlencode(params),headers={'X-Wecom-Context':token})
    try:
        with urllib.request.urlopen(req,timeout=10) as response:
            print(json.dumps(json.load(response),ensure_ascii=False,indent=2))
    except Exception as exc:
        p.exit(2,'上下文查询失败（'+type(exc).__name__+'）；请检查本机接口和任务凭证有效期。\n')
    return 0

if __name__=='__main__':main()
