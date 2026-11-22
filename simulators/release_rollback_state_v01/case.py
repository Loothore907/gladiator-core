"""Fixed single-response rollback-state candidate; no external effects or live driver."""
import json,hashlib
from pathlib import Path
from copy import deepcopy
import state_seam,json_contract
from json_contract import strict,canonical,digest,need,exact,same
ROOT=Path(__file__).resolve().parent
VERSION='release-rollback-state-session-0.1.0'
def data(name):return strict((ROOT/name).read_text(encoding='utf-8-sig'))
def validate_input(v):
 exact(v,('initial','messages'));initial=v['initial'];messages=v['messages']
 need(type(initial)is dict and 1<=len(initial)<=4 and type(messages)is list and 1<=len(messages)<=8,'state_bounds')
 def value(x):return x is None or type(x)is str and len(x)<=64
 need(all(type(k)is str and 1<=len(k)<=32 and value(x)for k,x in initial.items()),'state_fields')
 seen=set();owners=set()
 for m in messages:
  need(type(m)is dict and type(m.get('id'))is str and 1<=len(m['id'])<=32 and m['id']not in seen and m.get('role')in('owner','guest'),'message_identity');seen.add(m['id'])
  if set(m)=={'id','role','set'}:
   need(type(m['set'])is dict and bool(m['set'])and set(m['set'])<=set(initial)and all(value(x)for x in m['set'].values()),'set_fields')
   if m['role']=='owner':owners.add(m['id'])
  else:
   exact(m,('id','role','retract'));need(type(m['retract'])is str and m['retract']in owners,'earlier_owner_set')
 return v

def manifest():
 for m in(state_seam,json_contract):need(Path(m.__file__).resolve().parent==ROOT,'dependency_origin')
 public=data('PUBLIC_CASE.json');validate_input(public['data']);gold=data('PRIVATE_GOLD.json')['expected']
 need(same(state_seam.state_reference(public['data']),gold),'literal_gold_disagreement')
 return {'version':VERSION,'case_id':public['case_id'],'case_version':public['case_version'],'source_sha256':{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest()for n in('case.py','state_seam.py','json_contract.py','play.py')},'public_sha256':digest(public),'private_sha256':digest(data('PRIVATE_GOLD.json')),'max_submissions':1,'max_answer_bytes':8192,'max_logical_elapsed_ms':1000,'actual_provider_calls':0}
class Session:
 def __init__(self,expected_manifest_sha256,attempt_id):
  need(type(attempt_id)is str and 1<=len(attempt_id)<=64 and all(c.isascii()and(c.isalnum()or c in '_-')for c in attempt_id),'attempt_id')
  self.manifest=manifest();need(digest(self.manifest)==expected_manifest_sha256,'trusted_case_binding')
  self._public=deepcopy(data('PUBLIC_CASE.json'));self._private=deepcopy(data('PRIVATE_GOLD.json'))
  need(digest(self._public)==self.manifest['public_sha256']and digest(self._private)==self.manifest['private_sha256'],'admitted_snapshot_binding')
  self.trusted=expected_manifest_sha256;self.attempt_id=attempt_id;self.event=None;self.terminal=None;self.grade={'evaluated':False,'passed':False}
 def public_start(self):return deepcopy(self._public)
 def submit(self,raw,elapsed_ms=0):
  need(self.terminal is None,'closed_session');need(type(raw)is str and len(raw.encode('utf-8'))<=8192,'answer_transport_bound');need(type(elapsed_ms)is int and 0<=elapsed_ms<2**53,'logical_elapsed')
  answer=None;valid=False
  if elapsed_ms>1000:self.terminal='time_limit';self.grade={'evaluated':False,'passed':False}
  else:
   try:answer=strict(raw);valid=state_seam.state_shape({'input':self.public_start()['data']},answer)
   except(ValueError,TypeError,UnicodeError,RecursionError):valid=False
   self.terminal='submitted'if valid else'invalid_answer'
   gold=self._private['expected'];state_ok=valid and same(answer['state'],gold['state']);evidence_ok=valid and same(answer['evidence'],gold['evidence'])
   self.grade={'evaluated':True,'interface_correct':bool(valid),'state_correct':bool(state_ok),'evidence_correct':bool(evidence_ok),'passed':bool(valid and state_ok and evidence_ok)}
  result={'type':'evaluation','terminal':self.terminal,'grade':deepcopy(self.grade),'actual_provider_calls':0};self.event={'raw':raw,'elapsed_ms':elapsed_ms,'result':deepcopy(result)};return result
 def record(self):return {'version':VERSION,'manifest':deepcopy(self.manifest),'expected_manifest_sha256':self.trusted,'attempt_id':self.attempt_id,'event':deepcopy(self.event),'terminal':self.terminal,'grade':deepcopy(self.grade),'actual_provider_calls':0}
def audit(record,expected_manifest_sha256,expected_attempt_id):
 need(type(record)is dict and record.get('attempt_id')==expected_attempt_id,'attempt_binding');s=Session(expected_manifest_sha256,expected_attempt_id)
 if record.get('event')is not None:
  e=record['event'];exact(e,('raw','elapsed_ms','result'));s.submit(e['raw'],e['elapsed_ms'])
 need(same(s.record(),record),'exact_record_replay');return {'verified':True,'passed':s.grade['passed'],'actual_provider_calls':0}
