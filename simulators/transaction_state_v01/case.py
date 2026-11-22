"""Single-answer state reconstruction; no real transactions or external effects."""
import hashlib
from pathlib import Path
from copy import deepcopy
import json_contract,transaction_seam
from json_contract import need,exact,strict,digest,same
ROOT=Path(__file__).resolve().parent
VERSION='optimistic-transaction-state-session-0.1.0'
def snapshot():
 freeze=strict((ROOT/'INPUT_FREEZE.json').read_text(encoding='utf-8'));exact(freeze,('PUBLIC_CASE.json','PRIVATE_GOLD.json'));out={}
 for n,h in freeze.items():
  raw=(ROOT/n).read_bytes();need(hashlib.sha256(raw).hexdigest()==h,'input_freeze');out[n]=strict(raw.decode('utf-8'))
 need(same(transaction_seam.reference(out['PUBLIC_CASE.json']['data']),out['PRIVATE_GOLD.json']['expected']),'literal_gold_disagreement')
 return out

def manifest(snap=None):
 for module in (json_contract,transaction_seam):need(Path(module.__file__).resolve().parent==ROOT,'dependency_origin')
 snap=snapshot()if snap is None else snap;p=snap['PUBLIC_CASE.json']
 return {'version':VERSION,'case_id':p['case_id'],'case_version':p['case_version'],'snapshot_sha256':digest(snap),'source_sha256':{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest()for n in ('case.py','json_contract.py','transaction_seam.py','play.py')},'max_submissions':1,'max_answer_bytes':8192,'actual_provider_calls':0}

class Session:
 def __init__(self,expected_manifest_sha256,attempt_id):
  need(type(attempt_id)is str and 1<=len(attempt_id)<=64 and all(c.isascii()and(c.isalnum()or c in '_-')for c in attempt_id),'attempt_id')
  snap=snapshot();self.manifest=manifest(snap);need(digest(self.manifest)==expected_manifest_sha256,'trusted_case_binding')
  self._public=deepcopy(snap['PUBLIC_CASE.json']);self._gold=deepcopy(snap['PRIVATE_GOLD.json']['expected'])
  self.trusted=expected_manifest_sha256;self.attempt_id=attempt_id;self.event=None;self.terminal=None;self.grade={'evaluated':False,'passed':False}
 def public_start(self):return deepcopy(self._public)
 def submit(self,raw):
  need(self.terminal is None,'closed_session');need(type(raw)is str and len(raw.encode('utf-8'))<=8192,'answer_transport_bound')
  try:answer=strict(raw);valid=transaction_seam.shape(answer)
  except(ValueError,TypeError,UnicodeError,RecursionError):answer=None;valid=False
  grade={'evaluated':True,'interface_correct':valid}
  for k in ('state','revision','evidence','pending','commit_results'):grade[k+'_correct']=bool(valid and same(answer[k],self._gold[k]))
  grade['passed']=all(grade.values());self.grade=grade;self.terminal='submitted'if valid else'invalid_answer'
  result={'type':'evaluation','terminal':self.terminal,'grade':deepcopy(grade),'actual_provider_calls':0};self.event={'raw':raw,'result':deepcopy(result)};return result
 def record(self):return {'version':VERSION,'manifest':deepcopy(self.manifest),'expected_manifest_sha256':self.trusted,'attempt_id':self.attempt_id,'event':deepcopy(self.event),'terminal':self.terminal,'grade':deepcopy(self.grade),'actual_provider_calls':0}

def audit(record,expected_manifest_sha256,expected_attempt_id):
 need(type(record)is dict and record.get('attempt_id')==expected_attempt_id,'attempt_binding');s=Session(expected_manifest_sha256,expected_attempt_id)
 if record.get('event')is not None:
  e=record['event'];exact(e,('raw','result'));s.submit(e['raw'])
 need(same(s.record(),record),'exact_record_replay');return {'verified':True,'passed':s.grade['passed'],'actual_provider_calls':0}
