"""Immutable fixture snapshot for fixed bounded kit SQL repair."""
from pathlib import Path
from copy import deepcopy
import hashlib,json
import sql_executor as sql
from json_contract import strict,digest,need
ROOT=Path(__file__).resolve().parent
class Oracle:
 def __init__(self):
  need(Path(sql.__file__).resolve().parent==ROOT,'executor_origin');freeze=strict((ROOT/'INPUT_FREEZE.json').read_text());need(set(freeze['files'])=={'PUBLIC_INPUT.json','PRIVATE_REFERENCE.json'},'fixed_input_names');self.snapshot={}
  for name,h in freeze['files'].items():
   raw=(ROOT/name).read_bytes();need(hashlib.sha256(raw).hexdigest()==h,'input_drift');self.snapshot[name]=strict(raw.decode('utf-8'))
  self.sha=digest(self.snapshot)
 def inputs(self):return deepcopy(self.snapshot['PUBLIC_INPUT.json'])
 def evaluate(self,query):return sql.check(query,self.snapshot['PRIVATE_REFERENCE.json']['cases'])
 def test(self,query):return sql.check(query,json.loads(self.snapshot['PUBLIC_INPUT.json']['repository']['tests/public_cases.json']),public=True)
