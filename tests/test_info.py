import os, sys, time, json, tempfile, threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
sys.path.insert(0, "/home/claude/pydm")
import pydm
home = tempfile.mkdtemp()
pydm.APP_DIR = os.path.join(home, ".pydm"); os.makedirs(pydm.APP_DIR)
pydm.CONF_FILE = os.path.join(pydm.APP_DIR, "config.json"); pydm.LIST_FILE = os.path.join(pydm.APP_DIR, "downloads.json")
class H(SimpleHTTPRequestHandler):
    def __init__(s, *a, **k): super().__init__(*a, directory="/home/claude/t/www", **k)
    def log_message(s, *a): pass
srv = ThreadingHTTPServer(("127.0.0.1", 8771), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
B = "http://127.0.0.1:8771/"
ok = True
def check(n, c, e=""):
    global ok; print(("PASS " if c else "FAIL ") + n, e); ok &= bool(c)
def wait(fn, t=30):
    t0 = time.time()
    while not fn() and time.time() - t0 < t: time.sleep(0.1)
    return fn()
class FakeUI:
    def __init__(s): s.opened = []; s.closed = []
    def open(s, p): s.opened.append(p)
    def close(s, pid): s.closed.append(pid)
mgr = pydm.Manager(); mgr.cfg["folder"] = os.path.join(home, "dl"); api = pydm.Api(mgr)
# no UI yet → starts directly (old behaviour)
d0 = mgr.request(B + "song.mp3"); check("no dialog provider → direct add", d0 is not None and len(mgr.items) == 1); wait(lambda: d0.status == "done")
ui = FakeUI(); ui.mgr = mgr; mgr.info_ui = ui
# file request → dialog, nothing added yet
r = mgr.request(B + "direct.mp4", {"Referer": "x"}, {})
p = ui.opened[-1]
check("dialog opened, nothing downloading yet", r is None and len(mgr.items) == 1 and p["kind"] == "file" and p["name"] == "direct.mp4" and p["category"] == "video")
check("suggested folder is the category folder", p["folder"] == os.path.join(mgr.cfg["folder"], "Video"))
wait(lambda: not p["probing"]); check("probe fills the size", p["total"] == os.path.getsize("/home/claude/t/www/direct.mp4"), p)
ia = pydm.InfoApi(ui, p)
check("category change → folder", ia.category("doc") == os.path.join(mgr.cfg["folder"], "Documents") and ia.category("bogus") == os.path.join(mgr.cfg["folder"], "Documents"))
custom = os.path.join(home, "my folder")
check("start with custom name + folder", ia.done("start", "مصطفى.mp4", custom) and ui.closed == [p["id"]] and p["id"] not in mgr.pending)
d = mgr.items[-1]; wait(lambda: d.status == "done")
check("saved exactly there with that name", d.status == "done" and d.path == os.path.join(custom, "مصطفى.mp4") and os.path.isfile(d.path), d.path)
# same name again → "مصطفى 1.mp4"
mgr.request(B + "direct.mp4"); p = ui.opened[-1]; pydm.InfoApi(ui, p).done("start", "مصطفى.mp4", custom)
d = mgr.items[-1]; wait(lambda: d.status == "done")
check("existing name → 'مصطفى 1.mp4'", os.path.basename(d.path) == "مصطفى 1.mp4", d.path)
# later
mgr.request(B + "song.mp3"); p = ui.opened[-1]; pydm.InfoApi(ui, p).done("later", "", "")
d = mgr.items[-1]; time.sleep(0.5)
check("download later → paused in the list, not started", d.status == "paused" and not d.in_queue and d.thread is None)
api.resume(d.uid); wait(lambda: d.status == "done"); check("later item resumes fine", d.status == "done" and os.path.basename(os.path.dirname(d.path)) == "Music", d.path)
# cancel
n = len(mgr.items); mgr.request(B + "song.mp3"); p = ui.opened[-1]; pydm.InfoApi(ui, p).done("cancel")
check("cancel → nothing added", len(mgr.items) == n and not mgr.pending)
check("bad action rejected", pydm.InfoApi(ui, p).done("format") is False)
# closing the window with × after deciding does nothing; before deciding = cancel
mgr.request(B + "song.mp3"); p = ui.opened[-1]; mgr.resolve(p["id"], "cancel"); mgr.resolve(p["id"], "cancel")
check("window closed with × → cancelled once", len(mgr.items) == n)
# yt-dlp request: title cleaned, ext shown, extension stripped from custom name
mgr.request("https://www.youtube.com/watch?v=x", None, {"kind": "ytdlp", "mode": "video", "title": "Jolene (Live) - YouTube", "quality": "720"})
p = ui.opened[-1]
check("yt-dlp dialog: clean title + .mp4, no probing", p["name"] == "Jolene (Live)" and p["ext"] == ".mp4" and not p["probing"] and p["category"] == "video")
pydm.find_ytdlp = lambda: None
pydm.InfoApi(ui, p).done("later", "أغنيتي.mp4", custom); y = mgr.items[-1]
check("yt-dlp custom name without extension, quality kept", isinstance(y, pydm.YtdlpDownload) and y.force_name == "أغنيتي" and y.quality == "720" and y.folder == custom and y.title == "أغنيتي")
pydm.find_ytdlp = lambda: ["yt-dlp"]; pydm.find_ffmpeg = lambda: None; pydm.find_js_runtime = lambda: None
a = y._command(); tpl = a[a.index("-o") + 1]
check("yt-dlp output template uses the custom name", tpl.endswith(os.sep + "أغنيتي.%(ext)s") and ".pydm-" in tpl, tpl)
y._rm_part()
# playlist items and playlists never ask
k = len(ui.opened); mgr.request("https://www.youtube.com/watch?v=q", None, {"kind": "ytdlp", "subfolder": "PL", "prefix": "01 - "})
check("playlist items skip the dialog", len(ui.opened) == k); mgr.items[-1].cancel()
# never ask again
mgr.request(B + "song.mp3"); p = ui.opened[-1]; pydm.InfoApi(ui, p).done("start", "", "", True)
k = len(ui.opened); d = mgr.request(B + "song.mp3")
check("'don't show again' → setting off and next download starts directly", mgr.cfg["ask"] is False and d is not None and len(ui.opened) == k and api.state()["settings"]["ask"] is False)
check("setting can be turned back on", api.set_option("ask", True) and mgr.cfg["ask"] is True)
# app's own add bar goes through the dialog too
k = len(ui.opened); api.add(B + "song.mp3"); check("add bar opens the dialog", len(ui.opened) == k + 1)
# dialog failure → falls back to direct download
class Broken:
    def open(s, p): raise RuntimeError("no window")
mgr.info_ui = Broken(); n = len(mgr.items); pend = len(mgr.pending); d = mgr.request(B + "song.mp3")
check("dialog failure → download starts anyway", d is not None and len(mgr.items) == n + 1 and len(mgr.pending) == pend)
check("force_name persisted", pydm.Download.from_dict(json.loads(json.dumps(dict(mgr.items[1].to_dict())))).force_name == "مصطفى.mp4")
print("\nALL OK" if ok else "\nSOME FAILED")
