#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PyDM - مدير تحميل محمول
- لا يحتاج صلاحيات المدير ولا أي مكتبات خارجية (مكتبة بايثون القياسية فقط)
- تحميل متعدد الأجزاء + إيقاف/استئناف
- يستقبل الروابط من إضافة المتصفح عبر 127.0.0.1:9614
"""
import os
import sys
import re
import json
import time
import queue
import threading
import subprocess
import urllib.request
import urllib.parse
import urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, simpledialog, messagebox
except Exception:  # يسمح باختبار المحرك بدون واجهة
    tk = None

APP_DIR = os.path.join(os.path.expanduser("~"), ".pydm")
CONF_FILE = os.path.join(APP_DIR, "config.json")
LIST_FILE = os.path.join(APP_DIR, "downloads.json")
PORT = 9614
CHUNK = 64 * 1024
MIN_SPLIT = 2 * 1024 * 1024
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

incoming = queue.Queue()  # (url, headers) القادمة من الإضافة


# ----------------------------------------------------------------- أدوات
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def fmt_size(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def fmt_time(s):
    s = int(s)
    if s < 60:
        return f"{s}ث"
    if s < 3600:
        return f"{s // 60}د {s % 60}ث"
    return f"{s // 3600}س {(s % 3600) // 60}د"


def filename_from(content_disposition, url):
    name = None
    if content_disposition:
        m = re.search(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", content_disposition, re.I)
        if m:
            try:
                name = urllib.parse.unquote(m.group(2).strip().strip('"'),
                                            encoding=(m.group(1) or "utf-8"))
            except Exception:
                name = None
        if not name:
            m = re.search(r'filename\s*=\s*"?([^";]+)"?', content_disposition, re.I)
            if m:
                name = m.group(1).strip()
                try:  # http.client يفك الترويسات بـ latin-1
                    name = name.encode("latin-1").decode("utf-8")
                except Exception:
                    pass
    if not name:
        name = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(url).path))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(" .")
    return name or "download"


def unique_path(path):
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(path) or os.path.exists(path + ".part"):
        path = f"{base} ({i}){ext}"
        i += 1
    return path


class _Redirect(urllib.request.HTTPRedirectHandler):
    """لا نرسل الكوكيز إلى نطاق مختلف عند إعادة التوجيه."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            if urllib.parse.urlparse(newurl).netloc != urllib.parse.urlparse(req.full_url).netloc:
                for h in ("Cookie", "Authorization"):
                    new.remove_header(h)
        return new


_opener = urllib.request.build_opener(_Redirect)  # يلتقط إعدادات البروكسي من الويندوز تلقائياً


# ----------------------------------------------------------------- محرك التحميل
class Download:
    def __init__(self, url, folder, headers=None, conns=8):
        self.url = url
        self.folder = folder
        self.headers = dict(headers or {})
        self.conns = conns
        self.name = ""
        self.path = ""
        self.total = 0
        self.ranged = False
        self.segs = []  # [start, end(شامل), pos]
        self.status = "queued"
        self.error = ""
        self.speed = 0.0
        self.stop = threading.Event()
        self.thread = None
        self.failed = False
        self.cancelled = False

    # ---- حفظ/استرجاع
    def to_dict(self):
        return {k: getattr(self, k) for k in
                ("url", "folder", "headers", "conns", "name", "path", "total",
                 "ranged", "segs", "status", "error")}

    @classmethod
    def from_dict(cls, d):
        o = cls(d["url"], d["folder"], d.get("headers"), d.get("conns", 8))
        for k in ("name", "path", "total", "ranged", "segs", "error"):
            setattr(o, k, d.get(k, getattr(o, k)))
        st = d.get("status", "paused")
        o.status = st if st in ("done", "error") else "paused"
        return o

    @property
    def part(self):
        return self.path + ".part"

    def done_bytes(self):
        return sum(max(0, s[2] - s[0]) for s in self.segs)

    def percent(self):
        if self.status == "done":
            return 100
        return int(self.done_bytes() * 100 / self.total) if self.total else 0

    # ---- شبكة
    def _open(self, rng=None):
        h = {"User-Agent": DEFAULT_UA, "Accept": "*/*", "Accept-Encoding": "identity"}
        h.update(self.headers)
        if rng:
            h["Range"] = rng
        return _opener.open(urllib.request.Request(self.url, headers=h), timeout=15)

    def _probe(self):
        try:
            r = self._open("bytes=0-0")
        except urllib.error.HTTPError as e:
            if e.code != 416:
                raise
            r = self._open()
        with r:
            info = r.headers
            if r.status == 206:
                m = re.search(r"/(\d+)", info.get("Content-Range", ""))
                self.total = int(m.group(1)) if m else 0
                self.ranged = bool(self.total)
            else:
                self.total = int(info.get("Content-Length") or 0)
                self.ranged = False
            self.name = filename_from(info.get("Content-Disposition"), r.geturl() or self.url)

    def _prepare(self):
        os.makedirs(self.folder, exist_ok=True)
        self.path = unique_path(os.path.join(self.folder, self.name))
        self.name = os.path.basename(self.path)
        if self.ranged and self.total >= MIN_SPLIT:
            n = max(1, min(int(self.conns), 16))
            size, step = self.total, self.total // n
            self.segs = [[i * step, size - 1 if i == n - 1 else (i + 1) * step - 1, i * step]
                         for i in range(n)]
        elif self.ranged:
            self.segs = [[0, self.total - 1, 0]]
        else:
            self.segs = [[0, self.total - 1 if self.total else -1, 0]]
        self._make_part()

    def _make_part(self):
        with open(self.part, "wb") as f:
            if self.ranged:
                f.truncate(self.total)

    @staticmethod
    def _remaining(s):
        return s[1] < 0 or s[2] <= s[1]

    def _worker(self, seg, ev):
        retries = 0
        while not ev.is_set() and self._remaining(seg):
            before = seg[2]
            try:
                if not self.ranged:
                    seg[2] = seg[0]
                rng = f"bytes={seg[2]}-{seg[1]}" if self.ranged else None
                with self._open(rng) as r:
                    if self.ranged and r.status != 206:
                        raise IOError("الخادم لا يدعم التحميل المجزأ")
                    with open(self.part, "r+b" if self.ranged else "wb") as f:
                        f.seek(seg[2])
                        while not ev.is_set():
                            want = CHUNK if seg[1] < 0 else min(CHUNK, seg[1] - seg[2] + 1)
                            if want <= 0:
                                break
                            data = r.read(want)
                            if not data:
                                break
                            f.write(data)
                            seg[2] += len(data)
                if seg[1] < 0 and not ev.is_set():  # حجم غير معروف: انتهى البث
                    seg[1] = seg[2] - 1
                    self.total = seg[2]
                if seg[2] > before:
                    retries = 0
                elif not ev.is_set() and self._remaining(seg):
                    raise IOError("انقطع الاتصال")
            except Exception as e:
                if ev.is_set():
                    return
                retries += 1
                if retries > 6:
                    self.error = str(e)
                    self.failed = True
                    ev.set()
                    return
                time.sleep(min(2 * retries, 10))

    def _run(self, ev, prev):
        if prev is not None:
            prev.join()
        try:
            self.failed = False
            self.error = ""
            self.status = "downloading"
            if not self.segs:
                self._probe()
                if ev.is_set():
                    return
                self._prepare()
            elif not self.ranged:
                for s in self.segs:
                    s[2] = s[0]
            if not os.path.exists(self.part):  # الملف الجزئي مفقود: نبدأ من جديد
                for s in self.segs:
                    s[2] = s[0]
                self._make_part()
            workers = [threading.Thread(target=self._worker, args=(s, ev), daemon=True)
                       for s in self.segs if self._remaining(s)]
            for t in workers:
                t.start()
            last = (time.time(), self.done_bytes())
            while any(t.is_alive() for t in workers):
                time.sleep(0.5)
                now, d = time.time(), self.done_bytes()
                inst = (d - last[1]) / max(now - last[0], 1e-6)
                self.speed = inst if self.speed == 0 else self.speed * 0.6 + inst * 0.4
                last = (now, d)
            self.speed = 0
            if self.cancelled:
                self._rm_part()
                self.status = "canceled"
                return
            if self.failed:
                self.status = "error"
                return
            if ev.is_set():
                return  # إيقاف مؤقت
            if self.total and self.done_bytes() != self.total:
                raise IOError("حجم الملف غير مطابق")
            os.replace(self.part, self.path)
            self.status = "done"
        except Exception as e:
            self.error = str(e)
            self.status = "error"

    def _rm_part(self):
        try:
            if self.path and os.path.exists(self.part):
                os.remove(self.part)
        except Exception:
            pass

    # ---- تحكم
    def start(self):
        if self.status == "done":
            return
        if self.thread and self.thread.is_alive() and not self.stop.is_set():
            return
        prev = self.thread
        self.stop = threading.Event()
        self.cancelled = False
        self.status = "queued"
        self.thread = threading.Thread(target=self._run, args=(self.stop, prev), daemon=True)
        self.thread.start()

    def pause(self):
        if self.status in ("downloading", "queued"):
            self.status = "paused"
            self.stop.set()

    def cancel(self):
        self.cancelled = True
        self.stop.set()
        if self.status != "done":
            self.status = "canceled"
        if not (self.thread and self.thread.is_alive()):
            if self.status == "canceled":
                self._rm_part()


# ----------------------------------------------------------------- خادم محلي للإضافة
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _origin_ok(self):
        return self.headers.get("Origin", "").startswith(
            ("chrome-extension://", "moz-extension://", "extension://"))

    def _host_ok(self):
        return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

    def _send(self, code, obj=None):
        body = json.dumps(obj or {}).encode()
        self.send_response(code)
        origin = self.headers.get("Origin", "")
        if self._origin_ok():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        if not (self._host_ok() and self._origin_ok()):
            return self._send(403)
        self._send(204)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403)
        if self.path.startswith("/ping"):
            return self._send(200, {"ok": True, "app": "pydm"})
        self._send(404)

    def do_POST(self):
        # الأمان: نقبل الطلبات القادمة من إضافات المتصفح فقط، وليس من مواقع الويب
        if not (self._host_ok() and self._origin_ok()):
            return self._send(403)
        if self.path != "/add":
            return self._send(404)
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n <= 0 or n > 65536:
                return self._send(400)
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            url = str(data.get("url", ""))
            if urllib.parse.urlparse(url).scheme not in ("http", "https"):
                return self._send(400, {"error": "bad url"})
            hdrs = {k: str(v) for k, v in (data.get("headers") or {}).items()
                    if k in ("Referer", "Cookie", "User-Agent")}
            incoming.put((url, hdrs))
            self._send(200, {"ok": True})
        except Exception:
            self._send(400)


# ----------------------------------------------------------------- الواجهة
STATUS_AR = {"queued": "في الانتظار", "downloading": "جارٍ التحميل", "paused": "متوقف مؤقتاً",
             "done": "اكتمل ✔", "error": "خطأ", "canceled": "ملغى"}


class App:
    def __init__(self, root):
        self.root = root
        root.title("PyDM - مدير التحميل")
        root.geometry("940x440")
        default_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        self.cfg = load_json(CONF_FILE, {"folder": default_dir, "conns": 8})
        self.items = []
        self.by_iid = {}
        self.ticks = 0
        self._build()
        for d in load_json(LIST_FILE, []):
            try:
                self._insert(Download.from_dict(d))
            except Exception:
                pass
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.tick()

    def _build(self):
        bar = ttk.Frame(self.root, padding=6)
        bar.pack(fill="x")
        for text, cmd in (("➕ إضافة رابط", self.ask_url), ("⏯ إيقاف / استئناف", self.toggle),
                          ("✖ إلغاء", self.cancel_sel), ("🗑 إزالة من القائمة", self.remove_sel),
                          ("📂 فتح المجلد", self.open_folder), ("⚙ مجلد التحميل", self.pick_folder)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=3)
        ttk.Label(bar, text="  عدد الاتصالات:").pack(side="left")
        self.conns = tk.IntVar(value=int(self.cfg.get("conns", 8)))
        ttk.Spinbox(bar, from_=1, to=16, width=3, textvariable=self.conns).pack(side="left")

        cols = ("name", "size", "progress", "speed", "eta", "status")
        heads = ("الملف", "الحجم", "التقدم", "السرعة", "المتبقي", "الحالة")
        widths = (330, 80, 170, 90, 70, 130)
        self.tree = ttk.Treeview(self.root, columns=cols, show="headings", selectmode="extended")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c == "name" else "center")
        sb = ttk.Scrollbar(self.root, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.open_selected)

    # ---- إدارة القائمة
    def _insert(self, d):
        self.items.append(d)
        iid = str(id(d))
        self.by_iid[iid] = d
        self.tree.insert("", "end", iid=iid, values=("", "", "", "", "", ""))
        self._update_row(d)
        return d

    def add(self, url, headers=None):
        d = Download(url, self.cfg["folder"], headers, int(self.conns.get()))
        self._insert(d)
        d.start()

    def selected(self):
        return [self.by_iid[i] for i in self.tree.selection() if i in self.by_iid]

    def _update_row(self, d):
        iid = str(id(d))
        if not self.tree.exists(iid):
            return
        done = d.done_bytes()
        eta = ""
        if d.status == "downloading" and d.speed > 1 and d.total:
            eta = fmt_time((d.total - done) / d.speed)
        p = d.percent()
        bar = "█" * (p // 10) + "░" * (10 - p // 10) + f" {p}%"
        status = STATUS_AR.get(d.status, d.status)
        if d.status == "error" and d.error:
            status += f": {d.error[:40]}"
        self.tree.item(iid, values=(
            d.name or d.url[:60],
            fmt_size(d.total) if d.total else "—",
            bar if d.segs or d.status == "done" else "",
            (fmt_size(d.speed) + "/ث") if d.status == "downloading" and d.speed else "",
            eta, status))

    # ---- أزرار
    def ask_url(self):
        clip = ""
        try:
            clip = self.root.clipboard_get().strip()
        except Exception:
            pass
        s = simpledialog.askstring("إضافة رابط", "الصق الرابط (أو عدة روابط):",
                                   initialvalue=clip if clip.startswith("http") else "",
                                   parent=self.root)
        for url in re.findall(r"https?://\S+", s or ""):
            self.add(url)

    def toggle(self):
        for d in self.selected():
            if d.status in ("downloading", "queued"):
                d.pause()
            elif d.status in ("paused", "error"):
                d.start()

    def cancel_sel(self):
        for d in self.selected():
            if d.status != "done":
                d.cancel()

    def remove_sel(self):
        for d in self.selected():
            if d.status != "done":
                d.cancel()
            iid = str(id(d))
            self.tree.delete(iid)
            self.by_iid.pop(iid, None)
            self.items.remove(d)

    def open_folder(self):
        sel = self.selected()
        folder = os.path.dirname(sel[0].path) if sel and sel[0].path else self.cfg["folder"]
        self._open(folder)

    def open_selected(self, _e=None):
        sel = self.selected()
        if sel and sel[0].status == "done":
            self._open(sel[0].path)

    @staticmethod
    def _open(path):
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            pass

    def pick_folder(self):
        f = filedialog.askdirectory(initialdir=self.cfg["folder"], title="اختر مجلد التحميل")
        if f:
            self.cfg["folder"] = f
            self.save()

    # ---- دورة التحديث
    def tick(self):
        try:
            while True:
                url, hdrs = incoming.get_nowait()
                self.add(url, hdrs)
        except queue.Empty:
            pass
        for d in list(self.items):
            if d.status == "canceled" and not (d.thread and d.thread.is_alive()):
                iid = str(id(d))
                if self.tree.exists(iid):
                    self.tree.delete(iid)
                self.by_iid.pop(iid, None)
                self.items.remove(d)
                continue
            self._update_row(d)
        self.ticks += 1
        if self.ticks % 6 == 0:
            self.save()
        self.root.after(500, self.tick)

    def save(self):
        self.cfg["conns"] = int(self.conns.get())
        save_json(CONF_FILE, self.cfg)
        save_json(LIST_FILE, [d.to_dict() for d in self.items if d.status != "canceled"])

    def on_close(self):
        for d in self.items:
            d.pause()
        time.sleep(0.2)
        self.save()
        self.root.destroy()


def main():
    if tk is None:
        print("tkinter غير متوفر. ثبّت بايثون من python.org (للمستخدم الحالي فقط).")
        sys.exit(1)
    os.makedirs(APP_DIR, exist_ok=True)
    root = tk.Tk()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        messagebox.showerror("PyDM", "البرنامج يعمل بالفعل (المنفذ %d مشغول)." % PORT)
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
