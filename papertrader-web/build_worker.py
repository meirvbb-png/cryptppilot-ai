"""Builds worker.js = FILES + strategies.js + core.js + worker_main.js (single ES module)."""
import json, pathlib
d = pathlib.Path(__file__).parent
files = {"index.html": (d.parent / "papertrader/static/index.html").read_text(), "lw.js": (d / "lw.js").read_text()}
out = "const FILES = " + json.dumps(files) + ";\n" + (d / "strategies.js").read_text() + "\n" + (d / "core.js").read_text() + "\n" + (d / "worker_main.js").read_text()
(d / "worker.js").write_text(out)
print("worker.js", len(out) // 1024, "KB")
