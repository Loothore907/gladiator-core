"""General offline JSON console; no real ticket services or automatic retry."""
import sys,json,argparse,re,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parent
def guard(event,args):
 if event.startswith(('socket.','subprocess.','os.exec','os.spawn'))or event in('os.system','_winapi.CreateProcess','ctypes.dlopen','ctypes.dlsym','ctypes.call_function'):raise PermissionError('offline_only')
def main():
 p=argparse.ArgumentParser();p.add_argument('--run-id',default='tickets_'+uuid.uuid4().hex[:12]);a=p.parse_args()
 if not re.fullmatch('[a-z0-9][a-z0-9_-]{0,47}',a.run_id)or a.run_id in {'con','prn','aux','nul'}|{f'com{i}'for i in range(1,10)}|{f'lpt{i}'for i in range(1,10)}:raise ValueError('safe_run_id')
 sys.addaudithook(guard)
 import case
 from json_contract import digest
 parent=ROOT/'runs';folder=parent/a.run_id
 if parent.resolve()!=parent or folder.resolve()!=folder:raise ValueError('fixed_output_path')
 parent.mkdir(exist_ok=True);folder.mkdir(exist_ok=False);s=case.Session(digest(case.manifest()),a.run_id)
 def emit(v):print(json.dumps(v),flush=True)
 emit({'type':'start','case':s.public_start()});reason='terminal'
 while s.terminal is None:
  line=sys.stdin.buffer.readline(8195)
  if not line:reason='input_closed';break
  raw=line[:-2]if line.endswith(b'\r\n')else line[:-1]if line.endswith(b'\n')else line
  try:
   if len(raw)>8192:raise ValueError('bytes')
   text=raw.decode('utf-8')
  except(ValueError,UnicodeError):reason='transport_rejected';emit({'type':'transport_error'});break
  emit(s.step(text))
 record=s.record();case.audit(record,s.expected,s.attempt_id)
 with(folder/'operator_record.json').open('x',encoding='utf-8')as f:json.dump({'console_termination':reason,'record':record},f,indent=2)
 emit({'type':'evaluation','termination':reason,'grade':s.grade(),'actual_provider_calls':0})
if __name__=='__main__':main()
