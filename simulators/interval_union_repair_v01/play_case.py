"""Local public JSON action console; no provider transport or arbitrary code."""
import sys,json,argparse,re,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parent
def guard(event,args):
 if event.startswith(('socket.','subprocess.','os.exec','os.spawn'))or event in('os.system','_winapi.CreateProcess','ctypes.dlopen','ctypes.dlsym','ctypes.call_function'):raise PermissionError('offline_case_only')
def main():
 p=argparse.ArgumentParser();p.add_argument('--run-id',default='case_'+uuid.uuid4().hex[:12]);a=p.parse_args()
 if not re.fullmatch('[a-z0-9][a-z0-9_-]{0,47}',a.run_id)or a.run_id in {'con','prn','aux','nul'}|{f'com{i}'for i in range(1,10)}|{f'lpt{i}'for i in range(1,10)}:raise ValueError('safe_run_id')
 sys.addaudithook(guard)
 import coding_session as c
 parent=ROOT/'runs';folder=parent/a.run_id
 if parent.resolve()!=parent or folder.resolve()!=folder:raise ValueError('fixed_output_path')
 parent.mkdir(exist_ok=True);folder.mkdir(exist_ok=False);s=c.Session('candidate',{'scope':'offline_local_console','attempt_id':a.run_id});transcript=[]
 def emit(v):print(json.dumps(v),flush=True)
 emit({'type':'start','public_delivery':c.public_delivery(),'actions':{'read':['op','path'],'write':['op','path','content'],'test':['op'],'finish':['op']},'limits':c.definition()['limits'],'scope':'Offline SQL repair candidate. Same-user filesystem is not isolated. No provider calls.'})
 termination='terminal'
 while s.reason is None:
  line=sys.stdin.buffer.readline(32771)
  if not line:termination='input_closed';break
  raw=line[:-2]if line.endswith(b'\r\n')else line[:-1]if line.endswith(b'\n')else line
  if len(raw)>32768:termination='transport_rejected';emit({'type':'transport_error'});break
  try:action=raw.decode('utf-8')
  except UnicodeError:termination='transport_rejected';emit({'type':'transport_error'});break
  result=s.step(action);transcript.append({'raw':action,'public_feedback':result});emit({'type':'observation','result':result,'terminal':s.reason})
 record=s.record();c.audit_record('candidate',record,s.binding)
 with(folder/'operator_record.json').open('x',encoding='utf-8')as f:json.dump({'console_termination':termination,'record':record},f,indent=2)
 emit({'type':'evaluation','termination':termination,'terminal':record['terminal'],'grade':record['task_grade'],'actual_provider_calls':0})
if __name__=='__main__':main()
