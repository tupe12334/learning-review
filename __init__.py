"""Hermes plugin: session-scoped review before a skill-learning push."""
from __future__ import annotations
import contextvars, json, os, re, secrets, subprocess, sys, time
from pathlib import Path

HOME=Path(os.environ.get('HERMES_HOME',Path.home()/'.hermes'))
PLUGIN_DIR=Path(__file__).resolve().parent
RUNTIME=HOME/'runtime'/'learning-review-jobs'
UI=PLUGIN_DIR/'learning_review_ui.py'
WORKER=PLUGIN_DIR/'learning_review_worker.py'
CURRENT=contextvars.ContextVar('learning_review_context',default=None)


def _extract_content(message):
 content=message.get('content','') if isinstance(message,dict) else ''
 if isinstance(content,list): return '\n'.join(str(x.get('text','')) for x in content if isinstance(x,dict))
 return str(content)

def capture(event, gateway=None, session_store=None, **_kwargs):
 if (getattr(event,'get_command',lambda:None)() or '').replace('_','-')!='learn-review': return None
 try:
  source=event.source; key=gateway._session_key_for_source(source)
  entry=getattr(session_store,'_entries',{}).get(key)
  transcript=session_store.load_transcript(entry.session_id) if entry and entry.session_id else []
  CURRENT.set({'session_key':key,'session_id':entry.session_id if entry else '', 'source':source.to_dict() if source else {},'transcript':[{'role':m.get('role','unknown'),'content':_extract_content(m)} for m in transcript]})
 except Exception as exc: CURRENT.set({'error':str(exc)})
 return None

def _running_link():
 recovered=_recover_running_link()
 if recovered:
  return recovered
 for job in sorted(RUNTIME.glob('*'),key=lambda x:x.stat().st_mtime,reverse=True) if RUNTIME.exists() else []:
  info=_read(job/'link.json',{})
  if info.get('url') and _read(job/'status.json',{}).get('state') not in {'failed','complete'}: return info
 return None

def _read(path,default):
 try:return json.loads(path.read_text(encoding='utf-8'))
 except (OSError,json.JSONDecodeError):return default

_LINK_PATTERN=re.compile(r'https://[^\s|]+\.(?:loca\.lt|trycloudflare\.com)')


def _write_link(job, token):
 """Persist a tunnel link already emitted by the UI process, if present."""
 try:
  match=_LINK_PATTERN.search((job/'ui.log').read_text(encoding='utf-8'))
 except OSError:
  match=None
 if not match:
  return None
 url=f'{match.group(0).rstrip("/")}/?token={token}'
 (job/'link.json').write_text(json.dumps({'url':url}),encoding='utf-8')
 return url


def _job_token(job):
 """Read the locally protected token retained for an in-flight review job."""
 try:
  return (job/'token').read_text(encoding='utf-8').strip() or None
 except OSError:
  return None


def _recover_running_link():
 """Make a previously-started review reachable after an early tunnel race."""
 for job in sorted(RUNTIME.glob('*'),key=lambda x:x.stat().st_mtime,reverse=True) if RUNTIME.exists() else []:
  state=_read(job/'status.json',{}).get('state')
  if state in {'failed','complete'}:
   continue
  token=_job_token(job)
  if token:
   url=_write_link(job,token)
   if url:
    return {'url':url}
 return None


def _is_current_session_job(job, session_id):
 return bool(session_id) and _read(job/'request.json',{}).get('session_id') == session_id


def _recover_current_session_link(session_id):
 for job in sorted(RUNTIME.glob('*'),key=lambda x:x.stat().st_mtime,reverse=True) if RUNTIME.exists() else []:
  if not _is_current_session_job(job,session_id):
   continue
  token=_job_token(job)
  if token:
   url=_write_link(job,token)
   if url:
    return url
 return None

def wait_for_link(job,token,timeout_seconds=35):
 deadline=time.monotonic()+timeout_seconds
 while time.monotonic()<=deadline:
  try:match=_LINK_PATTERN.search((job/'ui.log').read_text(encoding='utf-8'))
  except OSError:match=None
  if match:
   url=f'{match.group(0)}/?token={token}'
   (job/'link.json').write_text(json.dumps({'url':url}),encoding='utf-8')
   return url
  time.sleep(.5)
 (job/'status.json').write_text(json.dumps({'state':'failed','message':'Could not create the public review tunnel. See ui.log for details.'}),encoding='utf-8')
 return None

def _handle(_raw_args):
 context=CURRENT.get()
 if not context: return 'Could not resolve this conversation. Please send /learn-review again.'
 if context.get('error'): return f"Could not load this session transcript: {context['error']}"
 if not context.get('transcript'): return 'This session has no saved transcript yet. Send /learn-review after at least one completed turn.'
 existing_url=_recover_current_session_link(context.get('session_id',''))
 if existing_url:return f"Learning review is already running:\n{existing_url}"
 RUNTIME.mkdir(parents=True,exist_ok=True); job=RUNTIME/f"{int(time.time())}-{secrets.token_hex(4)}";job.mkdir();token=secrets.token_urlsafe(24)
 (job/'token').write_text(token,encoding='utf-8');os.chmod(job/'token',0o600)
 (job/'request.json').write_text(json.dumps(context,ensure_ascii=False),encoding='utf-8')
 (job/'status.json').write_text(json.dumps({'state':'starting','message':'Starting isolated session-learning analysis…'}),encoding='utf-8')
 log=(job/'ui.log').open('w',encoding='utf-8')
 env={**os.environ,'LEARNING_REVIEW_JOB':str(job),'LEARNING_REVIEW_TOKEN':token}
 subprocess.Popen([sys.executable,str(UI)],stdout=log,stderr=subprocess.STDOUT,env=env,start_new_session=True)
 worker_log=(job/'analyze.log').open('w',encoding='utf-8')
 subprocess.Popen([sys.executable,str(WORKER),'analyze','--job',str(job)],stdout=worker_log,stderr=subprocess.STDOUT,start_new_session=True)
 url=wait_for_link(job,token)
 if url:return f'Learning analysis started in an isolated background session. Review it here:\n{url}'
 return 'Could not create the public review link. The job has been marked failed; please check its tunnel logs.'

def register(ctx):
 ctx.register_hook('pre_gateway_dispatch',capture)
 ctx.register_command('learn-review',_handle,'Analyze this session in the background and review proposed skill learnings.')
