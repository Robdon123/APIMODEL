import base64, hashlib, io, os, pathlib, shutil, tarfile

ROOT = pathlib.Path(__file__).resolve().parent
PART_DIR = ROOT / 'v23parts'
PARTS = sorted(PART_DIR.glob('part*.txt')) + sorted(PART_DIR.glob('r*.txt'))
if not PARTS:
    raise SystemExit('No v23 bundle parts found')

encoded = ''.join(p.read_text().strip() for p in PARTS)
blob = base64.b64decode(encoded, validate=True)
expected = '53b789674eaea092de2cb25317e66f81646d520640fa3314212f9cbfd0e7a0b4'
actual = hashlib.sha256(blob).hexdigest()
if actual != expected:
    raise SystemExit(f'v23 bundle integrity check failed: {actual}')

runtime = ROOT / 'v23_runtime'
if runtime.exists():
    shutil.rmtree(runtime)
runtime.mkdir(parents=True)
with tarfile.open(fileobj=io.BytesIO(blob), mode='r:gz') as t:
    t.extractall(runtime, filter='data')

app_file = runtime / 'app.py'
if not app_file.exists():
    raise SystemExit('Decoded v23 bundle contains no app.py')

os.chdir(runtime)
os.environ.setdefault('EDGE_DB_PATH', '/data/edge.db')
port = os.getenv('PORT', '8000')
# Full v23 feature-parity runtime: do not replace this with the lightweight cloud wrapper.
os.execvp('uvicorn', ['uvicorn','app:app','--host','0.0.0.0','--port',port])
