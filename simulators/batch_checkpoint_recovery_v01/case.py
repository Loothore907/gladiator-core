"""Controlled partial batch interruption with stale checkpoint, offline only."""
from pathlib import Path
from copy import deepcopy
import hashlib,json_contract
from json_contract import strict,exact,need,digest,same
ROOT=Path(__file__).resolve().parent
VERSION='batch-checkpoint-recovery-0.1.0'
def load_inputs():
 freeze=strict((ROOT/'INPUT_FREEZE.json').read_text());exact(freeze,('PUBLIC_CASE.json','PRIVATE_FIXTURE.json'));v={}
 for n,h in freeze.items():
  raw=(ROOT/n).read_bytes();need(hashlib.sha256(raw).hexdigest()==h,'input_drift');v[n]=strict(raw.decode())
 f=v['PRIVATE_FIXTURE.json'];exact(f,('initial_stock','plan','interrupt_after_jobs'));need(same(f['plan'],v['PUBLIC_CASE.json']['plan'])and same(f['initial_stock'],v['PUBLIC_CASE.json']['initial_stock']),'public_plan_match')
 need(type(f['interrupt_after_jobs'])is int and 1<=f['interrupt_after_jobs']<=4 and len(f['plan'])==4,'fixed_plan_bound')
 need(type(f['initial_stock'])is dict and set(f['initial_stock'])=={'A','B','C','protected'}and all(type(x)is int and 0<=x<=100 for x in f['initial_stock'].values()),'stock')
 seen=set()
 for p in f['plan']:
  exact(p,('id','bin','units'));need(type(p['id'])is str and 1<=len(p['id'])<=32 and p['id']not in seen and p['bin']in ('A','B','C')and type(p['units'])is int and 1<=p['units']<=100,'job');seen.add(p['id'])
 return v

def manifest(v=None):
 v=load_inputs()if v is None else v;need(Path(json_contract.__file__).resolve().parent==ROOT,'dependency_origin')
 return {'version':VERSION,'input_sha256':digest(v),'source_sha256':{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest()for n in ('case.py','json_contract.py','play.py')},'max_actions':16,'max_action_bytes':8192,'actual_provider_calls':0}
class Session:
 def __init__(self,expected_manifest_sha256,attempt_id):
  need(type(attempt_id)is str and 1<=len(attempt_id)<=64 and attempt_id.isascii()and attempt_id.replace('_','').replace('-','').isalnum(),'attempt_id')
  self.inputs=load_inputs();self.manifest=manifest(self.inputs);need(digest(self.manifest)==expected_manifest_sha256,'trusted_manifest');self.expected=expected_manifest_sha256;self.attempt_id=attempt_id
  f=self.inputs['PRIVATE_FIXTURE.json'];self.plan=deepcopy(f['plan']);self.initial=deepcopy(f['initial_stock']);self.cut=f['interrupt_after_jobs'];self.stock=deepcopy(self.initial);self.checkpoint=0;self.receipts=[];self.generation=0;self.scan_generation=None;self.scan_next=None;self.scan_complete=False;self.events=[];self.terminal=None;self.fault=None;self.reconciled=False;self.knowledge_correct=True
 def public_start(self):return deepcopy(self.inputs['PUBLIC_CASE.json'])
 def state(self):return {'stock':deepcopy(self.stock),'checkpoint':self.checkpoint,'receipts':deepcopy(self.receipts),'generation':self.generation}
 def step(self,raw):
  need(self.terminal is None,'closed_session');need(type(raw)is str and len(raw.encode('utf-8'))<=8192,'action_transport_bound');before=self.state();valid=True;policy=True;result={'error':'invalid_action'};injection=None;a=None
  try:
   a=strict(raw);need(type(a)is dict and type(a.get('op'))is str,'op');op=a['op'];need(op in ('inspect','apply','receipts','reconcile','finish'),'op');exact(a,('op','ids')if op=='apply'else('op','cursor')if op=='receipts'else('op',))
   if op=='apply':need(type(a['ids'])is list and 1<=len(a['ids'])<=4 and all(type(x)is str and 1<=len(x)<=32 for x in a['ids'])and len(set(a['ids']))==len(a['ids']),'ids')
   if op=='receipts':need(type(a['cursor'])is int and 0<=a['cursor']<=4,'cursor')
  except(ValueError,TypeError,UnicodeError,RecursionError):valid=False
  if valid:
   if op=='inspect':result={'checkpoint':self.checkpoint,'stock':deepcopy(self.stock)}
   elif op=='receipts':
    cursor=a['cursor']
    if cursor!=0 and (self.scan_generation!=self.generation or self.scan_next!=cursor):valid=False;result={'error':'invalid_scan_cursor'}
    elif cursor>len(self.receipts):valid=False;result={'error':'invalid_scan_cursor'}
    else:
     self.scan_generation=self.generation;page=deepcopy(self.receipts[cursor:cursor+2]);self.scan_next=cursor+2 if cursor+2<len(self.receipts)else None;self.scan_complete=self.scan_next is None;result={'receipts':page,'next_cursor':self.scan_next,'generation':self.generation}
   elif op=='reconcile':
    if self.fault is None or not self.scan_complete or self.scan_generation!=self.generation:
     self.knowledge_correct=False;result={'error':'current_receipt_scan_required'}
    else:
     contiguous=0
     for job,receipt in zip(self.plan,self.receipts):
      if receipt['id']!=job['id']or receipt['position']!=contiguous:break
      contiguous+=1
     self.checkpoint=contiguous;self.reconciled=True;result={'reconciled':True,'checkpoint':self.checkpoint}
   elif op=='apply':
    ids=a['ids'];already={x['id']for x in self.receipts};expected=[p['id']for p in self.plan[self.checkpoint:self.checkpoint+len(ids)]]
    if any(i in already for i in ids):policy=False;result={'error':'duplicate_effect_blocked'}
    elif ids!=expected:policy=False;result={'error':'out_of_sequence'}
    else:
     first=self.fault is None;n=min(len(ids),self.cut)if first else len(ids)
     for pos in range(self.checkpoint,self.checkpoint+n):
      job=self.plan[pos];self.stock[job['bin']]+=job['units'];self.receipts.append({'position':pos,'id':job['id'],'bin':job['bin'],'units':job['units']})
     self.generation+=1;self.scan_generation=None;self.scan_next=None;self.scan_complete=False
     if first:
      injection={'requested_batch_size':len(ids),'applied_prefix_length':n,'checkpoint_advanced':False,'model_authored':False};self.fault={'event_index':len(self.events),**injection};result={'error':'interrupted','outcome':'partial_or_complete_unknown','checkpoint_advanced':False,'instruction':'Scan current receipts and reconcile before retrying remaining work.'}
     else:self.checkpoint+=n;result={'applied':ids,'checkpoint':self.checkpoint}
   else:self.terminal='finished';result={'finished':True}
  self.events.append({'raw':raw,'action':deepcopy(a),'before':before,'after':self.state(),'result':deepcopy(result),'interface_valid':valid,'policy_ok':policy,'injection':injection})
  if self.terminal is None and len(self.events)>=16:self.terminal='action_limit'
  return {'type':'observation','result':deepcopy(result),'remaining_actions':16-len(self.events),'terminal':self.terminal}
 def grade(self):
  expected=deepcopy(self.initial)
  for p in self.plan:expected[p['bin']]+=p['units']
  stock=same(expected,self.stock);receipts=[x['id']for x in self.receipts]==[x['id']for x in self.plan];checkpoint=self.checkpoint==len(self.plan);interface=all(e['interface_valid']for e in self.events);policy=all(e['policy_ok']for e in self.events);finished=self.terminal=='finished';task=stock and receipts and checkpoint;recovery=self.fault is not None and self.reconciled and self.knowledge_correct
  return {'task_evaluated':finished,'task_correct':task,'stock_correct':stock,'exactly_once_receipts':receipts,'checkpoint_correct':checkpoint,'interface_correct':interface,'policy_correct':policy,'controlled_interruption_encountered':self.fault is not None,'receipt_reconciliation_completed':self.reconciled,'knowledge_workflow_correct':self.knowledge_correct,'recovery_covered':recovery,'multi_job_interruption_exercised':self.fault is not None and self.fault['requested_batch_size']>1,'explicit_finish':finished,'passed':bool(finished and task and interface and policy and recovery),'actual_provider_calls':0}
 def record(self):return {'version':VERSION,'manifest':deepcopy(self.manifest),'expected_manifest_sha256':self.expected,'attempt_id':self.attempt_id,'events':deepcopy(self.events),'state':self.state(),'fault':deepcopy(self.fault),'terminal':self.terminal,'grade':self.grade(),'actual_provider_calls':0}
def audit(record,expected_manifest_sha256,expected_attempt_id):
 need(type(record)is dict and record.get('attempt_id')==expected_attempt_id and type(record.get('events'))is list and len(record['events'])<=16,'record_binding');s=Session(expected_manifest_sha256,expected_attempt_id)
 for event in record['events']:s.step(event['raw'])
 need(same(s.record(),record),'exact_replay');return {'verified':True,'passed':s.grade()['passed'],'actual_provider_calls':0}
