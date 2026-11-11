import os, sys, time, json, tempfile, threading, urllib.request
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
sys.path.insert(0, "/home/claude/pydm")
import pydm
home = tempfile.mkdtemp()
pydm.APP_DIR = os.path.join(home, ".pydm"); os.makedirs(pydm.APP_DIR)
pydm.CONF_FILE = os.path.join(pydm.APP_DIR, "config.json"); pydm.LIST_FILE = os.path.join(pydm.APP_DIR, "downloads.json")
pydm.find_ytdlp = lambda: None

class H(SimpleHTTPRequestHandler):
    def __init__(s, *a, **k): super().__init__(*a, directory="/home/claude/t/www", **k)
    def log_message(s, *a): pass
srv = ThreadingHTTPServer(("127.0.0.1", 8766), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
B = "http://127.0.0.1:8766/"
ok = True
def check(n, c, e=""):
    global ok; print(("PASS " if c else "FAIL ") + n, e); ok &= bool(c)
def wait(fn, t=30):
    t0 = time.time()
    while not fn() and time.time() - t0 < t: time.sleep(0.2)
    return fn()

mgr = pydm.Manager(); mgr.cfg["folder"] = os.path.join(home, "dl"); api = pydm.Api(mgr)
check("detect", api.detect("https://youtu.be/x") == "site" and api.detect(B + "a.m3u8") == "stream" and api.detect(B + "a.zip") == "file" and api.detect("hello") == "")
n = api.add(f"{B}song.mp3\n{B}lo/index.m3u8  junk", "video")
check("add parses two urls", n == 2)
check("state json-serialisable", json.dumps(api.state()) is not None)
wait(lambda: all(r["status"] == "done" for r in api.state()["items"]))
st = api.state(); items = st["items"]
check("both done", [r["status"] for r in items] == ["done", "done"], [(r["status"], r["error"]) for r in items])
check("newest first + categories", items[0]["category"] == "video" and items[1]["category"] == "audio" and items[1]["name"] == "song.mp3", [(r["name"], r["category"]) for r in items])
check("settings/tools present", st["settings"]["conns"] == 8 and "ffmpeg" in st["tools"])
check("set_conns clamps", api.set_conns(99) and mgr.cfg["conns"] == 16 and not api.set_conns("x"))
song = items[1]["id"]
check("remove done + delete file", api.remove(song, True) and not os.path.exists(items[1]["path"]) and len(api.state()["items"]) == 1)
check("clear_done", api.clear_done() and api.state()["items"] == [])
check("unknown id safe", api.pause("999") is False and api.resume("nope") is False and api.remove("x") is False)

# incoming from extension shows up via state()
pydm.incoming.put((B + "direct.mp4", {}, {"kind": "", "mode": "video", "title": ""}))
check("extension link via pump", any(r["url"].endswith("direct.mp4") for r in api.state()["items"]))
wait(lambda: api.state()["items"][0]["status"] == "done")
# error + retry
api.add(B + "nope.bin")
wait(lambda: api.state()["items"][0]["status"] == "error", 40)
check("error surfaced", api.state()["items"][0]["status"] == "error")
# persistence across restart
mgr.save(); m2 = pydm.Manager(); s2 = pydm.Api(m2).state()["items"]
check("restored", len(s2) == 2 and {r["status"] for r in s2} == {"done", "error"}, [r["status"] for r in s2])
# ui ready -> /ping
srv2 = ThreadingHTTPServer(("127.0.0.1", 9614), pydm.Handler); threading.Thread(target=srv2.serve_forever, daemon=True).start()
ping = lambda: json.loads(urllib.request.urlopen("http://127.0.0.1:9614/ping").read())
check("ping ui false before ready", ping()["ui"] is False)
api.ready(); check("ping ui true after ready", ping()["ui"] is True)
check("/show safe without window", json.loads(urllib.request.urlopen("http://127.0.0.1:9614/show").read())["ok"])
check("clipboard safe on linux", api.clipboard() == "")
# ---------- settings + notifications (fake webview records the toast windows)
import types
created = []
class FakeWin:
    def __init__(s, **k): s.k = k; s.html = k.get("html"); s.js_api = k.get("js_api"); s.destroyed = False
    def destroy(s): s.destroyed = True
fake = types.ModuleType("webview")
fake.screens = [types.SimpleNamespace(width=1920, height=1080)]
def create_window(title, url=None, html=None, js_api=None, **k):
    w = FakeWin(title=title, html=html, js_api=js_api, **k); created.append(w); return w
fake.create_window = create_window
sys.modules["webview"] = fake
ok2 = True
def check2(n, c, e=""):
    global ok2; print(("PASS " if c else "FAIL ") + n, e); ok2 &= bool(c)
m3 = pydm.Manager(); m3.cfg["folder"] = os.path.join(home, "dl3"); a3 = pydm.Api(m3)
st = a3.state()["settings"]
check2("new settings default on", st["compat"] is True and st["notify"] is True)
check2("set_option", a3.set_option("compat", False) and m3.cfg["compat"] is False and not a3.set_option("folder", "x"))
m3.notifier = pydm.Notifier()
check2("toast template loaded", "/*__DATA__*/null" in m3.notifier.template)
d = m3.add(B + "song.mp3")
time.sleep(1.8)
check2("fast download: no 'started' notice", len(created) == 0, len(created))
wait(lambda: d.status == "done"); a3.state()
check2("done toast with file name", len(created) == 1 and '"event": "done"' in created[0].html and '"name": "song.mp3"' in created[0].html)
w = created[-1].k
check2("toast placed bottom-right, frameless, no focus", w["x"] == 1920 - 440 - 16 and w["y"] == 1080 - 128 - 64 and w["title"] == "PyDM-Toast" and w["frameless"] and w["on_top"] and w["focus"] is False)
check2("done toast has the file path", json.dumps(d.path)[1:-1] in created[0].html)
from http.server import BaseHTTPRequestHandler
class Slow(BaseHTTPRequestHandler):
    def log_message(s, *a): pass
    def do_GET(s):
        s.send_response(200); s.send_header("Content-Length", "100000000"); s.end_headers()
        try:
            for _ in range(60): s.wfile.write(b"x" * 1000); s.wfile.flush(); time.sleep(0.1)
        except Exception: pass
slow_srv = ThreadingHTTPServer(("127.0.0.1", 8767), Slow); threading.Thread(target=slow_srv.serve_forever, daemon=True).start()
slow = m3.add("http://127.0.0.1:8767/big.iso")
time.sleep(1.8)
check2("slow download: 'started' notice replaces the old one", len(created) == 2 and created[0].destroyed and '"event": "start"' in created[1].html)
slow.cancel()
api_t = created[1].js_api
check2("toast close works", api_t.close() and created[1].destroyed and m3.notifier.win is None)
m3.cfg["notify"] = False; d2 = m3.add(B + "nope.bin"); wait(lambda: d2.status == "error", 40); time.sleep(1.6); a3.state()
check2("no toasts when notifications are off", len(created) == 2)
m3.cfg["notify"] = True; a3.resume(d2.uid); time.sleep(0.5); wait(lambda: d2.status == "error", 40); a3.state()   # الحالة نفسها، لكن محاولة جديدة
check2("error toast", '"event": "error"' in created[-1].html)
d3 = pydm.YtdlpDownload("https://x.com/1", "/tmp", {}, 4)
m3.cfg["compat"] = True; m4 = m3.add("https://www.youtube.com/watch?v=z", None, {"kind": "ytdlp", "cookies": [{"name": "a"}]})
check2("manager passes compat + cookies to yt-dlp jobs", m4.compat is True and m4.cookies == [{"name": "a"}])
print("\nALL OK" if ok and ok2 else "\nSOME FAILED")
