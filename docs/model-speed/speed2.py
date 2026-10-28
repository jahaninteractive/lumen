import os
import base64, io, json, sys, time
from pathlib import Path
from PIL import Image
from lumen.config import Settings
from lumen.omlx import OmlxTagger
from lumen.vocabulary import load_vocabulary
s = Settings.from_environment(); vocab = load_vocabulary(s)
SNAP = Path(os.environ["LUMEN_BENCH_SNAPSHOT"])  # a <library>/trash/<snapshot> folder
previews = sorted((SNAP / "files/previews").glob("*.jpg"))
class Tagger(OmlxTagger):
    edge, compact = 1616, False
    def _image_content(self, path):
        image = Image.open(path); image.thumbnail((self.edge, self.edge)); buf = io.BytesIO(); image.convert("RGB").save(buf, "JPEG", quality=90)
        return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}}
    def _complete(self, content, timeout, **kw):
        if self.compact: content[0]["text"] += " Write the JSON compact on one line, no code fence."
        return super()._complete(content, timeout, **kw)
out = []
configs = [(m, e, c) for m in sys.argv[1:] for (e, c) in [(1616, False), (1024, True), (768, True)]]
for m, e, c in configs:
    for p in previews[:4] if e == 1616 else previews[4:8] if e == 1024 else previews[8:12]:
        t = Tagger(s.omlx_url, m, role="fast"); t.edge, t.compact = e, c
        start = time.monotonic()
        try: r = t.tag(p, vocabulary=vocab); res = {"description": r.description, "subjects": r.subjects, "species": list(r.species), "technical": r.technical}
        except Exception as x: res = {"error": str(x)[:120]}
        out.append({"model": m, "edge": e, "compact": c, "file": p.name[:10], "seconds": round(time.monotonic() - start, 1), **res})
        print(m[:10], e, c, out[-1]["seconds"], res.get("error", ""), flush=True)
(Path(__file__).parent / "results" / "speed2.json").write_text(json.dumps(out, indent=1))
