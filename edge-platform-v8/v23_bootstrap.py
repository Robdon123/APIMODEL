import base64, io, os, pathlib, shutil, tarfile

ROOT = pathlib.Path(__file__).resolve().parent
PART_DIR = ROOT / 'v23parts'
NAMES = ['part00.txt','part01.txt'] + [f'r{i:02d}.txt' for i in range(16)]
PARTS = [PART_DIR / name for name in NAMES]
missing = [p.name for p in PARTS if not p.exists()]
if missing:
    raise SystemExit('Missing v23 bundle parts: ' + ','.join(missing))

mobile_html = ROOT / 'edge-platform-full-mobile.html'
if not mobile_html.exists():
    raise SystemExit('Missing full mobile HTML')

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
mobile_target = runtime / 'edge-platform-full-mobile.html'
shutil.copy2(mobile_html, mobile_target)
# Backward-compatible UI shape fix for bookie-check responses.
mobile_text = mobile_target.read_text()
mobile_text = mobile_text.replace("const rows=Array.isArray(b)?b:(b.rows||b.quotes||b.results||[]);", "const rows=Array.isArray(b)?b:(b.board||b.rows||b.quotes||b.results||[]);")
mobile_target.write_text(mobile_text)

# Preserve the phone entry URL, serve the complete standalone UI before auth middleware,
# and fail closed if a future release drops core model/provider routes.
(runtime / 'cloud_entry_full.py').write_text('''from pathlib import Path\nfrom app import app as inner_app\n\nREQUIRED_PATHS = {\n    "/health", "/backend/doctor", "/backend/status", "/ufc", "/login",\n    "/mma/upcoming-card/current", "/mma/upcoming-model-v21", "/mma/prop-markets",\n    "/mma/prop-evaluate-live", "/mma/best-price", "/mma/bookie-check",\n    "/providers/mma/status", "/providers/mma/sync-all", "/mma/model-readiness",\n    "/mma/profitability-backtest", "/mma/profitability-gate", "/mma/value-board",\n    "/football/prop-model", "/football/prop-scan", "/football/value-board",\n    "/research/gate", "/research/drift", "/research/open-source-engines",\n    "/data/statsbomb/open/competitions", "/data/mma/ufcstats/status",\n    "/platform/status", "/cloud/status", "/cloud/maintenance"\n}\npaths = {getattr(r, "path", "") for r in inner_app.routes}\nmissing = sorted(REQUIRED_PATHS - paths)\nif missing or len(paths) < 130:\n    raise RuntimeError(f"FEATURE_PARITY_GATE_FAILED missing={missing} route_count={len(paths)}")\n\nMOBILE = Path(__file__).with_name("edge-platform-full-mobile.html")\nclass PhoneAlias:\n    def __init__(self, inner): self.inner = inner\n    async def __call__(self, scope, receive, send):\n        if scope.get("type") == "http" and scope.get("path") in ("/mobile", "/app"):\n            body = MOBILE.read_bytes()\n            await send({"type":"http.response.start","status":200,"headers":[(b"content-type",b"text/html; charset=utf-8"),(b"cache-control",b"no-store"),(b"content-length",str(len(body)).encode())]})\n            await send({"type":"http.response.body","body":body})\n            return\n        await self.inner(scope, receive, send)\n\napp = PhoneAlias(inner_app)\n''')

os.chdir(runtime)
os.environ.setdefault('EDGE_DB_PATH', '/data/edge.db')
port = os.getenv('PORT', '8000')
os.execvp('uvicorn', ['uvicorn','cloud_entry_full:app','--host','0.0.0.0','--port',port])
