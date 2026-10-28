import os
import json, sqlite3, sys, tempfile, time
from pathlib import Path
from lumen.config import Settings
from lumen.omlx import OmlxTagger
from lumen.rawdevelop import render_raw, describe_histogram
S = Path(__file__).parent
SNAP = Path(os.environ["LUMEN_BENCH_SNAPSHOT"])  # a <library>/trash/<snapshot> folder
label, models = sys.argv[1], sys.argv[2:]
s = Settings.from_environment()
db = sqlite3.connect(f"file:{SNAP/'catalog.sqlite3'}?mode=ro", uri=True)
rows = dict(db.execute("select id, path from photos"))
tmp = Path(tempfile.mkdtemp())
out = []
for pid in [1, 9, 19, 5, 12, 17]:
    r = render_raw(Path(rows[pid]), tmp / f"{pid}.jpg", s.raw_developer_path, settings={}, long_edge=1024, image_format="jpeg", histogram=True)
    note = describe_histogram(r.histogram); item = {"file": Path(rows[pid]).name, "histogram": note}
    for m in models:
        t = time.monotonic()
        try: a = OmlxTagger(s.omlx_url, m).suggest_develop(r.path, settings={}, histogram_note=note); res = {"ok": True, "settings": a.settings, "rationale": a.rationale}
        except Exception as e: res = {"ok": False, "error": str(e)[:200], "raw": (getattr(e, "raw_response", None) or "")[:500]}
        item[m] = {**res, "seconds": round(time.monotonic() - t, 1)}
    out.append(item); print(pid, file=sys.stderr)
(S / "results" / f"develop_{label}.json").write_text(json.dumps(out, indent=1))
