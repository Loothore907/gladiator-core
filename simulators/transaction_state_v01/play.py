"""Play the optimistic-transaction state candidate with one JSON answer on stdin."""
import sys,json

def guard(event,args):
 if event.startswith(('socket.','subprocess.','os.exec','os.spawn'))or event in('os.system','_winapi.CreateProcess','ctypes.dlopen','ctypes.dlsym','ctypes.call_function'):raise PermissionError('offline_transaction_state_only')
def main():
 sys.addaudithook(guard)
 import case
 from json_contract import digest
 s=case.Session(digest(case.manifest()),'local_console');print(json.dumps({'type':'start','case':s.public_start()}),flush=True)
 line=sys.stdin.buffer.readline(8195)
 if not line:print(json.dumps({'type':'evaluation','terminal':'input_closed','grade':{'evaluated':False,'passed':False}}));return
 raw=line[:-2]if line.endswith(b'\r\n')else line[:-1]if line.endswith(b'\n')else line
 try:
  if len(raw)>8192:raise ValueError('bytes')
  text=raw.decode('utf-8')
 except(ValueError,UnicodeError):print(json.dumps({'type':'evaluation','terminal':'transport_rejected','grade':{'evaluated':False,'passed':False}}));return
 result=s.submit(text);case.audit(s.record(),s.trusted,s.attempt_id);print(json.dumps(result),flush=True)
if __name__=='__main__':main()
