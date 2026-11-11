import os, sys, time, json, shutil, tempfile, threading, http.client
sys.path.insert(0, "/home/claude/pydm")
import pydm

tmp = tempfile.mkdtemp()
bin_dir = os.path.join(tmp, "bin"); os.makedirs(bin_dir)
os.symlink("/home/claude/t/fake_ytdlp.py", os.path.join(bin_dir, "yt-dlp"))
os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]
open(os.path.join(bin_dir, "deno"), "w").close(); os.chmod(os.path.join(bin_dir, "deno"), 0o755)
os.environ["FAKE_LOG"] = os.path.join(tmp, "calls.log")
out = os.path.join(tmp, "dl 100%")          # يحتوي مسافة وعلامة %
HDR = {"Referer": "https://www.youtube.com/watch?v=abc", "Cookie": "a=b", "User-Agent": "UA/1"}
ok = True
def check(n, c, extra=""):
    global ok; print(("PASS " if c else "FAIL ") + n, extra); ok &= bool(c)
def wait(d, t=30):
    t0 = time.time()
    while d.status in ("queued", "downloading") and time.time() - t0 < t: time.sleep(0.1)
    return d.status
def last_call():
    return json.loads(open(os.environ["FAKE_LOG"]).read().strip().splitlines()[-1])

real_ff = pydm.find_ffmpeg
URL = "https://www.youtube.com/watch?v=abc"

pydm.find_ffmpeg = lambda: "/usr/bin/ffmpeg"
d = pydm.YtdlpDownload(URL, out, HDR, 4, "T"); d.start(); st = wait(d)
a = last_call()
check("video ok", st == "done" and os.path.isfile(d.path) and d.name == "عنوان abc.mp4" and d.percent() == 100, f"{st} {d.error} {d.name}")
check("output template escapes % in folder", any("dl 100%%" in x for x in a))
check("no browser UA/cookies/referer sent to yt-dlp for youtube", "--user-agent" not in a and not any("Cookie" in x for x in a) and "--referer" not in a and "--add-header" not in a, a)
check("js runtime passed", "--js-runtimes" in a and a[a.index("--js-runtimes")+1].startswith("deno:"), a)
check("with ffmpeg: merge mp4", "--merge-output-format" in a and "--ffmpeg-location" in a and "bv*+ba/b" in a)
check("premiere-compatible H.264/AAC preferred", a[a.index("-S")+1] == "vcodec:h264,res,acodec:m4a", a)
check("youtube gets no cookies file", "--cookies" not in a)
check("url last + no-playlist", a[-1] == URL and "--no-playlist" in a)

d = pydm.YtdlpDownload(URL, out, HDR, 4, "T", "audio"); d.start(); st = wait(d)
a = last_call()
check("audio mp3 with ffmpeg", st == "done" and d.path.endswith(".mp3") and "-x" in a and "mp3" in a, f"{st} {d.error}")

pydm.find_ffmpeg = lambda: None
d = pydm.YtdlpDownload(URL, out, HDR, 4, "T"); d.start(); st = wait(d)
a = last_call()
check("no ffmpeg: single-file format", st == "done" and a[a.index("-f")+1] == "b" and "--merge-output-format" not in a, f"{st} {d.error}")
d = pydm.YtdlpDownload(URL, out, HDR, 4, "T", "audio"); d.start(); st = wait(d)
a = last_call()
check("no ffmpeg audio: native audio", st == "done" and a[a.index("-f")+1] == "ba/b" and "-x" not in a, f"{st} {d.error}")

# progress parsing while running
os.environ["FAKE_DELAY"] = "0.3"
d = pydm.YtdlpDownload(URL, out, HDR, 4, "T"); d.start(); time.sleep(1.2)
check("live progress", d.status == "downloading" and d.total == 1000000 and 0 < d.percent() < 100 and d.speed == 123456 and d.has_progress(), f"{d.status} {d.percent()} {d.speed}")
# pause then resume
d.pause(); time.sleep(0.8)
check("paused kills process", d.status == "paused" and d.proc is None)
snap = json.loads(json.dumps(d.to_dict()))
d2 = pydm.YtdlpDownload.from_dict(snap)
check("restore kind/media", snap["kind"] == "ytdlp" and d2.status == "paused" and d2.media == "video")
os.environ["FAKE_DELAY"] = "0.02"
d2.start(); st = wait(d2)
check("resume completes", st == "done" and os.path.isfile(d2.path), f"{st} {d2.error}")

# cancel
os.environ["FAKE_DELAY"] = "0.5"
d = pydm.YtdlpDownload(URL, out, HDR, 4, "T"); d.start(); time.sleep(1.0); d.cancel(); time.sleep(1.5)
check("cancel", d.status == "canceled" and d.proc is None)
os.environ["FAKE_DELAY"] = "0.05"

# compat off -> no codec preference
d = pydm.YtdlpDownload(URL, out, HDR, 4, "T"); d.compat = False; d.start(); wait(d)
check("compat off: best quality, any codec", "-S" not in last_call())

# facebook: cookies written to a Netscape file, removed afterwards
fb_cookies = [{"name": "c_user", "value": "123", "domain": ".facebook.com", "path": "/", "secure": True, "httpOnly": False, "expirationDate": 1999999999},
              {"name": "xs", "value": "abc", "domain": ".facebook.com", "path": "/", "secure": True, "httpOnly": True, "expirationDate": 0}]
os.environ["FAKE_COPY_COOKIES"] = os.path.join(tmp, "seen_cookies.txt")
d = pydm.YtdlpDownload("https://www.facebook.com/watch/?v=42", out, {}, 4, "T"); d.cookies = fb_cookies; d.start(); st = wait(d)
a = last_call(); ck = a[a.index("--cookies")+1] if "--cookies" in a else ""
seen = open(os.environ["FAKE_COPY_COOKIES"]).read() if os.path.exists(os.environ["FAKE_COPY_COOKIES"]) else ""
check("facebook gets a cookies file", st == "done" and ck and "\tc_user\t123" in seen and "#HttpOnly_.facebook.com\tTRUE\t/\tTRUE\t0\txs\tabc" in seen and seen.startswith("# Netscape"), seen)
check("cookies file deleted after download", ck and not os.path.exists(ck))
check("cookies never saved to the list", "cookies" not in json.dumps(d.to_dict()))

# non-youtube sites keep the referer
d = pydm.YtdlpDownload("https://vimeo.com/123", out, {"Referer": "https://site.example/page"}, 4, "T"); d.start(); wait(d)
a = last_call(); check("referer kept for other sites", a[a.index("--referer")+1] == "https://site.example/page", a)

# error from yt-dlp
os.environ["FAKE_MODE"] = "fail"
d = pydm.YtdlpDownload(URL, out, HDR, 4, "T"); d.start(); st = wait(d)
check("yt-dlp error surfaced", st == "error" and "Video unavailable" in d.error, d.error)
os.environ["FAKE_MODE"] = "ok"

# missing yt-dlp
os.remove(os.path.join(bin_dir, "yt-dlp"))
real_find = pydm.find_ytdlp
d = pydm.YtdlpDownload(URL, out, HDR, 4, "T"); pydm.find_ytdlp = lambda: None; d.start(); st = wait(d)
check("missing yt-dlp message", st == "error" and "yt-dlp.exe" in d.error, d.error)
pydm.find_ytdlp = real_find

# routing in App.add logic + handler meta
check("youtube url auto-routes", pydm.is_ytdlp_url(URL) and not pydm.is_ytdlp_url("https://cdn.example.com/a.mp4"))
srv = pydm.ThreadingHTTPServer(("127.0.0.1", 9614), pydm.Handler); threading.Thread(target=srv.serve_forever, daemon=True).start()
def post(body):
    c = http.client.HTTPConnection("127.0.0.1", 9614)
    c.request("POST", "/add", json.dumps(body), {"Content-Type": "application/json", "Origin": "chrome-extension://x"})
    return c.getresponse().status
check("handler ytdlp meta", post({"url": URL, "kind": "ytdlp", "mode": "audio", "title": "ع"}) == 200)
u, h, m = pydm.incoming.get(timeout=2)
check("meta mode/kind", m == {"kind": "ytdlp", "mode": "audio", "quality": "best", "playlist": False, "title": "ع", "cookies": []}, str(m))
post({"url": URL, "kind": "weird"}); u, h, m = pydm.incoming.get(timeout=2)
check("unknown kind sanitized", m["kind"] == "" and m["mode"] == "video")

print("\nALL OK" if ok else "\nSOME FAILED")
shutil.rmtree(tmp, ignore_errors=True)
