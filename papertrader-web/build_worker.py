"""Builds worker.js (single file) embedding index.html, local.js, lw.js."""
import json, pathlib
d = pathlib.Path(__file__).parent
files = {n: (d / n).read_text() for n in ("index.html", "local.js", "lw.js")}
out = (d / "worker_src.js").read_text().replace("__FILES__", json.dumps(files))
(d / "worker.js").write_text(out)
print("worker.js", len(out) // 1024, "KB")
