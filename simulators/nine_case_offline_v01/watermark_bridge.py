"""Pinned single-answer local-state adapter. No external state changes, provider or ledger actions.
"""
import builtins,types,json,hashlib
from pathlib import Path
from copy import deepcopy
ROOT=Path(__file__).resolve().parent
SOURCE=ROOT.parent/'watermark_state_v01'
VERSION='watermark_state_v01-operational-bridge-0.1'
PINS={'case.py': 'e80e9c384b0c3408183678c1205a8c9968b90ebb5f62348b515d6cbf25521d02', 'json_contract.py': '1c5a4bd673283db8bac91cabed04568032f88c08f1801be5200d9d28f945e4f1', 'play.py': 'b30394a89b238f150b0cfbbface6b452a4be3c225135af4aa18c196a2a266ef6', 'PUBLIC_CASE.json': 'f7186a89a79af782bea1e836b5fcf518a2c7deae8e29311cc4d7f91b9d961a57', 'PRIVATE_GOLD.json': '7e3fb34a566fdef4b7f66289c0668879cf25165a6434555daaf4d3f5de3b425d', 'stream_seam.py': 'f6398e08d5bda323ddb79c832321865e6eaa9ce56cd7fa35591e194e3defd737', 'INPUT_FREEZE.json': 'e1f4ed7ce108df7868a2cf835645b36652912b5fdde5e9038f51f913a29fce89'}

def need(ok,msg):
 if not ok:raise ValueError('watermark_state_v01 bridge: '+msg)
def sha(raw):return hashlib.sha256(raw).hexdigest()
def canonical(x):return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()
def digest(x):return sha(canonical(x))
def same(a,b):return canonical(a)==canonical(b)
def verified():
 need(not SOURCE.is_symlink(),'source directory');raw={}
 for name,h in PINS.items():
  p=SOURCE/name;need(p.is_file()and not p.is_symlink(),'source file');raw[name]=p.read_bytes();need(sha(raw[name])==h,'source/input drift '+name)
 return raw
_cached=None
def original():
 global _cached
 raw=verified()
 if _cached is None:
  modules={}
  def local_import(name,globals=None,locals=None,fromlist=(),level=0):
   if level==0 and name in modules:return modules[name]
   return builtins.__import__(name,globals,locals,fromlist,level)
  for name in ('json_contract','stream_seam','case'):
   m=types.ModuleType('_watermark_state_v01_bound_'+name);m.__file__=str(SOURCE/(name+'.py'));m.__dict__['__builtins__']={**vars(builtins),'__import__':local_import}
   exec(compile(raw[name+'.py'],m.__file__,'exec'),m.__dict__);modules[name]=m
  _cached=modules['case']
 return _cached

def manifest():
 c=original()
 return {'version':VERSION,'case_id':'watermark_state_v01','source_sha256':dict(PINS),'bridge_sha256':sha(Path(__file__).read_bytes()),'original_manifest':c.manifest(),'max_actions':1,'max_action_bytes':8192,'actual_provider_calls':0}
class Session:
 def __init__(self,expected_manifest_sha256,attempt_id):
  self.manifest=manifest();need(digest(self.manifest)==expected_manifest_sha256,'trusted_manifest');self.expected=expected_manifest_sha256;self.attempt_id=attempt_id;c=original();self._session=c.Session(c.digest(self.manifest['original_manifest']),attempt_id);self._events=[]
 def _check(self):need(same(manifest(),self.manifest),'session source drift')
 @property
 def terminal(self):return self._session.terminal
 @property
 def events(self):return deepcopy(self._events)
 def public_start(self):self._check();return self._session.public_start()
 def step(self,raw):
  self._check();need(self.terminal is None,'closed_session')
  result=self._session.submit(raw)
  self._events.append({'raw':raw,'interface_valid':result['grade']['interface_correct'],'result':deepcopy(result)})
  return result
 def grade(self):self._check();return deepcopy(self._session.grade)
 def record(self):
  self._check();return {'version':VERSION,'manifest':deepcopy(self.manifest),'expected_manifest_sha256':self.expected,'attempt_id':self.attempt_id,'events':self.events,'terminal':self.terminal,'grade':self.grade(),'original_record':self._session.record(),'actual_provider_calls':0}
def audit(record,expected_manifest_sha256,expected_attempt_id):
 need(type(record)is dict and record.get('attempt_id')==expected_attempt_id and type(record.get('events'))is list and len(record['events'])<=1,'record binding');s=Session(expected_manifest_sha256,expected_attempt_id)
 for e in record['events']:
  need(type(e)is dict and type(e.get('raw'))is str,'event raw');s.step(e['raw'])
 c=original();c.audit(record['original_record'],c.digest(s.manifest['original_manifest']),expected_attempt_id)
 need(same(s.record(),record),'exact bridge replay');return {'verified':True,'passed':s.grade()['passed'],'actual_provider_calls':0}
