import sys, os
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo')
sys.path.insert(0, '/home/mybuddy/workspace/mnemo-pg/repo/vendor/mnemosyne')
os.environ['HERMES_HOME'] = os.path.expanduser('~/.hermes/profiles/mike')
from mnemo_backends import vendor_bind, install
import numpy as np, json, inspect
import mnemosyne.core.embeddings as emb
names=[k for k in dir(emb) if 'embed' in k.lower() or 'encode' in k.lower()]
print('candidates:', names)
f=None
for n in ('embed_text','embed','get_embedding','encode','embed_query'):
    if hasattr(emb,n): f=getattr(emb,n); print('using',n); break
v = f('What animal has black and white stripes?')
a=np.asarray(v,dtype=np.float32)
print('json:', json.dumps(np.round(a,6).tolist(), separators=(',',':')))
