import os, sys, time, json, tempfile, threading, http.client
sys.path.insert(0, "/home/claude/pydm")
import pydm
tmp = tempfile.mkdtemp(); home = tmp
pydm.APP_DIR = os.path.join(home, ".pydm"); os.makedirs(pydm.APP_DIR)
pydm.CONF_FILE = os.path.join(pydm.APP_DIR, "config.json"); pydm.LIST_FILE = os.path.join(pydm.APP_DIR, "downloads.json")
bin_dir = os.path.join(tmp, "bin"); os.makedirs(bin_dir)
os.symlink("/home/claude/t/fake_ytdlp.py", os.path.join(bin_dir, "yt-dlp"))
os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]; os.environ["FAKE_LOG"] = os.path.join(tmp, "calls.log"); os.environ["FAKE_DELAY"] = "0.03"
pydm.update_ytdlp_once = lambda: None
ok = True
def check(n, c, e=""):
    global ok; print(("PASS " if c else "FAIL ") + n, e); ok &= bool(c)
def wait(fn, t=40):
    t0 = time.time()
    while not fn() and time.time() - t0 < t: time.sleep(0.1)
    return fn()
P = pydm.is_playlist_url
check("playlist url detection", P("https://www.youtube.com/playlist?list=PL123") and P("https://www.youtube.com/watch?v=a&list=PL123")
      and not P("https://www.youtube.com/watch?v=a") and not P("https://www.youtube.com/watch?v=a&list=RDabc") and not P("https://example.com/playlist?list=PL1")
      and P("https://soundcloud.com/artist/sets/album-1"))
mgr = pydm.Manager(); mgr.cfg["folder"] = os.path.join(home, "dl"); mgr.cfg["max_parallel"] = 2; api = pydm.Api(mgr)
check("detect → playlist", api.detect("https://www.youtube.com/playlist?list=PL123") == "playlist" and api.detect("https://youtu.be/x") == "site")
n = api.add("https://www.youtube.com/watch?v=aaa&list=PL123", "video", "720", True)
check("add returns 1 and shows reading notice", n == 1 and api.state()["notice"] == "جارٍ قراءة القائمة...")
wait(lambda: len(mgr.items) == 3)
st = api.state()
check("3 videos queued (private + duplicate skipped)", len(mgr.items) == 3, len(mgr.items))
check("done notice", "أُضيف 3 فيديو" in st["notice"] and api.state()["notice"] == "", st["notice"])
d = mgr.items
check("order, titles, prefix, quality", [x.prefix for x in d] == ["01 - ", "02 - ", "03 - "] and [x.title for x in d] == ["الدرس الأول", "الدرس الثاني", "Lesson 3"] and all(x.quality == "720" for x in d))
check("each item is a single-video url", [x.url.split("=")[-1] for x in d] == ["aaa", "ccc", "ddd"])
check("folder: Video/<playlist name sanitized>", all(x.folder == os.path.join(mgr.cfg["folder"], "Video", "قائمة_ دروس_المونتاج") for x in d), d[0].folder)
check("queue limit respected", sum(x.status in ("queued", "downloading") and not x.in_queue for x in d) <= 2)
wait(lambda: all(x.status == "done" for x in d))
names = sorted(os.listdir(d[0].folder))
check("files numbered in the playlist folder", names == ["01 - عنوان aaa.mp4", "02 - عنوان ccc.mp4", "03 - عنوان ddd.mp4"], names)
first = json.loads(open(os.environ["FAKE_LOG"]).read().splitlines()[0])
check("listing command", "--flat-playlist" in first and "--yes-playlist" in first and first[-1].endswith("list=PL123"))
check("prefix persisted", pydm.YtdlpDownload.from_dict(json.loads(json.dumps(d[1].to_dict()))).prefix == "02 - ")
# single video with list= but playlist off
k = api.add("https://www.youtube.com/watch?v=zzz&list=PL123", "audio", "best", False); time.sleep(0.3)
check("playlist off: one item only, in Music", len(mgr.items) == 4 and os.path.basename(mgr.items[-1].folder) == "Music")
# error
os.environ["FAKE_PL"] = "empty"; api.add("https://www.youtube.com/playlist?list=PLx", "video", "best", True)
wait(lambda: "تعذّرت" in (mgr.notice or ""), 10)
check("unreadable playlist → clear notice, nothing added", "does not exist" in mgr.notice and len(mgr.items) == 4, mgr.notice)
os.environ.pop("FAKE_PL")
# from the extension
srv = pydm.ThreadingHTTPServer(("127.0.0.1", 9614), pydm.Handler); threading.Thread(target=srv.serve_forever, daemon=True).start()
c = http.client.HTTPConnection("127.0.0.1", 9614)
c.request("POST", "/add", json.dumps({"url": "https://www.youtube.com/playlist?list=PL9", "kind": "ytdlp", "mode": "audio", "playlist": True}), {"Content-Type": "application/json", "Origin": "chrome-extension://x"})
check("handler accepts playlist flag", c.getresponse().status == 200)
mgr.pump(); wait(lambda: len(mgr.items) == 7)
check("extension playlist → 3 audio items in Music/<name>", len(mgr.items) == 7 and all(x.media == "audio" and "Music" in x.folder for x in mgr.items[4:]))
print("\nALL OK" if ok else "\nSOME FAILED")
