"""Controlled paginated snapshot refresh; local report only, no remote service."""
from pathlib import Path
from copy import deepcopy
import hashlib,json_contract
from json_contract import strict,exact,need,digest,same
ROOT=Path(__file__).resolve().parent
VERSION='snapshot-scan-recovery-0.1.0'
def ident(x):return type(x)is str and 1<=len(x)<=32 and x.isascii()and all(c.isalnum()or c in '_-'for c in x)
def load_inputs():
 freeze=strict((ROOT/'INPUT_FREEZE.json').read_text());exact(freeze,('PUBLIC_CASE.json','PRIVATE_FIXTURE.json'));v={}
 for n,h in freeze.items():
  raw=(ROOT/n).read_bytes();need(hashlib.sha256(raw).hexdigest()==h,'input_drift');v[n]=strict(raw.decode())
 f=v['PRIVATE_FIXTURE.json'];exact(f,('initial','refresh'));need(type(f['initial'])is dict and len(f['initial'])==6,'six_records')
 for k,row in f['initial'].items():
  need(ident(k),'record_id');exact(row,('stock','minimum'));need(all(type(x)is int and 0<=x<=100 for x in row.values()),'record_values')
 need(type(f['refresh'])is dict and set(f['refresh'])<=set(f['initial'])and all(type(x)is int and 0<=x<=100 for x in f['refresh'].values()),'refresh')
 return v

def manifest(v=None):
 v=load_inputs()if v is None else v;need(Path(json_contract.__file__).resolve().parent==ROOT,'dependency_origin')
 return {'version':VERSION,'input_sha256':digest(v),'source_sha256':{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest()for n in ('case.py','json_contract.py','play.py')},'max_actions':16,'max_action_bytes':8192,'actual_provider_calls':0}
class Session:
 def __init__(self,expected_manifest_sha256,attempt_id):
  need(type(attempt_id)is str and 1<=len(attempt_id)<=64 and attempt_id.isascii()and all(c.isalnum()or c in '_-'for c in attempt_id),'attempt_id');self.inputs=load_inputs();self.manifest=manifest(self.inputs);need(digest(self.manifest)==expected_manifest_sha256,'trusted_manifest');self.expected=expected_manifest_sha256;self.attempt_id=attempt_id
  self.initial=deepcopy(self.inputs['PRIVATE_FIXTURE.json']['initial']);self.store=deepcopy(self.initial);self.order=sorted(self.store);self.version=1;self.scan_version=None;self.scan_next=None;self.complete=False;self.events=[];self.fault=None;self.report=None;self.first_report=None;self.knowledge_correct=True;self.terminal=None
 def public_start(self):return deepcopy(self.inputs['PUBLIC_CASE.json'])
 def reset_scan(self):self.scan_version=None;self.scan_next=None;self.complete=False
 def state(self):return {'store':deepcopy(self.store),'version':self.version,'report':deepcopy(self.report)}
 def selected(self):return sorted(k for k,v in self.store.items()if v['stock']<v['minimum'])
 def step(self,raw):
  need(self.terminal is None,'closed_session');need(type(raw)is str and len(raw.encode('utf-8'))<=8192,'action_transport_bound');before=self.state();valid=True;result={'error':'invalid_action'};injection=None;a=None
  try:
   a=strict(raw);need(type(a)is dict and type(a.get('op'))is str,'op');op=a['op'];need(op in ('page','publish','finish'),'op');exact(a,('op','cursor','version')if op=='page'else('op','version','ids')if op=='publish'else('op',))
   if op=='page':need(type(a['cursor'])is int and a['cursor']in (0,2,4)and(a['version']is None if a['cursor']==0 else type(a['version'])is int and 1<=a['version']<=100),'page_fields')
   if op=='publish':need(type(a['version'])is int and 1<=a['version']<=100 and type(a['ids'])is list and len(a['ids'])<=6 and all(ident(x)for x in a['ids'])and a['ids']==sorted(set(a['ids'])),'report_fields')
  except(ValueError,TypeError,UnicodeError,RecursionError):valid=False
  if valid:
   if op=='page':
    cursor=a['cursor']
    if cursor!=0 and a['version']!=self.version:self.reset_scan();result={'error':'snapshot_expired','current_version':self.version}
    elif cursor!=0 and(self.scan_version!=self.version or self.scan_next!=cursor):valid=False;result={'error':'invalid_scan_cursor'}
    elif cursor!=0 and self.fault is None:
     updates=self.inputs['PRIVATE_FIXTURE.json']['refresh']
     for k,value in updates.items():self.store[k]['stock']=value
     self.version+=1;self.reset_scan();injection={'stock_updates':deepcopy(updates),'model_authored':False};self.fault={'event_index':len(self.events),**injection};result={'error':'snapshot_expired','current_version':self.version}
    else:
     if cursor==0:self.scan_version=self.version;self.complete=False
     self.scan_next=cursor+2 if cursor+2<len(self.order)else None;self.complete=self.scan_next is None;result={'records':[dict(id=k,**deepcopy(self.store[k]))for k in self.order[cursor:cursor+2]],'version':self.version,'next_cursor':self.scan_next}
   elif op=='publish':
    if a['version']!=self.version:result={'error':'version_conflict','current_version':self.version}
    elif not self.complete or self.scan_version!=self.version:self.knowledge_correct=False;result={'error':'complete_current_scan_required'}
    else:
     self.report={'version':a['version'],'ids':deepcopy(a['ids'])}
     if self.first_report is None:self.first_report={'report':deepcopy(self.report),'correct':a['ids']==self.selected()}
     result={'accepted_report':True,'version':self.version}
   else:self.terminal='finished';result={'finished':True}
  self.events.append({'raw':raw,'action':deepcopy(a),'before':before,'after':self.state(),'result':deepcopy(result),'interface_valid':valid,'injection':injection})
  if self.terminal is None and len(self.events)>=16:self.terminal='action_limit'
  return {'type':'observation','result':deepcopy(result),'remaining_actions':16-len(self.events),'terminal':self.terminal}
 def grade(self):
  correct=self.report=={'version':self.version,'ids':self.selected()};fresh=self.complete and self.scan_version==self.version and self.fault is not None;interface=all(e['interface_valid']for e in self.events);finished=self.terminal=='finished';expected=deepcopy(self.initial)
  if self.fault:
   for k,value in self.inputs['PRIVATE_FIXTURE.json']['refresh'].items():expected[k]['stock']=value
  preserved=same(expected,self.store)
  return {'task_evaluated':finished,'report_correct':correct,'fresh_complete_scan':fresh,'controlled_refresh_encountered':self.fault is not None,'source_state_preserved':preserved,'interface_correct':interface,'knowledge_workflow_correct':self.knowledge_correct,'first_report_correct':None if self.first_report is None else self.first_report['correct'],'explicit_finish':finished,'passed':bool(finished and correct and fresh and preserved and interface and self.knowledge_correct),'actual_provider_calls':0}
 def record(self):return {'version':VERSION,'manifest':deepcopy(self.manifest),'expected_manifest_sha256':self.expected,'attempt_id':self.attempt_id,'events':deepcopy(self.events),'state':self.state(),'fault':deepcopy(self.fault),'first_report':deepcopy(self.first_report),'terminal':self.terminal,'grade':self.grade(),'actual_provider_calls':0}
def audit(record,expected_manifest_sha256,expected_attempt_id):
 need(type(record)is dict and record.get('attempt_id')==expected_attempt_id and type(record.get('events'))is list and len(record['events'])<=16,'record_binding');s=Session(expected_manifest_sha256,expected_attempt_id)
 for event in record['events']:s.step(event['raw'])
 need(same(s.record(),record),'exact_replay');return {'verified':True,'passed':s.grade()['passed'],'actual_provider_calls':0}
