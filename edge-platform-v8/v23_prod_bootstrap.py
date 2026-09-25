import base64, hashlib, io, os, pathlib, shutil, sys, tarfile

ROOT = pathlib.Path(__file__).resolve().parent
PART_DIR = ROOT / "v23prod16"
EXPECTED_PARTS = 10
EXPECTED_SHA256 = "508c084e9de5a043c0d4bf239ca266e964890c93fb46a6e92fd4da82934bb38c"
RUNTIME = ROOT / "v23runtime"

parts = [PART_DIR / f"part{i:02d}.txt" for i in range(EXPECTED_PARTS)]
missing = [str(p) for p in parts if not p.exists()]
if missing:
    raise RuntimeError(f"Missing v23 bundle parts: {missing}")
encoded = "".join(p.read_text().strip() for p in parts)
raw = base64.b64decode(encoded, validate=True)
actual = hashlib.sha256(raw).hexdigest()
if actual != EXPECTED_SHA256:
    raise RuntimeError(f"v23 bundle SHA256 mismatch: expected {EXPECTED_SHA256}, got {actual}")
if RUNTIME.exists():
    shutil.rmtree(RUNTIME)
RUNTIME.mkdir(parents=True)
with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
    try:
        tf.extractall(RUNTIME, filter="data")
    except TypeError:
        tf.extractall(RUNTIME)
pathlib.Path("/data").mkdir(parents=True, exist_ok=True)
os.chdir(RUNTIME)
os.execvp(sys.executable, [sys.executable, "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", os.getenv("PORT", "8000")])
