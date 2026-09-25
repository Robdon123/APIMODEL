import base64, gzip, io, os, pathlib, shutil, tarfile, zipfile

ROOT = pathlib.Path(__file__).resolve().parent
PARTS = sorted((ROOT / 'v23parts').glob('part*.txt'))
if not PARTS:
    raise SystemExit('No v23parts/part*.txt files found')
encoded = ''.join(p.read_text().strip() for p in PARTS)
blob = base64.b64decode(encoded)
# Accept gzip-wrapped tar/zip or a direct tar/zip payload.
try:
    raw = gzip.decompress(blob) if blob[:2] == b'\x1f\x8b' else blob
except Exception:
    raw = blob
runtime = ROOT / 'v23_runtime'
if runtime.exists():
    shutil.rmtree(runtime)
runtime.mkdir(parents=True)
extracted = False
if raw[:2] == b'PK':
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        z.extractall(runtime)
    extracted = True
if not extracted:
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:*') as t:
            t.extractall(runtime)
        extracted = True
    except tarfile.TarError:
        pass
if not extracted:
    # Some staged bundles are gzip-compressed tar data; try original bytes directly.
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode='r:*') as t:
            t.extractall(runtime)
        extracted = True
    except tarfile.TarError:
        pass
if not extracted:
    raise SystemExit('v23 staged bundle could not be decoded as zip/tar/gzip-tar')
apps = list(runtime.rglob('app.py'))
if not apps:
    raise SystemExit('Decoded v23 bundle contains no app.py')
app_dir = apps[0].parent
os.chdir(app_dir)
os.environ.setdefault('EDGE_DB_PATH', str(app_dir / 'edge.db'))
port = os.getenv('PORT', '8000')
os.execvp('uvicorn', ['uvicorn','app:app','--host','0.0.0.0','--port',port])
