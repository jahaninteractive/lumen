import sys, time
from concurrent.futures import ThreadPoolExecutor
sys.argv = ["x", "Qwen3.8-27B-4bit"]
exec(open(__file__.replace("parallel.py", "speed2.py")).read().split("out = []")[0])
from PIL import ImageOps
def one(p, edge):
    t = Tagger(s.omlx_url, "Qwen3.8-27B-4bit", role="fast"); t.edge, t.compact = edge, True
    t.tag(p, vocabulary=vocab)
# fresh image bytes per run: 1000 and 1001 px edges are never cached from earlier runs
for workers, edge, frames in [(1, 1000, previews[12:16]), (4, 1001, previews[12:16]), (4, 1002, previews[16:20])]:
    start = time.monotonic()
    with ThreadPoolExecutor(workers) as pool: list(pool.map(lambda p: one(p, edge), frames))
    total = time.monotonic() - start
    print(f"{workers} at once: {total:.1f}s for {len(frames)} frames = {total/len(frames):.1f}s per frame", flush=True)
