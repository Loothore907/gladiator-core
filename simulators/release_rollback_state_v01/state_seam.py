"""Exact extracted state evaluator/answer-shape seam; no original module imports."""
from copy import deepcopy

def state_reference(data):
 removed={m['retract'] for m in data['messages'] if m['role']=='owner' and 'retract'in m}
 state=deepcopy(data['initial']);evidence={k:'initial' for k in state}
 for m in data['messages']:
  if m['role']=='owner' and m['id'] not in removed:
   for k,v in m.get('set',{}).items():state[k]=v;evidence[k]=m['id']
 return dict(state=state,evidence=evidence)


def state_shape(d, answer):
    if type(answer) is not dict or set(answer) != {'state', 'evidence'}:
        return False
    fields = set(d['input']['initial'])
    return (type(answer['state']) is dict and type(answer['evidence']) is dict
            and set(answer['state']) == set(answer['evidence']) == fields
            and all((type(answer['state'][k]) is str or answer['state'][k] is None)
                    and type(answer['evidence'][k]) is str for k in fields))

