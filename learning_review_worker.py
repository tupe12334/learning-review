#!/usr/bin/env python3
"""Background analyzer/applier for the learning-review plugin."""
from __future__ import annotations
import argparse, json, os, re, subprocess, sys, time
from pathlib import Path

HOME=Path(os.environ.get('HERMES_HOME',Path.home()/'.hermes'))
PLUGIN_DIR=Path(__file__).resolve().parent
REPO=HOME

def read(p, default):
 try:return json.loads(p.read_text(encoding='utf-8'))
 except (OSError,json.JSONDecodeError):return default
def write(job,name,value):
 p=job/name; tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(tmp,p)
def status(job,state,message,**extra): write(job,'status.json',{'state':state,'message':message,**extra})
def run(cmd,*,cwd=None,timeout=600):
 return subprocess.run(cmd,cwd=cwd,text=True,capture_output=True,timeout=timeout)
def extract_json(text):
 match=re.search(r'```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```',text,re.S)
 raw=match.group(1) if match else text.strip()
 start=min([x for x in (raw.find('{'),raw.find('[')) if x>=0],default=-1)
 if start<0: raise ValueError('No JSON object in agent response')
 decoder=json.JSONDecoder(); return decoder.raw_decode(raw[start:])[0]
def agent(prompt,cwd):
 result=run(['hermes','-z',prompt,'--cli','--safe-mode'],cwd=cwd,timeout=600)
 combined='\n'.join(part.strip() for part in (result.stderr,result.stdout) if part.strip())
 if result.returncode: raise RuntimeError(combined[-3000:])
 output=result.stdout.strip()
 if 'Failed to parse ' in combined or re.search(r'HTTP \d{3}:',combined):
  raise RuntimeError(combined[-3000:])
 return output
def run_agent(job,prompt,cwd):
 try:
  output=agent(prompt,cwd)
 except Exception as exc:
  output=f'{type(exc).__name__}: {exc}'
  (job/'agent-output.txt').write_text(output,encoding='utf-8')
  raise
 (job/'agent-output.txt').write_text(output,encoding='utf-8')
 return output
def cleanup_worktree(worktree,*,succeeded):
 if succeeded:
  run(['git','worktree','remove','--force',str(worktree)],cwd=REPO,timeout=120)
def pr_state(job,worktree,branch):
 result=run(['gh','pr','list','--head',branch,'--base','main','--state','all','--json','number,url,state,mergeCommit,mergeable,mergeStateStatus,statusCheckRollup'],cwd=worktree,timeout=60)
 if result.returncode: raise RuntimeError(result.stderr[-1500:])
 items=json.loads(result.stdout or '[]')
 if len(items)!=1: raise RuntimeError(missing_pr_error(job,branch,len(items)))
 return items[0]
def reconcile_and_merge(job,worktree,branch):
 """Make progress through ordinary conflicts/check failures without user intervention."""
 for attempt in range(1,4):
  item=pr_state(job,worktree,branch)
  if item.get('state')=='MERGED': return item
  status(job,'applying',f'Reconciling learning PR #{item.get("number")} (attempt {attempt}/3): resolving conflicts and waiting for checks.')
  repair=f'''Continue delivering the existing learning-review PR {item.get("url")} from worktree {worktree}. You must merge it without asking the user. Fetch origin/main. If the PR is conflicted or main advanced, integrate origin/main into this branch and resolve conflicts while preserving both the current main behavior and the approved skills-only changes. Do not force-push and do not modify files outside skills/ and .changeset/. Run git diff --check and relevant validation, commit any resolution, and push normally. Inspect GitHub check failures; add a focused .changeset/*.md when required, and fix any in-scope failures. Wait for checks to complete. When the PR is CLEAN/MERGEABLE with successful required checks, merge it using gh pr merge --squash --delete-branch. Return factual status only. Do not stop merely because a conflict or ordinary check failure occurred.'''
  run_agent(job,repair,worktree)
  item=pr_state(job,worktree,branch)
  if item.get('state')=='MERGED': return item
  checks=run(['gh','pr','checks',str(item['number']),'--watch','--interval','10'],cwd=worktree,timeout=600)
  item=pr_state(job,worktree,branch)
  if item.get('state')=='MERGED': return item
  if checks.returncode and attempt==3: raise RuntimeError(f'PR {item.get("url")} remains unmerged after conflict/CI recovery: {checks.stderr[-1000:] or checks.stdout[-1000:]}')
 raise RuntimeError(f'PR recovery exhausted without merge: {pr_state(job,worktree,branch).get("url")}')
def analyze(job):
 request=read(job/'request.json',{})
 transcript=request.get('transcript',[])
 if not transcript: raise RuntimeError('The source transcript was unavailable.')
 status(job,'analyzing','An isolated learning agent is identifying reusable lessons from this session.')
 evidence='\n\n'.join(f"[{m.get('role','unknown')}] {str(m.get('content',''))[:3000]}" for m in transcript[-80:])
 prompt=f'''You are an isolated learning-review agent. Analyze ONLY the transcript below. Identify reusable, durable Hermes skills/workflows—not temporary task progress, secrets, raw logs, or user-specific facts. Return ONLY valid JSON: {{"candidates":[{{"id":"short-kebab-id","title":"short title","tldr_he":"תקציר קצר וברור בעברית (עד 18 מילים)","lesson":"what reusable procedure or rule should be retained","evidence":"brief quoted/paraphrased support from this transcript","target_hint":"existing skill name to update, or new skill name"}}]}}. Every candidate MUST include tldr_he written in Hebrew, no more than 18 words. Return at most 8 candidates. Do not make any file changes.\n\nTRANSCRIPT:\n{evidence}'''
 data=extract_json(run_agent(job,prompt,REPO))
 items=[]
 for index,c in enumerate(data.get('candidates',[]) if isinstance(data,dict) else []):
  if not isinstance(c,dict):continue
  title=str(c.get('title','')).strip(); lesson=str(c.get('lesson','')).strip()
  if not title or not lesson:continue
  cid=re.sub(r'[^a-z0-9-]+','-',str(c.get('id') or f'lesson-{index+1}').lower()).strip('-') or f'lesson-{index+1}'
  tldr_he=str(c.get('tldr_he','')).strip()
  items.append({'id':cid,'title':title[:160],'tldr_he':tldr_he[:280],'lesson':lesson[:1800],'evidence':str(c.get('evidence',''))[:1800],'target_hint':str(c.get('target_hint',''))[:100]})
 write(job,'candidates.json',{'candidates':items})
 status(job,'ready',f'{len(items)} candidate lesson(s) are ready for your review.')
def missing_pr_error(job, branch, count):
 detail=(job/'agent-output.txt').read_text(encoding='utf-8').strip() if (job/'agent-output.txt').exists() else ''
 message=f'Expected exactly one PR for {branch}, found {count}.'
 return f'{message} Delivery output: {detail[-1200:]}' if detail else message

def prepare_worktree(job, branch):
 worktrees=HOME/'runtime'/'learning-review-worktrees';worktrees.mkdir(parents=True,exist_ok=True);worktree=worktrees/job.name
 fetched=run(['git','fetch','origin','main','--prune'],cwd=REPO,timeout=120)
 if fetched.returncode: raise RuntimeError(fetched.stderr[-1500:])
 if worktree.exists():
  current=run(['git','branch','--show-current'],cwd=worktree,timeout=30)
  if current.returncode or current.stdout.strip()!=branch:
   raise RuntimeError(f'Recovery worktree {worktree} is not on {branch}.')
  dirty=run(['git','status','--short'],cwd=worktree,timeout=30)
  if dirty.returncode: raise RuntimeError(dirty.stderr[-1500:])
  if dirty.stdout.strip(): raise RuntimeError(f'Recovery worktree has uncommitted changes:\n{dirty.stdout.strip()}')
  return worktree, True
 existing=run(['git','show-ref','--verify','--quiet',f'refs/heads/{branch}'],cwd=REPO,timeout=30)
 if existing.returncode==0:
  add=run(['git','worktree','add',str(worktree),branch],cwd=REPO,timeout=120)
 else:
  add=run(['git','worktree','add','-b',branch,str(worktree),'origin/main'],cwd=REPO,timeout=120)
 if add.returncode: raise RuntimeError(add.stderr[-1500:])
 return worktree, False

def delivery_prompt(worktree, branch):
 return f'''You are recovering delivery of the already committed learning-review branch {branch} from worktree {worktree}. Do not edit skills or other files. Inspect git status and the committed diff. Push only branch {branch} to origin; never push directly to main and never force push. Create a pull request from {branch} into main if none exists. Check mergeability and required checks. If an in-scope delivery check fails, diagnose it and make only the minimum required delivery metadata change, then commit and push normally. Once the PR is CLEAN/MERGEABLE with successful required checks, merge it using the repository's normal merge method. Reply with only factual delivery details: PR URL, PR number, branch commit SHA, merge commit SHA, check results, and whether the remote branch was deleted.'''

def apply(job):
 selection=read(job/'selection.json',{}).get('selected',[]); candidates=read(job/'candidates.json',{}).get('candidates',[]); chosen=[x for x in candidates if x.get('id') in selection]
 if not chosen: raise RuntimeError('No approved lessons were found.')
 status(job,'applying','Creating an isolated branch from origin/main for a reviewed pull request.')
 branch=f'learning-review/{job.name}'
 worktree,recovered=prepare_worktree(job,branch)
 succeeded=False
 try:
  status(job,'applying','The isolated agent is updating approved skills and opening a pull request.')
  approved=json.dumps(chosen,ensure_ascii=False,indent=2)
  prompt=delivery_prompt(worktree,branch) if recovered else f'''You are applying explicitly approved Hermes learning candidates in the isolated git worktree at {worktree}, on branch {branch}. Update only skill files under {worktree}/skills, using the repository's existing skill conventions. Do not touch config, plugins, session data, memories, or unrelated files. Implement precisely these approved candidates:\n{approved}\n\nRequired delivery workflow:\n1. Inspect git diff and stop if it includes anything outside skills/.\n2. Run relevant validation and git diff --check.\n3. Commit the focused skill changes with a clear message.\n4. Push only branch {branch} to origin; NEVER push directly to main and never force push.\n5. Create a pull request from {branch} into main with a concise summary and validation evidence.\n6. Check the PR's current mergeability and all required checks. If checks are pending, wait for them. If any required check fails, investigate and fix only an in-scope failure, push the fix, and recheck. Do not merge failed or missing-required-check PRs.\n7. Once all required checks pass and the PR is mergeable, merge it using the repository's normal merge method, then verify the PR is MERGED and origin/main contains the resulting merge commit.\n\nReply with only factual delivery details: PR URL, PR number, branch commit SHA, merge commit SHA, check results, and whether the remote branch was deleted.'''
  output=run_agent(job,prompt,worktree)
  changes=run(['git','status','--short'],cwd=worktree).stdout.strip()
  if changes: raise RuntimeError(f'Agent left uncommitted changes:\n{changes}')
  item=reconcile_and_merge(job,worktree,branch)
  if item.get('state')!='MERGED': raise RuntimeError(f"PR {item.get('url')} was not merged; state is {item.get('state')}.")
  merge=item.get('mergeCommit') or {}; merge_sha=str(merge.get('oid') or '')
  if not merge_sha: raise RuntimeError('Merged PR has no merge commit SHA.')
  verify=run(['git','fetch','origin','main','--prune'],cwd=worktree,timeout=120)
  if verify.returncode: raise RuntimeError(verify.stderr[-1500:])
  contained=run(['git','merge-base','--is-ancestor',merge_sha,'origin/main'],cwd=worktree,timeout=30)
  if contained.returncode: raise RuntimeError('Merged PR commit is not contained in origin/main.')
  write(job,'result.json',{'pr_url':item.get('url'),'pr_number':item.get('number'),'commit':merge_sha,'agent_output':output[-4000:]})
  status(job,'complete',f"Applied {len(chosen)} lesson(s) through merged PR #{item.get('number')}; verified origin/main contains {merge_sha[:12]}.",commit=merge_sha,pr_url=item.get('url'))
  succeeded=True
 finally:
  cleanup_worktree(worktree,succeeded=succeeded)
def main():
 p=argparse.ArgumentParser();p.add_argument('action',choices=['analyze','apply']);p.add_argument('--job',required=True);a=p.parse_args();job=Path(a.job)
 try: analyze(job) if a.action=='analyze' else apply(job)
 except Exception as e: status(job,'failed',f'{a.action.capitalize()} failed: {e}')
if __name__=='__main__':main()
