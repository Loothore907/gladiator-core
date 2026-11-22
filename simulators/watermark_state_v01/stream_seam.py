"""Bounded fictional event-time aggregation reducer; no external streaming engine."""
from copy import deepcopy
from json_contract import exact,need
PARTS={'A','B'}
def ident(x):return type(x)is str and 1<=len(x)<=32 and x.isascii()and all(c.isalnum()or c in '_-'for c in x)
def integer(x,lo,hi):return type(x)is int and lo<=x<=hi
def reference(data):
 exact(data,('window_width','initial_watermarks','commands'));need(type(data['window_width'])is int and data['window_width']==5 and data['initial_watermarks']=={'A':0,'B':0}and all(type(v)is int for v in data['initial_watermarks'].values()),'initial');commands=data['commands'];need(type(commands)is list and len(commands)<=32,'commands')
 watermarks={'A':0,'B':0};accepted={'A':set(),'B':set()};windows={};closed=[];rejections=[];seen=set()
 for e in commands:
  need(type(e)is dict and e.get('op')in ('ingest','watermark'),'operation');op=e['op'];exact(e,('command','op','partition','sample_id','time','amount')if op=='ingest'else('command','op','partition','time'));need(ident(e['command'])and e['command']not in seen,'command_id');seen.add(e['command']);p=e['partition'];need(type(p)is str and p in PARTS and integer(e['time'],0,95 if op=='ingest'else 100),'partition_time')
  if op=='ingest':
   need(ident(e['sample_id'])and integer(e['amount'],1,100),'sample');sid=e['sample_id']
   reason='duplicate'if sid in accepted[p]else'late'if e['time']<watermarks[p]else None
   if reason:rejections.append({'command':e['command'],'reason':reason});continue
   accepted[p].add(sid);start=e['time']//5*5;key=(p,start)
   if key not in windows:windows[key]={'partition':p,'start':start,'end':start+5,'total':0,'count':0}
   windows[key]['total']+=e['amount'];windows[key]['count']+=1
  else:
   need(e['time']>=watermarks[p],'nondecreasing_watermark');watermarks[p]=e['time']
   for key in sorted(list(windows)):
    if key[0]==p and windows[key]['end']<=watermarks[p]:closed.append(windows.pop(key))
 return {'watermarks':watermarks,'closed':closed,'open':[windows[k]for k in sorted(windows)],'accepted_ids':{p:sorted(accepted[p])for p in sorted(PARTS)},'rejections':rejections}
def shape(answer):
 try:
  exact(answer,('watermarks','closed','open','accepted_ids','rejections'));exact(answer['watermarks'],PARTS);need(all(integer(v,0,100)for v in answer['watermarks'].values()),'watermarks');exact(answer['accepted_ids'],PARTS)
  for ids in answer['accepted_ids'].values():need(type(ids)is list and len(ids)<=32 and all(ident(x)for x in ids)and ids==sorted(set(ids)),'accepted_ids')
  for name in ('closed','open'):
   rows=answer[name];need(type(rows)is list and len(rows)<=32,'window_list')
   for v in rows:
    exact(v,('partition','start','end','total','count'));need(type(v['partition'])is str and v['partition']in PARTS and integer(v['start'],0,95)and v['start']%5==0 and integer(v['end'],5,100)and v['end']==v['start']+5 and integer(v['total'],1,3200)and integer(v['count'],1,32),'window_row')
  rs=answer['rejections'];need(type(rs)is list and len(rs)<=32,'rejections')
  for v in rs:exact(v,('command','reason'));need(ident(v['command'])and v['reason']in ('duplicate','late'),'rejection')
  return True
 except(ValueError,TypeError):return False
