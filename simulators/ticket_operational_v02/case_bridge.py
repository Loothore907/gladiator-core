"""Pinned changing-permission simulation. Public observations delegate original case.
Private records include original state for replay. No external ticket effects.
"""
import builtins,types,json,hashlib
from pathlib import Path
from copy import deepcopy
ROOT=Path(__file__).resolve().parent
SOURCE=ROOT.parent/'ticket_revocation_recovery_v01'
VERSION='ticket-operational-case-bridge-0.1'
PINS={'case.py': '560548ce898f88174e5b6e32ed4d00bd67e70d81a3ca8e89187364e151c564f8', 'action_seam.py': '7623cfd3c7215a1ae1bf06db18c79974cf1c4b78c3491c9f7b345e0df7f59727', 'json_contract.py': '1c5a4bd673283db8bac91cabed04568032f88c08f1801be5200d9d28f945e4f1', 'play.py': '40f61bf9c85fe382bc090346260c45142f89067a4d1eaa9e703d37777978baf2', 'INPUT_FREEZE.json': 'a42e93a455b1bf8bafe05794673cd5c163e158b51b1f4b7fdc4d79244df4bd41', 'PUBLIC_CASE.json': '951416c84a1189ee7ab8095ae51f82e3fdc2f4bdaa1206d2132698b53536c4a7', 'PRIVATE_FIXTURE.json': 'c09e9095db9a3b292282ecdaf9745eb57b7b7d9c30db7fdb912ecccfc7b69ef3'}

def need(ok,msg):
 if not ok:raise ValueError('Ticket bridge: '+msg)
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
  for name in ('json_contract','action_seam','case'):
   m=types.ModuleType('_ticket_bound_'+name);m.__file__=str(SOURCE/(name+'.py'));m.__dict__['__builtins__']={**vars(builtins),'__import__':local_import}
   exec(compile(raw[name+'.py'],m.__file__,'exec'),m.__dict__);modules[name]=m
  _cached=modules['case']
 return _cached

def manifest():
 c=original()
 return {'version':VERSION,'case_id':'ticket_revocation_recovery_v01','source_sha256':dict(PINS),'bridge_sha256':sha(Path(__file__).read_bytes()),'original_manifest':c.manifest(),'max_actions':14,'max_action_bytes':8192,'actual_provider_calls':0}
class Session:
 def __init__(self,expected_manifest_sha256,attempt_id):
  self.manifest=manifest();need(digest(self.manifest)==expected_manifest_sha256,'trusted_manifest');self.expected=expected_manifest_sha256;self.attempt_id=attempt_id;c=original();self._session=c.Session(c.digest(self.manifest['original_manifest']),attempt_id)
 def _check(self):need(same(manifest(),self.manifest),'session source drift')
 @property
 def terminal(self):return self._session.terminal
 @property
 def events(self):return deepcopy(self._session.events)
 def public_start(self):self._check();return self._session.public_start()
 def step(self,raw):self._check();return self._session.step(raw)
 def grade(self):self._check();return deepcopy(self._session.grade())
 def record(self):
  self._check();return {'version':VERSION,'manifest':deepcopy(self.manifest),'expected_manifest_sha256':self.expected,'attempt_id':self.attempt_id,'events':self.events,'terminal':self.terminal,'grade':self.grade(),'original_record':self._session.record(),'actual_provider_calls':0}
def audit(record,expected_manifest_sha256,expected_attempt_id):
 need(type(record)is dict and record.get('attempt_id')==expected_attempt_id and type(record.get('events'))is list and len(record['events'])<=14,'record binding');s=Session(expected_manifest_sha256,expected_attempt_id)
 for e in record['events']:
  need(type(e)is dict and type(e.get('raw'))is str,'event raw');s.step(e['raw'])
 c=original();c.audit(record['original_record'],c.digest(s.manifest['original_manifest']),expected_attempt_id)
 need(same(s.record(),record),'exact bridge replay');return {'verified':True,'passed':s.grade()['passed'],'actual_provider_calls':0}
