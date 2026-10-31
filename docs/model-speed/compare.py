"""Every model task through the unmodified OmlxTagger, all frames for one model before the next. Usage: compare.py <model>...

Frames are previews the snapshot never sent to a model (no tag, caption, reject, develop, burst or composition event),
stepped through with a stride from LUMEN_BENCH_OFFSET (default 40). Writes results/compare-<models>.json.
"""
import json, os, sqlite3, statistics, subprocess, sys, tempfile, time
from pathlib import Path
from PIL import Image
from lumen.analysis import analyze_subject_regions
from lumen.composition import generate_candidates
from lumen.config import Settings
from lumen.omlx import OmlxTagger
from lumen.previews import read_image_dimensions
from lumen.rawdevelop import render_raw, describe_histogram
from lumen.vocabulary import load_vocabulary

s = Settings.from_environment(); vocab = load_vocabulary(s); models = sys.argv[1:]
SNAP = Path(os.environ["LUMEN_BENCH_SNAPSHOT"]); PV = SNAP / "files/previews"
db = sqlite3.connect(f"file:{SNAP/'catalog.sqlite3'}?mode=ro", uri=True)
seen = {r[0] for r in db.execute("select distinct photo_id from provenance where kind in ('tagged','tag_failed','caption_written',"
        "'reject_suggested','develop_advised','burst_reviewed','burst_review_failed','composition_reviewed')")}
photos = {}  # preview name -> (id, raw path, group, exposure metric)
for pid, raw, prev, gid, exp in db.execute("select p.id, p.path, p.preview_path, p.group_id, m.exposure from photos p left join metrics m on m.photo_id = p.id"):
    if prev and (PV / Path(prev).name).is_file():
        photos[Path(prev).name] = (pid, Path(raw), gid, exp)
fresh = sorted(n for n, v in photos.items() if v[0] not in seen)
off = int(os.environ.get("LUMEN_BENCH_OFFSET", "40")); stride = (len(fresh) - off) // 10
frames = fresh[off::stride][:10]
astro = sorted(frames, key=lambda n: photos[n][3] if photos[n][3] is not None else 1)[:4]  # darkest: no frame here is a long exposure
groups = {}
for n, (pid, _, gid, _) in photos.items():
    if gid is not None: groups.setdefault(gid, []).append((pid, n))
burst_seen = {r[0] for r in db.execute("select distinct photo_id from provenance where kind like 'burst_review%'")}  # other tasks put text before the image, so no shared prefix
eligible = sorted(g for g, m in groups.items() if 3 <= len(m) <= 4 and not any(pid in burst_seen for pid, _ in m))
bursts = [sorted(groups[g]) for g in (eligible[0], eligible[len(eligible) // 2], eligible[-1])]

tmp = Path(tempfile.mkdtemp()); raws_ok = all(photos[n][1].is_file() for n in frames[:4])
develop = []  # rendered once, so both models see the same bytes
for n in frames[:4] if raws_ok else []:
    r = render_raw(photos[n][1], tmp / f"dev-{n}", s.raw_developer_path, settings={}, long_edge=1024, image_format="jpeg", histogram=True)
    develop.append((n, r.path, describe_histogram(r.histogram)))
compose = []  # what Library.review_composition sends: Vision regions on the raw, candidates cropped from the preview
for n in frames[4:8] if raws_ok else []:
    raw = photos[n][1]; w, h = read_image_dimensions(raw); reg = analyze_subject_regions(raw, s.vision_analyzer_path)
    cands, pv = generate_candidates(w, h, reg if reg and (reg.image_width, reg.image_height) == (w, h) else None), PV / n
    paths = []
    with Image.open(pv) as im:
        for c in cands:
            p = pv if c.kind == "original" else tmp / f"{n}-{c.id}.jpg"
            if p != pv: im.crop(c.rect.pixel_box(*im.size)).convert("RGB").save(p, format="JPEG", quality=90)
            paths.append((c.id, p))
    compose.append((n, pv, paths, [(c.id, c.kind, c.aspect) for c in cands]))

def rss_mb():
    pid = subprocess.run(["pgrep", "-x", "omlx-server"], capture_output=True, text=True).stdout.split()[:1]
    return round(int(subprocess.run(["ps", "-o", "rss=", "-p", pid[0]], capture_output=True, text=True).stdout) / 1024) if pid else None

def du(m):
    return subprocess.run(["du", "-sh", next(iter(Path("/Users/nasir/Models").glob(f"*/{m}")), m)], capture_output=True, text=True).stdout.split("\t")[0]

meta = {"snapshot": SNAP.name, "offset": off, "stride": stride, "fresh_previews": len(fresh), "raws_available": raws_ok,
        "usage": "not recorded: OmlxTagger._complete returns only the message text",
        "frames": {n: photos[n][0] for n in frames}, "astro_frames": astro, "bursts": [[pid for pid, _ in b] for b in bursts],
        "develop_frames": [d[0] for d in develop], "composition": {c[0]: c[3] for c in compose},
        "model_size_on_disk": {m: du(m) for m in models}, "omlx_rss_mb": {"before": rss_mb()}}
out = {"meta": meta, "calls": []}

def call(m, task, frame, fn, fields):
    t = time.monotonic()
    try: r = fn(); res = {"ok": True, **fields(r)}
    except Exception as e: res = {"ok": False, "error": str(e)[:300], "raw": (getattr(e, "raw_response", None) or "")[:600]}
    out["calls"].append({"model": m, "task": task, "frame": frame, "seconds": round(time.monotonic() - t, 2), **res})
    print(json.dumps(out["calls"][-1]), flush=True)

for m in models:
    tg = OmlxTagger(s.omlx_url, m)
    for n in frames:
        call(m, "tag", n, lambda: tg.tag(PV / n, vocabulary=vocab), lambda r: {"description": r.description, "subjects": r.subjects, "scene": r.scene,
             "species": list(r.species), "confidence": r.confidence, "technical": r.technical})
    for n in frames:
        call(m, "rejects", n, lambda: tg.review_rejects(PV / n), lambda r: {"problems": list(r.problems),
             "suggest_reject": r.suggest_reject, "confidence": r.confidence})
    for n in astro:
        call(m, "astro", n, lambda: tg.triage_astro(PV / n), lambda r: {"sky": r.sky, "issues": list(r.issues), "confidence": r.confidence})
    for n in frames[:6]:
        call(m, "caption", n, lambda: tg.caption(PV / n), lambda r: {"caption": r.caption, "alt_text": r.alt_text})
    for b in bursts:
        call(m, "burst", [pid for pid, _ in b], lambda: tg.critique_burst([(str(pid), PV / n) for pid, n in b]),
             lambda r: {"frame_id": r.frame_id, "rationale": r.rationale, "confidence": r.confidence})
    for n, path, note in develop:
        call(m, "develop", n, lambda: tg.suggest_develop(path, settings={}, histogram_note=note), lambda r: {"settings": r.settings, "rationale": r.rationale})
    for n, pv, paths, _ in compose:
        call(m, "composition", n, lambda: tg.review_composition(pv, paths), lambda r: {"candidate_id": r.candidate_id, "rationale": r.rationale, "confidence": r.confidence})
    meta["omlx_rss_mb"][f"after {m}"] = rss_mb()
if not raws_ok: meta["skipped"] = "raws unavailable: develop and composition"
meta["median_seconds"] = {m: {t: statistics.median(c["seconds"] for c in out["calls"] if c["model"] == m and c["task"] == t)
                              for t in dict.fromkeys(c["task"] for c in out["calls"])} for m in models}
meta["parse_ok"] = {m: {t: f'{sum(c["ok"] for c in out["calls"] if c["model"] == m and c["task"] == t)}/{sum(1 for c in out["calls"] if c["model"] == m and c["task"] == t)}'
                        for t in meta["median_seconds"][m]} for m in models}
label = "-vs-".join(m.split("-Instruct")[0].split("-4bit")[0] for m in models)
(Path(__file__).parent / "results" / f"compare-{label}.json").write_text(json.dumps(out, indent=1))
