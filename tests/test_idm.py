import os, sys, time, json, tempfile, threading, hashlib
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
sys.path.insert(0, "/home/claude/pydm")
import pydm
home = tempfile.mkdtemp()
pydm.APP_DIR = os.path.join(home, ".pydm"); os.makedirs(pydm.APP_DIR)
pydm.CONF_FILE = os.path.join(pydm.APP_DIR, "config.json"); pydm.LIST_FILE = os.path.join(pydm.APP_DIR, "downloads.json")
DATA = open("/home/claude/t/www/big.bin", "rb").read()
SLOW_FROM = [None]          # الطلبات التي تبدأ بعد هذا الموضع تكون بطيئة جداً
PER_REQ_DELAY = [0.0]
ranges = []
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(s, *a): pass
    def do_GET(s):
        name = s.path.split("?")[0].lstrip("/")
        body = DATA if name.startswith("big") else b"x" * 3_000_000
        rng = s.headers.get("Range")
        a, b = 0, len(body) - 1
        if rng:
            x, y = rng.replace("bytes=", "").split("-"); a = int(x); b = int(y) if y else len(body) - 1
            s.send_response(206); s.send_header("Content-Range", f"bytes {a}-{b}/{len(body)}")
        else:
            s.send_response(200)
        ranges.append((a, b))
        s.send_header("Content-Length", str(b - a + 1)); s.send_header("Content-Disposition", f'attachment; filename="{name}"'); s.end_headers()
        slow = SLOW_FROM[0] is not None and a >= SLOW_FROM[0] and (b - a) > 500_000 and a < SLOW_FROM[0] + 2_000_000
        pos = a
        try:
            while pos <= b:
                n = min(16384, b - pos + 1)
                s.wfile.write(body[pos:pos + n]); pos += n
                if slow: time.sleep(0.05)          # ≈ 320 KB/s لهذا الاتصال فقط
                elif PER_REQ_DELAY[0]: time.sleep(PER_REQ_DELAY[0])
        except Exception:
            pass
srv = ThreadingHTTPServer(("127.0.0.1", 8770), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
B = "http://127.0.0.1:8770/"
ok = True
def check(n, c, e=""):
    global ok; print(("PASS " if c else "FAIL ") + n, e); ok &= bool(c)
def wait(fn, t=60):
    t0 = time.time()
    while not fn() and time.time() - t0 < t: time.sleep(0.1)
    return fn()
md5 = lambda p: hashlib.md5(open(p, "rb").read()).hexdigest()
WANT = hashlib.md5(DATA).hexdigest()

# 1. dynamic segmentation: segment 3 of 4 is throttled; others finish and split it
SLOW_FROM[0] = 6_000_000
out = os.path.join(home, "dyn")
d = pydm.Download(B + "big1.bin", out, {}, 4); t0 = time.time(); d.start(); wait(lambda: d.status in ("done", "error"), 90)
el = time.time() - t0
check("dynamic: file correct", d.status == "done" and md5(d.path) == WANT, f"{d.status} {d.error}")
check("dynamic: segments were split", len(d.segs) > 4, len(d.segs))
check("dynamic: much faster than the slow connection alone (3MB @320KB/s ≈ 9s)", el < 6, f"{el:.1f}s")
SLOW_FROM[0] = None

# 2. resume after pause with split segments
PER_REQ_DELAY[0] = 0.02
d = pydm.Download(B + "big2.bin", os.path.join(home, "res"), {}, 4); d.start(); time.sleep(1.5); d.pause(); time.sleep(0.6)
snap = json.loads(json.dumps(d.to_dict())); PER_REQ_DELAY[0] = 0
check("paused midway", 0 < d.done_bytes() < len(DATA), d.done_bytes())
d2 = pydm.Download.from_dict(snap); d2.start(); wait(lambda: d2.status in ("done", "error"), 60)
check("resume of a split download is correct", d2.status == "done" and md5(d2.path) == WANT, f"{d2.status} {d2.error} paused_at={d.done_bytes()}")

# 3. speed limit
pydm.LIMITER.set(2_000_000)
d = pydm.Download(B + "big3.bin", os.path.join(home, "lim"), {}, 4); t0 = time.time(); d.start(); wait(lambda: d.status in ("done", "error"), 60)
el = time.time() - t0; pydm.LIMITER.set(0)
check("speed limit ~2 MB/s (12MB → ≥5s)", d.status == "done" and 4.8 <= el <= 9, f"{el:.1f}s")

# 4. queue: max 2 at once
mgr = pydm.Manager(); mgr.cfg["folder"] = os.path.join(home, "q"); mgr.cfg["max_parallel"] = 2; api = pydm.Api(mgr)
PER_REQ_DELAY[0] = 0.004
ds = [mgr.add(B + f"q{i}.bin") for i in range(5)]
time.sleep(0.5); mgr.pump()
running = [d for d in ds if d.status == "downloading"]; waiting = [r for r in api.state()["items"] if r["waiting"]]
check("queue: only 2 run, 3 wait", len(running) == 2 and len(waiting) == 3, (len(running), len(waiting)))
check("queue: oldest start first", set(running) == set(ds[:2]))
seen_max = [0]
def mon():
    while not all(d.status == "done" for d in ds):
        seen_max[0] = max(seen_max[0], sum(d.status == "downloading" for d in ds)); time.sleep(0.05)
threading.Thread(target=mon, daemon=True).start()   # المراقب لا يستدعي pump: الطابور يتقدم وحده
wait(lambda: all(d.status == "done" for d in ds), 90); PER_REQ_DELAY[0] = 0
check("queue: all finish, never more than 2 at once", all(d.status == "done" for d in ds) and seen_max[0] <= 2, seen_max[0])
check("categories: .bin goes to the main folder", all(os.path.dirname(d.path) == mgr.cfg["folder"] for d in ds))

# 5. categories
m2 = pydm.Manager(); m2.cfg["folder"] = os.path.join(home, "cat")
for n in ("clip.mp4", "song.mp3", "pack.zip", "setup.exe", "doc.pdf"):
    m2.add(B + n)
wait(lambda: all(d.status == "done" for d in m2.items), 60)
dirs = sorted(os.path.basename(os.path.dirname(d.path)) for d in m2.items)
check("categories: IDM-style folders", dirs == ["Compressed", "Documents", "Music", "Programs", "Video"], dirs)
y = m2.add("https://www.youtube.com/watch?v=x", None, {"kind": "ytdlp", "mode": "audio", "quality": "720"})
check("categories: yt-dlp audio → Music, quality kept", os.path.basename(y.folder) == "Music" and y.quality == "720"); y.cancel()
m2.cfg["categories"] = False; z = m2.add(B + "z.mp4"); wait(lambda: z.status == "done")
check("categories off: main folder", os.path.dirname(z.path) == m2.cfg["folder"])

# 6. refresh link
PER_REQ_DELAY[0] = 0.02
d = mgr.add(B + "big4.bin"); wait(lambda: d.status == "downloading"); time.sleep(1.2); api.pause(d.uid); time.sleep(0.5)
before = d.done_bytes()
check("refresh: paused midway", d.status == "paused" and 0 < before < len(DATA), (d.status, before))
check("refresh: rejects bad url / running", not api.refresh_link(d.uid, "ftp://x") and not api.refresh_link("nope", B + "big5.bin"))
check("refresh: accepted", api.refresh_link(d.uid, B + "big5.bin?token=new")); PER_REQ_DELAY[0] = 0
wait(lambda: d.status in ("done", "error"), 60)
check("refresh: resumed with the new link and file is correct", d.status == "done" and md5(d.path) == WANT and "token=new" in d.url and before > 0, f"{d.status} {d.error}")
PER_REQ_DELAY[0] = 0.02
e = mgr.add(B + "big6.bin"); wait(lambda: e.status == "downloading"); time.sleep(0.6); api.pause(e.uid); time.sleep(0.4); PER_REQ_DELAY[0] = 0
api.refresh_link(e.uid, B + "other.bin"); wait(lambda: e.status in ("done", "error"), 30)
check("refresh: different file is refused", e.status == "error" and "مختلف" in e.error, e.error)

# 7. quality → yt-dlp args
q = pydm.YtdlpDownload("https://youtu.be/x", home, {}, 4); q.compat = True; q.quality = "720"
pydm.find_ytdlp = lambda: ["yt-dlp"]; pydm.find_ffmpeg = lambda: "ffmpeg"; pydm.find_js_runtime = lambda: None
a = q._command(); check("quality 720 + compat", a[a.index("-S") + 1] == "vcodec:h264,res:720,acodec:m4a", a)
q.compat = False; a = q._command(); check("quality 720 no compat", a[a.index("-S") + 1] == "res:720")
q.quality = "best"; a = q._command(); check("best no compat: no sort", "-S" not in a)
pydm.LIMITER.set(500000); a = q._command(); pydm.LIMITER.set(0); check("speed limit passed to yt-dlp", a[a.index("--limit-rate") + 1] == "500000")
check("quality persisted", pydm.YtdlpDownload.from_dict(json.loads(json.dumps(dict(q.to_dict(), quality="1080")))).quality == "1080")

# 8. when done
calls = []; pydm.run_when_done = lambda a: calls.append(a)
m3 = pydm.Manager(); m3.cfg["folder"] = os.path.join(home, "wd"); a3 = pydm.Api(m3)
check("when_done validation", a3.set_option("when_done", "shutdown") and not a3.set_option("when_done", "format c:"))
f = m3.add(B + "w.bin"); m3.pump(); wait(lambda: f.status == "done"); m3.pump()
check("shutdown triggered once when the queue empties", calls == ["shutdown"] and m3.cfg["when_done"] == "none", calls)
g = m3.add(B + "w2.bin"); m3.pump(); wait(lambda: g.status == "done"); m3.pump()
check("one-shot: not triggered again", calls == ["shutdown"])
check("when_done not restored after restart", pydm.Manager().cfg["when_done"] == "none")
check("settings: max_parallel clamp + speed", a3.set_option("max_parallel", 50) and m3.cfg["max_parallel"] == 10 and a3.set_option("speed_limit", "1048576") and pydm.LIMITER.rate == 1048576)
a3.set_option("speed_limit", 0)
print("\nALL OK" if ok else "\nSOME FAILED")
