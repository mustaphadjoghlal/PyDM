import os, sys, time, threading, shutil, tempfile, hashlib, subprocess, json
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
sys.path.insert(0, "/home/claude/pydm")
import pydm

WWW = "/home/claude/t/www"
seen = []
SLOW = [False]

class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=WWW, **k)
    def log_message(self, *a): pass
    def send_head(self):
        seen.append((self.path, self.headers.get("Referer"), self.headers.get("Cookie")))
        if SLOW[0] and "/seg" in self.path: time.sleep(0.5)
        rng = self.headers.get("Range")
        path = self.translate_path(self.path.split("?")[0])
        if not rng or not os.path.isfile(path):
            return super().send_head()
        size = os.path.getsize(path)
        a, b = rng.replace("bytes=", "").split("-")
        a = int(a); b = int(b) if b else size - 1
        b = min(b, size - 1)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
        self.send_header("Content-Length", str(b - a + 1))
        self.end_headers()
        f = open(path, "rb"); f.seek(a)
        self._left = b - a + 1
        return f
    def copyfile(self, source, outputfile):
        left = getattr(self, "_left", None)
        if left is None:
            return super().copyfile(source, outputfile)
        outputfile.write(source.read(left)); self._left = None

srv = ThreadingHTTPServer(("127.0.0.1", 8765), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
B = "http://127.0.0.1:8765/"
out = tempfile.mkdtemp()

def wait(d, t=60):
    t0 = time.time()
    while d.status in ("queued", "downloading") and time.time() - t0 < t:
        time.sleep(0.2)
    return d.status

def probe(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", p],
                       capture_output=True, text=True)
    return sorted(r.stdout.split())

ok = True
def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name, extra)
    ok &= bool(cond)

real_ff = pydm.find_ffmpeg
HDR = {"Referer": "http://page.example/watch", "Cookie": "a=b"}

# 1. regression: direct mp4 (ranged, multi-part) + mp3 (small)
for f in ("direct.mp4", "song.mp3"):
    d = pydm.Download(B + f, out, HDR, 4); d.start(); st = wait(d)
    same = os.path.exists(d.path) and open(d.path,"rb").read() == open(os.path.join(WWW, f),"rb").read()
    check(f"direct {f}", st == "done" and same, st + " " + d.error)

# duplicate file name → "song 1.mp3"
d = pydm.Download(B + "song.mp3", out, HDR, 4); d.start(); wait(d)
check("duplicate direct download renamed 'song 1.mp3'", d.status == "done" and d.name == "song 1.mp3" and os.path.exists(os.path.join(out, "song.mp3")), d.name)

# 2. HLS master -> picks hi variant, no ffmpeg -> .ts
pydm.find_ffmpeg = lambda: None
d = pydm.StreamDownload(B + "master.m3u8", out, HDR, 4, "فيديو تجريبي"); d.start(); st = wait(d)
want = b"".join(open(f"{WWW}/hi/seg{i}.ts","rb").read() for i in range(6))
check("HLS master no-ffmpeg", st == "done" and d.path.endswith(".ts") and open(d.path,"rb").read() == want, f"{st} {d.error} {d.name}")
check("HLS name from title", d.name.startswith("فيديو تجريبي"))
check("HLS headers forwarded", any(r[1] == HDR["Referer"] and r[2] == "a=b" and "seg0" in r[0] for r in seen))
check("HLS workdir cleaned", not os.path.exists(d.path + ".parts"))

# 3. HLS with ffmpeg -> .mp4 containing audio+video
pydm.find_ffmpeg = real_ff
d = pydm.StreamDownload(B + "lo/index.m3u8", out, HDR, 4); d.start(); st = wait(d)
check("HLS -> mp4 via ffmpeg", st == "done" and d.path.endswith(".mp4") and probe(d.path) == ["audio","video"], f"{st} {d.error} {d.name} {probe(d.path) if os.path.exists(d.path) else ''}")
check("no leftovers", not any(n.endswith((".part",".parts")) for n in os.listdir(out)), str(os.listdir(out)))

# 4. fMP4 HLS (init segment) no ffmpeg needed
pydm.find_ffmpeg = lambda: None
d = pydm.StreamDownload(B + "fmp4/index.m3u8", out, HDR, 4); d.start(); st = wait(d)
check("fMP4 HLS", st == "done" and d.path.endswith(".mp4") and probe(d.path) == ["audio","video"], f"{st} {d.error} {probe(d.path) if os.path.exists(d.path) else ''}")

# 5. encrypted HLS: no ffmpeg -> clear error; with ffmpeg -> works
d = pydm.StreamDownload(B + "enc/index.m3u8", out, HDR, 4); d.start(); st = wait(d)
check("encrypted no-ffmpeg error", st == "error" and "ffmpeg" in d.error, d.error)
pydm.find_ffmpeg = real_ff
d = pydm.StreamDownload(B + "enc/index.m3u8", out, HDR, 4); d.start(); st = wait(d)
check("encrypted via ffmpeg", st == "done" and probe(d.path) == ["audio","video"], f"{st} {d.error}")

# 6. DASH via ffmpeg (kind detected by content, extension absent via meta path)
d = pydm.StreamDownload(B + "dash/manifest.mpd", out, HDR, 4, "dash clip"); d.start(); st = wait(d)
check("DASH via ffmpeg", st == "done" and probe(d.path) == ["audio","video"], f"{st} {d.error}")
pydm.find_ffmpeg = lambda: None
d = pydm.StreamDownload(B + "dash/manifest.mpd", out, HDR, 4); d.start(); st = wait(d)
check("DASH no-ffmpeg error", st == "error" and "ffmpeg" in d.error, d.error)

# 7. live playlist rejected
d = pydm.StreamDownload(B + "live.m3u8", out, HDR, 4); d.start(); st = wait(d)
check("live rejected", st == "error" and "مباشر" in d.error, d.error)

# 8. missing segment -> error after retries (shorten sleeps)
pydm.time_sleep = None
bad = os.path.join(WWW, "badpl.m3u8")
open(bad, "w").write("#EXTM3U\n#EXTINF:2,\nlo/seg0.ts\n#EXTINF:2,\nlo/nope.ts\n#EXT-X-ENDLIST\n")
_sleep = time.sleep
pydm.time.sleep = lambda s: _sleep(min(s, 0.05)) if s != 0.5 else _sleep(0.1)
d = pydm.StreamDownload(B + "badpl.m3u8", out, HDR, 2); d.start(); st = wait(d)
check("bad segment -> error", st == "error" and "القطعة 2" in d.error, d.error)
pydm.time.sleep = _sleep

# 9. pause / resume / persistence (slow server path via many pauses)
pydm.find_ffmpeg = lambda: None
SLOW[0] = True
d = pydm.StreamDownload(B + "hi/index.m3u8", out, HDR, 1); d.start()
time.sleep(1.4); d.pause(); time.sleep(1.5)
st_after_pause = d.status
snap = json.loads(json.dumps(d.to_dict()))
d2 = pydm.StreamDownload.from_dict(snap)
check("restore from json is paused", d2.status == "paused" and d2.mode == "segments" and len(d2.seg_urls) == 6)
SLOW[0] = False
check("paused midway", 0 < sum(d2.seg_done) < 6, str(d2.seg_done))
d2.start(); st = wait(d2)
want = b"".join(open(f"{WWW}/hi/seg{i}.ts","rb").read() for i in range(6))
check("resume after restore", st == "done" and open(d2.path,"rb").read() == want, f"{st} {d2.error} paused_state={st_after_pause}")

# 10. cancel cleans up
SLOW[0] = True
before = set(os.listdir(out))
d = pydm.StreamDownload(B + "lo/index.m3u8", out, HDR, 1); d.start(); time.sleep(1.0); d.cancel(); time.sleep(2.5)
SLOW[0] = False
left = [n for n in set(os.listdir(out)) - before]
check("cancel cleans temp", d.status == "canceled" and not left, f"{d.status} {left}")

# 11. handler/meta plumbing
import http.client
pydm_srv = pydm.ThreadingHTTPServer(("127.0.0.1", 9614), pydm.Handler)
threading.Thread(target=pydm_srv.serve_forever, daemon=True).start()
def post(origin, body):
    c = http.client.HTTPConnection("127.0.0.1", 9614); h = {"Content-Type": "application/json"}
    if origin: h["Origin"] = origin
    c.request("POST", "/add", json.dumps(body), h); r = c.getresponse(); return r.status
check("handler rejects web origin", post("https://evil.example", {"url": "http://x/y.mp4"}) == 403)
check("handler accepts ext + meta", post("chrome-extension://abc", {"url": "http://x/y", "kind": "stream", "title": "T", "headers": {"Referer": "r"}}) == 200)
u, h, m = pydm.incoming.get(timeout=2)
check("meta passed", m == {"kind": "stream", "mode": "video", "quality": "best", "playlist": False, "title": "T", "cookies": []} and h == {"Referer": "r"}, str(m))

print("\nALL OK" if ok else "\nSOME FAILED")
shutil.rmtree(out, ignore_errors=True)
