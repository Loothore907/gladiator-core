"""Fixed in-memory SQLite SELECT boundary. Never runs model Python or shell."""
import sqlite3,time,json

SQLITE_VERSION=sqlite3.sqlite_version
COLUMNS=('edge_id','tenant','source','target','status')
FUNCTIONS=frozenset(('sum','lag','row_number','max','min','count','coalesce','ifnull','nullif','typeof','length','lower','upper','abs'))

def execute(sql,rows):
    if type(sql)is not str or not 0<len(sql.encode('utf-8'))<=8192 or '\x00'in sql:
        return {'ok':False,'error':'sql_size_or_encoding','policy_violation':False}
    if type(rows)is not list or len(rows)>16:raise ValueError('Fixed bounded fixture rows')
    for r in rows:
        if type(r)is not list or len(r)!=5:raise ValueError('Fixture shape')
        if type(r[0])is not int or not all(type(r[i])is str and 0<len(r[i])<=16 and all(32<=ord(c)<=126 for c in r[i]) for i in (1,2,3,4)):raise ValueError('Fixture types')
        if not 0<=r[0]<=1000 or r[4]not in ('active','inactive'):raise ValueError('Fixture values')
    if len({r[0]for r in rows})!=len(rows):raise ValueError('Unique edge IDs')
    if len({(r[1],n)for r in rows for n in r[2:4]})>6:raise ValueError('Six tenant-node endpoint bound')
    denied=[];ticks=0;started=time.monotonic()
    def authorize(action,a,b,db,source):
        if action in (sqlite3.SQLITE_SELECT,sqlite3.SQLITE_RECURSIVE):return sqlite3.SQLITE_OK
        if action==sqlite3.SQLITE_READ and db=='main' and a=='edges' and b in COLUMNS+('',):return sqlite3.SQLITE_OK
        if action==sqlite3.SQLITE_FUNCTION and type(b)is str and b.lower()in FUNCTIONS:return sqlite3.SQLITE_OK
        denied.append([action,a,b]);return sqlite3.SQLITE_DENY
    def progress():
        nonlocal ticks
        ticks+=100
        return int(ticks>50000 or time.monotonic()-started>=.25)
    con=sqlite3.connect(':memory:')
    try:
        con.enable_load_extension(False)
        con.execute('PRAGMA temp_store=MEMORY')
        con.execute('CREATE TABLE edges(edge_id INTEGER PRIMARY KEY,tenant TEXT NOT NULL,source TEXT NOT NULL,target TEXT NOT NULL,status TEXT NOT NULL)')
        con.executemany('INSERT INTO edges VALUES(?,?,?,?,?)',rows)
        con.commit();con.execute('PRAGMA query_only=ON')
        for limit,value in ((sqlite3.SQLITE_LIMIT_LENGTH,65536),(sqlite3.SQLITE_LIMIT_SQL_LENGTH,8192),(sqlite3.SQLITE_LIMIT_COLUMN,16),(sqlite3.SQLITE_LIMIT_EXPR_DEPTH,32),(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT,8),(sqlite3.SQLITE_LIMIT_VDBE_OP,10000),(sqlite3.SQLITE_LIMIT_WORKER_THREADS,0)):
            con.setlimit(limit,value)
        con.set_authorizer(authorize);con.set_progress_handler(progress,100)
        try:
            cursor=con.execute(sql)
            columns=[c[0]for c in cursor.description]if cursor.description else []
            if any(type(c)is not str or len(c.encode())>128 for c in columns):return {'ok':False,'error':'sql_result_bound','policy_violation':False}
            total_bytes=len(json.dumps(columns,ensure_ascii=False).encode())
            result=cursor.fetchmany(33)
            if len(result)>32:return {'ok':False,'error':'sql_result_bound','policy_violation':False}
            for row in result:
                for value in row:
                    if not(value is None or type(value)is int and abs(value)<2**53 or type(value)is str and len(value.encode())<=4096):
                        return {'ok':False,'error':'sql_result_type','policy_violation':False}
                total_bytes+=len(json.dumps(list(row),ensure_ascii=False).encode())
                if total_bytes>4096:return {'ok':False,'error':'sql_result_bound','policy_violation':False}
            return {'ok':True,'columns':columns,'rows':[list(r)for r in result],'policy_violation':False}
        except(sqlite3.Error,MemoryError,OverflowError,ValueError)as error:
            if isinstance(error,sqlite3.Error)and str(error).startswith('no such function:'):
                denied.append(['unsupported_function',None,None])
            return {'ok':False,'error':'sql_policy_denied'if denied else 'sql_execution_error','policy_violation':bool(denied)}
    finally:con.close()

def typed_equal(a,b):
    if type(a)is not type(b):return False
    if type(a)is list:return len(a)==len(b)and all(typed_equal(x,y)for x,y in zip(a,b))
    if type(a)is dict:return set(a)==set(b)and all(typed_equal(a[k],b[k])for k in a)
    return a==b

def check(sql,cases,public=False):
    reports=[];passed=0;policy=False
    for c in cases:
        result=execute(sql,c['rows']);ok=result.get('ok')is True and result.get('columns')==['tenant','source','target'] and typed_equal(result.get('rows'),c['expected'])
        passed+=ok;policy|=result['policy_violation']
        r={'id':c['id'],'passed':ok}
        if public:r.update(actual=result,expected=c['expected'])
        reports.append(r)
    return {'passed':passed,'failed':len(cases)-passed,'policy_violation':policy,'cases':reports}
