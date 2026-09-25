import base64
import hashlib
import os
from pathlib import Path
import tarfile

EXPECTED = "53b789674eaea092de2cb25317e66f81646d520640fa3314212f9cbfd0e7a0b4"
ROOT = Path(__file__).resolve().parent
PARTS = ROOT / "v23parts"
TARGET = Path("/tmp/edge-platform-v23")

# r00..r15 are the complete staged archive. Older part00/part01 files are
# retained only for audit history and must not be concatenated here.
parts = sorted(PARTS.glob("r*.txt"))
if not parts:
    raise SystemExit("Missing staged v23 r*.txt bundle parts")
encoded = "".join(p.read_text().strip() for p in parts)
blob = base64.b64decode(encoded)
actual = hashlib.sha256(blob).hexdigest()
if actual != EXPECTED:
    raise SystemExit(f"v23 staged bundle checksum mismatch: {actual}")

TARGET.mkdir(parents=True, exist_ok=True)
archive = Path("/tmp/edge-platform-v23.tgz")
archive.write_bytes(blob)
with tarfile.open(archive, "r:gz") as tf:
    tf.extractall(TARGET)

db_path = Path(os.getenv("EDGE_DB_PATH", str(TARGET / "edge.db")))
db_path.parent.mkdir(parents=True, exist_ok=True)
os.environ["EDGE_DB_PATH"] = str(db_path)

os.chdir(TARGET)
port = os.getenv("PORT", "8000")
os.execvp("uvicorn", ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", port])
