"""Tag fresh frames with and without a JSON-schema response_format, through the real tagger. Usage: schema.py <model>..."""
import json, os, sys, time
from pathlib import Path
from lumen.config import Settings
from lumen.omlx import OmlxTagger
from lumen.vocabulary import load_vocabulary
s = Settings.from_environment(); vocab = load_vocabulary(s)
previews = sorted((Path(os.environ["LUMEN_BENCH_SNAPSHOT"]) / "files/previews").glob("*.jpg"))
start_at = int(os.environ.get("LUMEN_BENCH_OFFSET", "300"))
STRINGS = {"type": "array", "items": {"type": "string"}}
TAG_SCHEMA = {"type": "object", "properties": {
    "description": {"type": "string"}, "subjects": STRINGS, "scene": {"type": "string"}, "technical": STRINGS,
    "confidence": {"type": "number"},
    "species": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "confidence": {"type": "number"}},
                                          "required": ["name", "confidence"], "additionalProperties": False}},
    "conditions": STRINGS}, "required": ["description", "subjects", "scene", "technical", "confidence", "species", "conditions"],
    "additionalProperties": False}
class Tagger(OmlxTagger):
    schema = False
    def _complete(self, content, timeout, **kw):
        if not self.schema: return super()._complete(content, timeout, **kw)
        import urllib.request
        real = urllib.request.Request
        def patched(url, data=None, headers=None):
            payload = json.loads(data); payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "tags", "schema": TAG_SCHEMA}}
            return real(url, data=json.dumps(payload).encode(), headers=headers)
        urllib.request.Request = patched
        try: return super()._complete(content, timeout, **kw)
        finally: urllib.request.Request = real
out = []
for i, m in enumerate(sys.argv[1:]):
    for j, use_schema in enumerate((False, True)):
        for p in previews[start_at + 6 * i + 3 * j: start_at + 6 * i + 3 * j + 3]:
            t = Tagger(s.omlx_url, m, role="fast"); t.schema = use_schema; t0 = time.monotonic()
            try: r = t.tag(p, vocabulary=vocab); res = {"description": r.description, "subjects": r.subjects, "species": list(r.species), "raw_len": len(r.raw_response)}
            except Exception as x: res = {"error": str(x)[:160]}
            out.append({"model": m, "schema": use_schema, "file": p.name[:10], "seconds": round(time.monotonic() - t0, 1), **res}); print(json.dumps(out[-1]), flush=True)
(Path(__file__).parent / "results" / "schema.json").write_text(json.dumps(out, indent=1))
