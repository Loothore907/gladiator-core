"""One sealed SQL repository task; no generic code, callback or import API."""
import json,hashlib
from pathlib import Path
from copy import deepcopy
import oracle

HERE=Path(__file__).resolve().parent
KEY='dependency-reachability-repair'
RECORD='dependency-reachability-coding-record-0.1.0'
PROTOCOL='phase-sql-coding-tools-0.1.0'
SCORING='dependency-reachability-grading-0.1.0'
STATES=('completed','refused','content_filtered','incomplete_output_limit','provider_error','provider_unknown')

def need(ok,message):
    if not ok:raise ValueError('Coding: '+message)
def plain(x):
    def visit(v,depth=0):
        need(depth<=32,'depth')
        if v is None or type(v)is bool:return
        if type(v)is int:need(abs(v)<2**53,'integer');return
        if type(v)is str:need(len(v.encode())<=65536,'string');return
        if type(v)is list:
            need(len(v)<=2000,'list')
            for y in v:visit(y,depth+1)
            return
        if type(v)is dict:
            need(len(v)<=100,'object')
            for k,y in v.items():need(type(k)is str,'key');visit(y,depth+1)
            return
        raise ValueError('Plain builtin JSON only')
    visit(x)
def exact(x,keys):need(type(x)is dict and set(x)==set(keys),'exact action fields')
def canonical(x):plain(x);return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
def digest(x):return hashlib.sha256(canonical(x)).hexdigest()
def same(a,b):return canonical(a)==canonical(b)
def strict(raw):
    need(type(raw)is str and len(raw.encode())<=32768,'bounded raw JSON')
    depth=0;quoted=False;escaped=False
    for c in raw:
        if quoted:
            if escaped:escaped=False
            elif c=='\\':escaped=True
            elif c=='"':quoted=False
        elif c=='"':quoted=True
        elif c in '[{':depth+=1;need(depth<=32,'raw depth')
        elif c in ']}':depth-=1
    def pairs(items):
        d={}
        for k,v in items:need(k not in d,'duplicate key');d[k]=v
        return d
    def bad(_):raise ValueError('Nonfinite JSON')
    x=json.loads(raw,object_pairs_hook=pairs,parse_constant=bad);plain(x);return x
def data(n):return strict((HERE/n).read_text(encoding='utf-8-sig'))
def inputs():return oracle.Oracle().inputs()

def public_delivery():
    p=inputs();return {'instructions':p['protocol'],'initial_observation':deepcopy(p['initial_observation'])}
def definition(o=None):
    o=oracle.Oracle()if o is None else o;p=o.inputs()
    return {'node_id':p['task_id'],'mode':'interactive','category':'bounded_sql_coding','definition_version':'dependency-reachability-coding-0.1.0','task_version':'dependency-reachability-0.1.0','scoring_version':SCORING,'trusted_adapter':'fixed-sqlite-query-repository-v1','instructions':p['protocol'],'input':{'repository_sha256':digest(p['repository']),'snapshot_sha256':o.sha},'source_sha256':{n:hashlib.sha256((HERE/n).read_bytes()).hexdigest()for n in('session.py','oracle.py','sql_executor.py','json_contract.py','play.py')},'initial_observation':p['initial_observation'],'limits':{'max_actions':10,'max_elapsed_ms':1000,'max_output_tokens':1024}}

class Session:
    def __init__(self,label,binding=None):
        need(type(label)is str and label in ('current','candidate'),'fixed entrant')
        self.label=label;self.binding=deepcopy(binding)if binding is not None else {'scope':'offline-candidate-review','entrant':label};plain(self.binding)
        self.oracle=oracle.Oracle();self.definition=definition(self.oracle);self.public=self.oracle.inputs()
        self.repo=deepcopy(self.public['repository']);self.events=[];self.reason=None;self.elapsed=0
        self.invalid=0;self.prohibited=0;self.writes=[];self.grade={'evaluated':False,'passed':False}
    def step(self,raw,elapsed_ms=0,provider_state='completed'):
        need(self.reason is None,'closed session');need(type(elapsed_ms)is int and elapsed_ms>=0,'elapsed integer');need(type(provider_state)is str and provider_state in STATES,'provider state')
        need(type(raw)is str and len(raw.encode())<=32768,'bounded raw action')
        before={'query_sha256':digest(self.repo['query.sql']),'writes':len(self.writes)};self.elapsed+=elapsed_ms;valid=policy=True
        if provider_state!='completed':self.reason=provider_state;result={'error':provider_state}
        elif self.elapsed>1000:self.reason='time_limit';result={'error':'logical_time_limit'}
        else:
            try:
                a=strict(raw);need(type(a)is dict and type(a.get('op'))is str,'action operation');op=a['op']
                need(op in ('read','write','test','finish'),'fixed operation')
                exact(a,{'read':('op','path'),'write':('op','path','content'),'test':('op',),'finish':('op',)}[op])
                if op in ('read','write'):need(type(a['path'])is str,'path type')
                if op=='write':need(type(a['content'])is str and 0<len(a['content'].encode())<=8192 and '\x00'not in a['content'],'bounded SQL content')
            except(ValueError,TypeError,KeyError,RecursionError):
                self.invalid+=1;valid=False;result={'error':'invalid_action'}
            else:
                if op in ('read','write')and(a['path']not in self.repo or op=='write'and a['path']!='query.sql'):
                    self.prohibited+=1;policy=False;result={'error':'unauthorized_file'}
                elif op=='read':result={'path':a['path'],'content':self.repo[a['path']]}
                elif op=='write':
                    self.repo['query.sql']=a['content'];self.writes.append(a['content']);result={'written':'query.sql','sha256':digest(a['content'])}
                elif op=='test':
                    result=self.oracle.test(self.repo['query.sql'])
                    if result['policy_violation']:self.prohibited+=1;policy=False
                else:
                    self.reason='finished';r=self.oracle.evaluate(self.repo['query.sql'])
                    if r['policy_violation']:self.prohibited+=1;policy=False
                    self.grade={'evaluated':True,'passed':r['failed']==0 and self.invalid==0 and self.prohibited==0,'code_correct':r['failed']==0,'interface_correct':self.invalid==0,'policy_correct':self.prohibited==0,'tests_passed':r['passed'],'tests_failed':r['failed']}
                    result={'finished':True}
        self.events.append({'step':len(self.events)+1,'action':raw,'elapsed_ms':elapsed_ms,'provider_state':provider_state,'before':before,'after':{'query_sha256':digest(self.repo['query.sql']),'writes':len(self.writes)},'result':deepcopy(result),'interface_valid':valid,'policy_ok':policy,'applied':True})
        if self.reason is None and len(self.events)==10:self.reason='step_limit'
        return deepcopy(result)
    def record(self):
        first=self.oracle.evaluate(self.writes[0])if self.writes else None
        outcome='active'if self.reason is None else 'interface_invalid'if self.invalid else 'policy_violation'if self.prohibited else 'completed_correct'if self.grade['passed']else 'completed_wrong'if self.reason=='finished'else self.reason
        return {'record_version':RECORD,'protocol_version':PROTOCOL,'scoring_version':SCORING,'definition_sha256':digest(self.definition),'entrant':self.label,'session_binding':deepcopy(self.binding),'events':deepcopy(self.events),'terminal':{'reason':self.reason,'outcome':outcome},'task_grade':deepcopy(self.grade),'resources':{'actions':len(self.events),'elapsed_ms':self.elapsed,'provider_calls_made_by_component':0},'first_write':None if first is None else {'query':self.writes[0],'sha256':digest(self.writes[0]),'code_correct':first['failed']==0,'tests_passed':first['passed'],'tests_failed':first['failed'],'policy_violation':first['policy_violation']},'final_query':self.repo['query.sql'],'writes':len(self.writes),'invalid_actions':self.invalid,'prohibited_attempts':self.prohibited}

def audit_record(label,record,binding=None):
    plain(record);need(type(record)is dict and record.get('entrant')==label,'record entrant')
    need(type(record.get('events'))is list and len(record['events'])<=10,'bounded events')
    s=Session(label,binding)
    for e in record['events']:
        exact(e,('step','action','elapsed_ms','provider_state','before','after','result','interface_valid','policy_ok','applied'))
        s.step(e['action'],e['elapsed_ms'],e['provider_state'])
    need(same(s.record(),record),'exact coding replay')
    return {'replay_verified':True,'outcome':record['terminal']['outcome'],'semantic_evaluated':record['task_grade']['evaluated'],'semantic_passed':record['task_grade']['passed'],'model_observation':False}
