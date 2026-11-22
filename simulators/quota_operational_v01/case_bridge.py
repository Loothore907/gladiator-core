"""Exact-source adapter for the frozen quota SQL case. No provider or filesystem actions.

Public output adds explicit action syntax to original public_delivery; step output is unchanged.
Records are operator-only.
Logical elapsed_ms is fixed zero, NOT measured SQL time or session wall time.
The original SQLite VM, per-query .25s, SQL and result limits remain unchanged.
"""
import builtins, hashlib, json, types, sqlite3
from pathlib import Path
from copy import deepcopy
ROOT=Path(__file__).resolve().parent
SOURCE=ROOT.parent/'quota_prefix_repair_v01'
VERSION='quota-operational-case-bridge-0.1'
PINS={'session.py': '32bac75cabb0213902426daba36cfd9303c8e7d175e44483ed3ef067cf5b4a07', 'oracle.py': 'e7cf73e1d32cb013e3293fb7d23c47b7d0ad53eb332e6058c77c9115276d3d64', 'sql_executor.py': '7292aa00da29c15e78c1143d165e12ccc9f89df3716d13477f027ffa7ef6fea2', 'json_contract.py': '1c5a4bd673283db8bac91cabed04568032f88c08f1801be5200d9d28f945e4f1', 'play.py': 'd2d13fa34fcd890fdc1286d9987c98cca7b737b6299c7e6e34905711f1aa2a00', 'INPUT_FREEZE.json': 'e962495d4c3d43bffae8291eac18070aae7d0064a5e7b3030170588995d753b7', 'PUBLIC_INPUT.json': '7261abeaf571d78366858a59401f57994e83add8d468ade2fd88cadb486b86f6', 'PRIVATE_REFERENCE.json': '10a28a4f72e6429a05323f1603f05d250dbb91ddd60d2e10807bcf0a14d69f83'}
def need(ok,msg):
 if not ok:raise ValueError('Interval bridge: '+msg)
def sha(raw):return hashlib.sha256(raw).hexdigest()
def canonical(x):return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()
def digest(x):return sha(canonical(x))
def same(a,b):return canonical(a)==canonical(b)
def verified():
 need(not SOURCE.is_symlink(),'source directory')
 result={}
 for name,pin in PINS.items():
  p=SOURCE/name;need(p.is_file() and not p.is_symlink(),'source file');raw=p.read_bytes();need(sha(raw)==pin,'source/input drift '+name);result[name]=raw
 return result
_cached=None
def original():
 global _cached
 raw=verified()
 if _cached is None:
  modules={}
  def local_import(name,globals=None,locals=None,fromlist=(),level=0):
   if level==0 and name in modules:return modules[name]
   return builtins.__import__(name,globals,locals,fromlist,level)
  for name in ('json_contract','sql_executor','oracle','session'):
   module=types.ModuleType('_quota_bound_'+name);module.__file__=str(SOURCE/(name+'.py'))
   module.__dict__['__builtins__']={**vars(builtins),'__import__':local_import}
   exec(compile(raw[name+'.py'],module.__file__,'exec'),module.__dict__)
   modules[name]=module
  c=modules['session']
  _cached=c
 return _cached

def manifest():
 c=original()
 return {'version':VERSION,'case_id':'quota_prefix_repair_v01','source_sha256':dict(PINS),
  'bridge_sha256':sha(Path(__file__).read_bytes()),'input_sha256':digest({n:PINS[n] for n in ('INPUT_FREEZE.json','PUBLIC_INPUT.json','PRIVATE_REFERENCE.json')}),
  'original_definition_sha256':c.digest(c.definition()),'sqlite_version':sqlite3.sqlite_version,
  'max_actions':10,'max_action_bytes':32768,'max_sql_bytes':8192,'logical_elapsed_ms_per_action':0,
  'logical_elapsed_policy':'fixed zero; not measured wall time','original_logical_limit_ms':1000,
  'actual_provider_calls':0}
class Session:
 def __init__(self,expected_manifest_sha256,attempt_id):
  need(type(attempt_id)is str and 1<=len(attempt_id)<=64 and attempt_id.isascii() and all(c.isalnum() or c in '_-' for c in attempt_id),'attempt_id')
  self.manifest=manifest();need(digest(self.manifest)==expected_manifest_sha256,'trusted_manifest')
  self.expected=expected_manifest_sha256;self.attempt_id=attempt_id
  self._binding={'scope':VERSION,'manifest_sha256':self.expected,'attempt_id':attempt_id}
  self._session=original().Session('candidate',self._binding)
 def _check(self):need(same(manifest(),self.manifest),'session source drift')
 @property
 def terminal(self):return self._session.reason
 @property
 def events(self):
  return [dict(deepcopy(e),raw=e['action']) for e in self._session.events]
 def public_start(self):
  self._check();public=original().public_delivery()
  def variant(op,fields):
   properties={'op':{'const':op},**fields}
   return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}
  public['action_contract']={
   'version':'quota-action-contract-0.2',
   'transport':'Call submit_action with action_json set to a JSON-encoded object matching exactly one schema below. The required operation key is op, NOT action. No extra or duplicate keys.',
   'schema':{'oneOf':[
    variant('read',{'path':{'type':'string','enum':sorted(original().inputs()['repository'])}}),
    variant('write',{'path':{'const':'query.sql'},'content':{'type':'string','minLength':1}}),
    variant('test',{}),variant('finish',{})]},
   'byte_constraints':{'action_utf8_max':32768,'write_content_utf8_max':8192,'write_content_forbids':'U+0000'},
   'examples':{'read':{'op':'read','path':'README.txt'},'write':{'op':'write','path':'query.sql','content':'SELECT 1'},'test':{'op':'test'},'finish':{'op':'finish'}},
   'example_note':'The write example demonstrates syntax only; SELECT 1 is not a solution. Finish ends the session and grades the current query. Read README.txt first for the task and SQL restrictions.',
   'first_action':{'op':'read','path':'README.txt'}}
  return public
 def step(self,raw):
  self._check();result=self._session.step(raw,elapsed_ms=0,provider_state='completed')
  return {'type':'observation','result':result,'remaining_actions':10-len(self._session.events),'terminal':self.terminal}
 def grade(self):
  self._check();g=deepcopy(self._session.grade)
  return dict(g,task_evaluated=g['evaluated'],actual_provider_calls=0)
 def record(self):
  self._check()
  return {'version':VERSION,'manifest':deepcopy(self.manifest),'expected_manifest_sha256':self.expected,
   'attempt_id':self.attempt_id,'events':self.events,'terminal':self.terminal,'grade':self.grade(),
   'original_record':self._session.record(),'actual_provider_calls':0}
def audit(record,expected_manifest_sha256,expected_attempt_id):
 need(type(record)is dict and record.get('attempt_id')==expected_attempt_id,'record binding')
 need(type(record.get('events'))is list and len(record['events'])<=10,'event bound')
 s=Session(expected_manifest_sha256,expected_attempt_id)
 for event in record['events']:
  need(type(event)is dict and type(event.get('raw'))is str,'event raw');s.step(event['raw'])
 original().audit_record('candidate',record['original_record'],s._binding)
 need(same(s.record(),record),'exact bridge replay')
 return {'verified':True,'passed':s.grade()['passed'],'actual_provider_calls':0}
