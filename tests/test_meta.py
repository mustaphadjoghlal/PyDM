# فيسبوك وإنستغرام: أعلى دقة أولاً، ثم تحويل VP9 إلى H.264 بالدقة نفسها
import os, sys, json, tempfile, subprocess, time
sys.path.insert(0, "/home/claude/pydm")
import pydm

tmp = tempfile.mkdtemp()
bin_dir = os.path.join(tmp, "bin"); os.makedirs(bin_dir)
os.symlink("/home/claude/pydm/tests/fake_ytdlp.py", os.path.join(bin_dir, "yt-dlp"))
os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]
os.environ["FAKE_LOG"] = os.path.join(tmp, "calls.log")
ok = True
def check(n, c, extra=""):
    global ok; print(("PASS " if c else "FAIL ") + n, extra); ok &= bool(c)
def wait(d, t=120):
    t0 = time.time()
    while d.status in ("queued", "downloading") and time.time() - t0 < t: time.sleep(0.1)
    return d.status
def last_call():
    return json.loads(open(os.environ["FAKE_LOG"]).read().strip().splitlines()[-1])
def codecs(p):
    return pydm.YtdlpDownload._codecs("ffmpeg", p)

vp9 = os.path.join(tmp, "src.mp4")
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=1080x1920:d=1:r=25",
                "-f", "lavfi", "-i", "sine=d=1", "-c:v", "libvpx-vp9", "-b:v", "1M", "-c:a", "libopus",
                "-shortest", vp9], check=True)
HDR = {"Referer": "https://www.instagram.com/", "User-Agent": "UA/1"}

os.environ["FAKE_SRC"] = vp9
d = pydm.YtdlpDownload("https://www.instagram.com/reel/abc?v=ig1", os.path.join(tmp, "out"), HDR, 4, "T")
d.start(); st = wait(d); a = last_call()
check("instagram: resolution sorted before codec", a[a.index("-S") + 1] == "res,vcodec:h264,acodec:m4a", a)
check("instagram: browser UA passed", "--user-agent" in a and a[a.index("--user-agent") + 1] == "UA/1")
check("done", st == "done", f"{st} {d.error}")
vc, ac = codecs(d.path)
check("vp9 converted to h264 + aac", (vc, ac) == ("h264", "aac"), (vc, ac))
r = subprocess.run(["ffmpeg", "-hide_banner", "-i", d.path], capture_output=True, text=True).stderr
check("resolution kept 1080x1920", "1080x1920" in r)
check("no leftover temp file", not any(f.endswith(".h264.mp4") for f in os.listdir(os.path.dirname(d.path))))

d = pydm.YtdlpDownload("https://www.facebook.com/reel/123?v=fb1", os.path.join(tmp, "out"), HDR, 4, "T")
d.quality = "720"; d.start(); wait(d); a = last_call()
check("facebook 720: res:720 first", a[a.index("-S") + 1] == "res:720,vcodec:h264,acodec:m4a", a)

d = pydm.YtdlpDownload("https://www.facebook.com/reel/123?v=fb1", os.path.join(tmp, "out2"), HDR, 4, "T")
d.compat = False; d.start(); wait(d)
check("compat off: file left as vp9", codecs(d.path)[0] == "vp9")

del os.environ["FAKE_SRC"]
d = pydm.YtdlpDownload("https://www.youtube.com/watch?v=abc", os.path.join(tmp, "out3"), HDR, 4, "T")
d.start(); wait(d); a = last_call()
check("youtube unchanged: h264 first, no UA", a[a.index("-S") + 1] == "vcodec:h264,res,acodec:m4a" and "--user-agent" not in a, a)
print("ALL OK" if ok else "SOME FAILED"); sys.exit(0 if ok else 1)
