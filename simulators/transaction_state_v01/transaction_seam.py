"""Bounded synthetic optimistic-transaction reducer and answer schema."""
from copy import deepcopy
from json_contract import exact,need
FIELDS={'release','region','note'}
def patch_shape(p):return type(p)is dict and set(p)<=FIELDS and all(v is None or type(v)is str and len(v)<=64 for v in p.values())
def reference(data):
 exact(data,('initial_revision','initial','events'));need(type(data['initial_revision'])is int and 0<=data['initial_revision']<=100,'initial_revision');need(patch_shape(data['initial'])and set(data['initial'])==FIELDS,'initial_state')
 need(type(data['events'])is list and len(data['events'])<=32,'event_bound')
 state=deepcopy(data['initial']);rev=data['initial_revision'];evidence={k:'initial'for k in state};pending={};results=[];ids=set();txs=set()
 for e in data['events']:
  need(type(e)is dict and e.get('op')in ('begin','stage','commit','rebase','abort'),'operation');op=e['op'];exact(e,('id','op','tx','patch')if op=='stage'else('id','op','tx'))
  need(type(e['id'])is str and 1<=len(e['id'])<=32 and e['id']not in ids,'event_id');ids.add(e['id']);t=e['tx'];need(type(t)is str and 1<=len(t)<=32,'transaction_id')
  if op=='begin':
   need(t not in txs,'unique_tx');txs.add(t);pending[t]={'base_revision':rev,'patch':{}}
  else:
   need(t in pending,'open_transaction')
   if op=='stage':need(patch_shape(e['patch']),'patch');pending[t]['patch'].update(deepcopy(e['patch']))
   elif op=='rebase':pending[t]['base_revision']=rev
   elif op=='abort':del pending[t]
   elif pending[t]['base_revision']!=rev:results.append({'event':e['id'],'result':'conflict','revision':rev})
   else:
    patch=pending.pop(t)['patch'];state.update(patch);rev+=1
    for k in patch:evidence[k]=e['id']
    results.append({'event':e['id'],'result':'committed','revision':rev})
 return {'state':state,'revision':rev,'evidence':evidence,'pending':pending,'commit_results':results}

def shape(answer):
 try:
  exact(answer,('state','revision','evidence','pending','commit_results'))
  need(patch_shape(answer['state'])and set(answer['state'])==FIELDS,'state')
  need(type(answer['revision'])is int and 0<=answer['revision']<=132,'revision')
  e=answer['evidence'];need(type(e)is dict and set(e)==FIELDS and all(type(v)is str and 1<=len(v)<=32 for v in e.values()),'evidence')
  p=answer['pending'];need(type(p)is dict and len(p)<=32,'pending')
  for k,v in p.items():
   need(type(k)is str and 1<=len(k)<=32,'pending_id');exact(v,('base_revision','patch'));need(type(v['base_revision'])is int and 0<=v['base_revision']<=132 and patch_shape(v['patch']),'pending_value')
  cr=answer['commit_results'];need(type(cr)is list and len(cr)<=32,'results')
  for v in cr:
   exact(v,('event','result','revision'));need(type(v['event'])is str and 1<=len(v['event'])<=32 and v['result']in ('committed','conflict')and type(v['revision'])is int and 0<=v['revision']<=132,'result_fields')
  return True
 except(ValueError,TypeError):return False
