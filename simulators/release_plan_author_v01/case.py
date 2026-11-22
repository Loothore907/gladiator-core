"""Offline schedule artifact evaluator. No deployment or contestant code execution."""
import hashlib
from pathlib import Path
from copy import deepcopy
import json_contract
from json_contract import need,exact,strict,digest,same
ROOT=Path(__file__).resolve().parent
VERSION='release-plan-author-session-0.1.0'

def public_input():
 raw=(ROOT/'PUBLIC_CASE.json').read_bytes()
 freeze=strict((ROOT/'INPUT_FREEZE.json').read_text(encoding='utf-8'))
 need(hashlib.sha256(raw).hexdigest()==freeze['PUBLIC_CASE.json'],'input_freeze')
 return strict(raw.decode('utf-8'))

def manifest():
 need(Path(json_contract.__file__).resolve().parent==ROOT,'dependency_origin')
 p=public_input()
 return {'version':VERSION,'case_id':p['case_id'],'case_version':p['case_version'],'public_sha256':digest(p),'source_sha256':{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest()for n in ('case.py','json_contract.py','play.py')},'max_submissions':1,'max_answer_bytes':8192,'actual_provider_calls':0}

def evaluate(public,answer):
 grade={k:False for k in ('interface_correct','all_tasks_once','windows_correct','dependencies_correct','resources_correct','passed')}
 try:
  exact(answer,('schedule',));items=answer['schedule']
  need(type(items)is list and len(items)<=16,'schedule_bound')
  for item in items:
   exact(item,('task','start'));need(type(item['task'])is str and 1<=len(item['task'])<=32,'task_id');need(type(item['start'])is int and 0<=item['start']<=public['horizon'],'start_tick')
 except (ValueError,TypeError):return grade
 grade['interface_correct']=True
 tasks={t['id']:t for t in public['tasks']};starts={x['task']:x['start']for x in items}
 grade['all_tasks_once']=len(items)==len(tasks) and len(starts)==len(items) and set(starts)==set(tasks)
 if not grade['all_tasks_once']:return grade
 ends={k:starts[k]+t['duration'] for k,t in tasks.items()}
 grade['windows_correct']=all(starts[k]>=t['earliest'] and ends[k]<=min(t['latest_finish'],public['horizon'])for k,t in tasks.items())
 grade['dependencies_correct']=all(ends[d]<=starts[k]for k,t in tasks.items()for d in t['after'])
 grade['resources_correct']=all(ends[a]<=starts[b]or ends[b]<=starts[a]for a in tasks for b in tasks if a<b and tasks[a]['resource']==tasks[b]['resource'])
 grade['passed']=all(grade[k]for k in grade if k!='passed')
 return grade

class Session:
 def __init__(self,expected_manifest_sha256,attempt_id):
  need(type(attempt_id)is str and 1<=len(attempt_id)<=64 and all(c.isascii()and(c.isalnum()or c in '_-')for c in attempt_id),'attempt_id')
  self.manifest=manifest();need(digest(self.manifest)==expected_manifest_sha256,'trusted_case_binding')
  self._public=deepcopy(public_input());need(digest(self._public)==self.manifest['public_sha256'],'admitted_snapshot_binding')
  self.trusted=expected_manifest_sha256;self.attempt_id=attempt_id;self.event=None;self.terminal=None;self.grade={'evaluated':False,'passed':False}
 def public_start(self):return deepcopy(self._public)
 def submit(self,raw):
  need(self.terminal is None,'closed_session');need(type(raw)is str and len(raw.encode('utf-8'))<=8192,'answer_transport_bound')
  try:answer=strict(raw);self.grade=evaluate(self._public,answer)
  except (ValueError,TypeError,UnicodeError,RecursionError):self.grade=evaluate(self._public,None)
  self.grade['evaluated']=True;self.terminal='submitted'if self.grade['interface_correct']else'invalid_answer'
  result={'type':'evaluation','terminal':self.terminal,'grade':deepcopy(self.grade),'actual_provider_calls':0}
  if self.grade['passed']:
   result['authored_schedule']=deepcopy(answer)
   result['authorship_receipt']={'artifact_sha256':digest(answer),'definition_sha256':self.trusted,'foundation_admission':False,'independent_capability_score':False}
  self.event={'raw':raw,'result':deepcopy(result)};return result
 def record(self):return {'version':VERSION,'manifest':deepcopy(self.manifest),'expected_manifest_sha256':self.trusted,'attempt_id':self.attempt_id,'event':deepcopy(self.event),'terminal':self.terminal,'grade':deepcopy(self.grade),'actual_provider_calls':0}

def audit(record,expected_manifest_sha256,expected_attempt_id):
 need(type(record)is dict and record.get('attempt_id')==expected_attempt_id,'attempt_binding');s=Session(expected_manifest_sha256,expected_attempt_id)
 if record.get('event')is not None:
  e=record['event'];exact(e,('raw','result'));s.submit(e['raw'])
 need(same(s.record(),record),'exact_record_replay');return {'verified':True,'passed':s.grade['passed'],'actual_provider_calls':0}
