"""Exact bounded JSON helper bodies reused from reviewed coding session."""
import json,hashlib

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

