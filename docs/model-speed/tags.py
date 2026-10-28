import os
import json, sqlite3, time
from pathlib import Path
from lumen.config import Settings
from lumen.omlx import OmlxTagger
from lumen.vocabulary import load_vocabulary
SNAP = Path(os.environ["LUMEN_BENCH_SNAPSHOT"])  # a <library>/trash/<snapshot> folder
s = Settings.from_environment(); vocab = load_vocabulary(s)
db = sqlite3.connect(f"file:{SNAP/'catalog.sqlite3'}?mode=ro", uri=True)
rows = {i: (Path(p).name, SNAP / "files/previews" / Path(pv).name) for i, p, pv in db.execute("select id, path, preview_path from photos")}
out = []
for pid in [2, 4, 8, 11, 13, 15, 18, 20]:
    name, preview = rows[pid]; item = {"file": name}
    for m in ["Qwen3-VL-8B-Instruct-4bit", "Qwen3.8-27B-4bit"]:
        t = time.monotonic()
        try:
            r = OmlxTagger(s.omlx_url, m, role="fast").tag(preview, vocabulary=vocab)
            item[m] = {"seconds": round(time.monotonic() - t, 1), "description": r.description, "subjects": r.subjects, "scene": r.scene,
                       "technical": r.technical, "species": list(r.species), "conditions": list(r.conditions), "confidence": r.confidence}
        except Exception as e:
            item[m] = {"seconds": round(time.monotonic() - t, 1), "error": str(e)[:200]}
    out.append(item); print(pid, flush=True)
(Path(__file__).parent / "results" / "tags.json").write_text(json.dumps(out, indent=1))
