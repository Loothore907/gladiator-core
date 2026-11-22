"""Exact-source adapter for the frozen interval SQL case. No provider or filesystem actions.

Public output adds explicit action syntax to original public_delivery; step output is unchanged.
Records are operator-only.
Logical elapsed_ms is fixed zero, NOT measured SQL time or session wall time.
The original SQLite VM, per-query .25s, SQL and result limits remain unchanged.
"""
import builtins, hashlib, json, types, sqlite3
from pathlib import Path
from copy import deepcopy
ROOT=Path(__file__).resolve().parent
SOURCE=ROOT.parent/'interval_union_repair_v01'
VERSION='interval-operational-case-bridge-0.2'
PINS={
 'coding_session.py':'588067e45106fc74be4057f888df56942e22e5400e9e5c4351f243daf2c72d92',
 'sql_executor.py':'c052fa95dec7fe8c9465a67ee611c56d8176cbe5a189fe68de9a4def1cf6028a',
 'INPUT_FREEZE.json':'9e56b612c944ad02e4fae877f1c53b4101e35999d548afff2a2b10db3f1796e6',
 'PUBLIC_INPUT.json':'04cf387947ea11c3aa6f2c8f1052dddf7813910ff4ec4b1418c9863388bde2cb',
 'PRIVATE_REFERENCE.json':'78120481d609a908eb75bd7e168f8ceffa45b202867ba66e88e042eadad7c854'}
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
  sql=types.ModuleType('_interval_bound_sql');sql.__file__=str(SOURCE/'sql_executor.py')
  exec(compile(raw['sql_executor.py'],sql.__file__,'exec'),sql.__dict__)
  c=types.ModuleType('_interval_bound_coding');c.__file__=str(SOURCE/'coding_session.py')
  def local_import(name,globals=None,locals=None,fromlist=(),level=0):
   if name=='sql_executor' and level==0:return sql
   return builtins.__import__(name,globals,locals,fromlist,level)
  c.__dict__['__builtins__']={**vars(builtins),'__import__':local_import}
  exec(compile(raw['coding_session.py'],c.__file__,'exec'),c.__dict__)
  _cached=c
 return _cached

def manifest():
 c=original()
 return {'version':VERSION,'case_id':'interval_union_repair_v01','source_sha256':dict(PINS),
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
   'version':'interval-action-contract-0.2',
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
