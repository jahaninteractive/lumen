"""Tag fresh frames through the unmodified OmlxTagger to confirm the in-tree image size. Usage: live_check.py <model>..."""
import json, os, sys, time
from pathlib import Path
from lumen.config import Settings
from lumen.omlx import OmlxTagger
from lumen.vocabulary import load_vocabulary
s = Settings.from_environment(); vocab = load_vocabulary(s)
previews = sorted((Path(os.environ["LUMEN_BENCH_SNAPSHOT"]) / "files/previews").glob("*.jpg"))
start_at = int(os.environ.get("LUMEN_BENCH_OFFSET", "100")); out = []
for i, m in enumerate(sys.argv[1:]):
    for p in previews[start_at + 3 * i: start_at + 3 * i + 3]:
        t0 = time.monotonic()
        try: r = OmlxTagger(s.omlx_url, m, role="fast").tag(p, vocabulary=vocab); res = {"description": r.description, "subjects": r.subjects, "species": list(r.species)}
        except Exception as x: res = {"error": str(x)[:160]}
        out.append({"model": m, "file": p.name[:10], "seconds": round(time.monotonic() - t0, 1), **res}); print(json.dumps(out[-1]), flush=True)
(Path(__file__).parent / "results" / "live-check-1024.json").write_text(json.dumps(out, indent=1))
