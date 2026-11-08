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
print("\nALL OK" if ok else "\nSOME FAILED")
