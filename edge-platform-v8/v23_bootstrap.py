import base64, io, os, pathlib, shutil, tarfile

ROOT = pathlib.Path(__file__).resolve().parent
PART_DIR = ROOT / 'v23parts'
NAMES = ['part00.txt','part01.txt'] + [f'r{i:02d}.txt' for i in range(16)]
PARTS = [PART_DIR / name for name in NAMES]
missing = [p.name for p in PARTS if not p.exists()]
if missing:
    raise SystemExit('Missing v23 bundle parts: ' + ','.join(missing))

encoded = ''.join(p.read_text().strip() for p in PARTS)
blob = base64.b64decode(encoded, validate=True)

runtime = ROOT / 'v23_runtime'
if runtime.exists():
    shutil.rmtree(runtime)
runtime.mkdir(parents=True)

required = {'app.py','phase21.py','phase22.py','ufc-live-card-v23.html','dashboard.html','requirements.txt'}
try:
    with tarfile.open(fileobj=io.BytesIO(blob), mode='r:gz') as t:
        names = {pathlib.PurePosixPath(n).name for n in t.getnames()}
        missing_payload = sorted(required - names)
        if missing_payload:
            raise SystemExit('v23 bundle missing required runtime files: ' + ','.join(missing_payload))
        t.extractall(runtime, filter='data')
except (tarfile.TarError, EOFError, OSError) as e:
    raise SystemExit(f'v23 bundle is not a valid complete gzip-tar: {e}')

for name in required:
    if not (runtime / name).exists():
        raise SystemExit(f'v23 extracted runtime missing {name}')

# Preserve the old phone entry URL without weakening the full app's auth middleware.
(runtime / 'cloud_entry_full.py').write_text('''from app import app as inner_app\n\nclass PhoneAlias:\n    def __init__(self, inner): self.inner = inner\n    async def __call__(self, scope, receive, send):\n        if scope.get("type") == "http" and scope.get("path") == "/mobile":\n            target = b"/login"\n            await send({"type":"http.response.start","status":307,"headers":[(b"location",target),(b"content-length",b"0")]})\n            await send({"type":"http.response.body","body":b""})\n            return\n        await self.inner(scope, receive, send)\n\napp = PhoneAlias(inner_app)\n''')

os.chdir(runtime)
os.environ.setdefault('EDGE_DB_PATH', '/data/edge.db')
port = os.getenv('PORT', '8000')
# Full v23 feature-parity runtime: never replace this with the lightweight cloud wrapper.
os.execvp('uvicorn', ['uvicorn','cloud_entry_full:app','--host','0.0.0.0','--port',port])
