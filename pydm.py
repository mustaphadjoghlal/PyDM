#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PyDM - مدير تحميل محمول
- لا يحتاج صلاحيات المدير. المحرك من مكتبة بايثون القياسية، والواجهة صفحة ويب داخل نافذة (pywebview)
- تحميل متعدد الأجزاء + إيقاف/استئناف
- يدعم روابط HLS (m3u8) مباشرة، و DASH (mpd) والمشفّر عبر ffmpeg إن وُجد
- مواقع الفيديو (يوتيوب وغيره) عبر yt-dlp إن وُجد (yt-dlp.exe بجانب البرنامج)
- يستقبل الروابط من إضافة المتصفح عبر 127.0.0.1:9614
"""
import os
import sys
import re
import json
import time
import queue
import shutil
import importlib.util
import threading
import subprocess
import urllib.request
import urllib.parse
import urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler


APP_DIR = os.path.join(os.path.expanduser("~"), ".pydm")
CONF_FILE = os.path.join(APP_DIR, "config.json")
LIST_FILE = os.path.join(APP_DIR, "downloads.json")
PORT = 9614
CHUNK = 64 * 1024
MIN_SPLIT = 2 * 1024 * 1024
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # إخفاء نافذة ffmpeg في ويندوز

incoming = queue.Queue()  # (url, headers, meta) القادمة من الإضافة
UI = {"ready": False, "window": None}  # حالة النافذة: يُستخدم في /ping و /show


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


def safe_name(s):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s or "").strip(" .")[:120]


def unique_path(path):
    base, ext = os.path.splitext(path)
    i = 1
    while (os.path.exists(path) or os.path.exists(path + ".part")
           or os.path.exists(path + ".parts")):
        path = f"{base} ({i}){ext}"
        i += 1
    return path


def is_stream_url(url):
    return bool(re.search(r"\.(m3u8|mpd)$", urllib.parse.urlparse(url).path, re.I))


def find_ffmpeg():
    """ffmpeg اختياري: بجانب البرنامج، أو في ~/.pydm، أو في PATH."""
    dirs = []
    try:
        dirs.append(os.path.dirname(os.path.abspath(sys.argv[0])))
    except Exception:
        pass
    if sys.executable:
        dirs.append(os.path.dirname(sys.executable))
    dirs.append(APP_DIR)
    for d in dirs:
        for n in ("ffmpeg.exe", "ffmpeg"):
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    return shutil.which("ffmpeg")


def find_ytdlp():
    """yt-dlp اختياري: ملف بجانب البرنامج أو في ~/.pydm أو PATH، وإلا وحدة بايثون عند التشغيل من المصدر.
    يعيد قائمة الأمر (أو None)."""
    dirs = []
    try:
        dirs.append(os.path.dirname(os.path.abspath(sys.argv[0])))
    except Exception:
        pass
    if sys.executable:
        dirs.append(os.path.dirname(sys.executable))
    dirs.append(APP_DIR)
    for d in dirs:
        for n in ("yt-dlp.exe", "yt-dlp"):
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return [p]
    p = shutil.which("yt-dlp")
    if p:
        return [p]
    if not getattr(sys, "frozen", False) and importlib.util.find_spec("yt_dlp"):
        return [sys.executable, "-m", "yt_dlp"]
    return None


YTDLP_HOSTS = re.compile(
    r"(^|\.)(youtube\.com|youtu\.be|facebook\.com|fb\.watch|instagram\.com|tiktok\.com|"
    r"twitter\.com|x\.com|vimeo\.com|dailymotion\.com|twitch\.tv|soundcloud\.com|reddit\.com)$", re.I)


def is_ytdlp_url(url):
    """صفحة فيديو في موقع معروف (وليس ملفاً مباشراً)."""
    p = urllib.parse.urlparse(url)
    if not YTDLP_HOSTS.search(p.hostname or ""):
        return False
    return not re.search(r"\.(mp4|webm|m4a|mp3|jpe?g|png|gif|m3u8|mpd)$", p.path, re.I)


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

    def has_progress(self):
        return bool(self.segs)

    def title_or_url(self):
        return self.url[:60]

    def percent(self):
        if self.status == "done":
            return 100
        return int(self.done_bytes() * 100 / self.total) if self.total else 0

    # ---- شبكة
    def _open(self, rng=None, url=None):
        h = {"User-Agent": DEFAULT_UA, "Accept": "*/*", "Accept-Encoding": "identity"}
        h.update(self.headers)
        if rng:
            h["Range"] = rng
        return _opener.open(urllib.request.Request(url or self.url, headers=h), timeout=15)

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


# ----------------------------------------------------------------- البث (HLS / DASH)
def parse_m3u8(text, base):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise IOError("الرابط ليس قائمة m3u8 صالحة")
    out = {"variants": [], "segments": [], "encrypted": False, "endlist": False,
           "byterange": False, "map": None, "audio_group": False, "audio_uri": False}
    pending_bw = None
    for l in lines:
        if l.startswith("#"):
            if l.startswith("#EXT-X-STREAM-INF"):
                m = re.search(r"BANDWIDTH=(\d+)", l)
                pending_bw = int(m.group(1)) if m else 0
                if 'AUDIO="' in l:
                    out["audio_group"] = True
            elif l.startswith("#EXT-X-MEDIA") and "TYPE=AUDIO" in l and 'URI="' in l:
                out["audio_uri"] = True
            elif l.startswith("#EXT-X-KEY"):
                if "METHOD=NONE" not in l:
                    out["encrypted"] = True
            elif l.startswith("#EXT-X-MAP"):
                m = re.search(r'URI="([^"]+)"', l)
                if m:
                    out["map"] = urllib.parse.urljoin(base, m.group(1))
            elif l.startswith("#EXT-X-BYTERANGE"):
                out["byterange"] = True
            elif l.startswith("#EXT-X-ENDLIST"):
                out["endlist"] = True
            continue
        uri = urllib.parse.urljoin(base, l)
        if pending_bw is not None:
            out["variants"].append((pending_bw, uri))
            pending_bw = None
        else:
            out["segments"].append(uri)
    return out


class StreamDownload(Download):
    """HLS غير مشفّر: تحميل القطع بالتوازي ثم دمجها (ويُحوَّل إلى mp4 إن وُجد ffmpeg).
    DASH (mpd) والمشفّر وذو الصوت المنفصل: عبر ffmpeg فقط."""

    def __init__(self, url, folder, headers=None, conns=8, title=""):
        super().__init__(url, folder, headers, conns)
        self.title = title
        self.mode = ""  # "segments" | "ffmpeg"
        self.seg_urls = []
        self.seg_done = []
        self.workdir = ""
        self.bytes_done = 0
        self.proc = None
        self._blk = threading.Lock()

    def to_dict(self):
        d = super().to_dict()
        d.update(kind="stream", title=self.title, mode=self.mode, seg_urls=self.seg_urls,
                 seg_done=self.seg_done, workdir=self.workdir)
        return d

    @classmethod
    def from_dict(cls, d):
        o = super().from_dict(d)
        o.title = d.get("title", "")
        o.mode = d.get("mode", "")
        o.seg_urls = d.get("seg_urls", [])
        o.seg_done = d.get("seg_done", [])
        o.workdir = d.get("workdir", "")
        if o.status == "done":
            o.bytes_done = o.total
        return o

    def done_bytes(self):
        return self.bytes_done

    def has_progress(self):
        return self.mode == "segments" and bool(self.seg_urls)

    def title_or_url(self):
        return self.title or self.url[:60]

    def percent(self):
        if self.status == "done":
            return 100
        n = len(self.seg_urls)
        return int(sum(self.seg_done) * 100 / n) if n and self.mode == "segments" else 0

    # ---- تحليل الرابط
    def _fetch_text(self, url):
        with self._open(url=url) as r:
            return r.read(4 * 1024 * 1024).decode("utf-8", "replace"), (r.geturl() or url)

    def _resolve(self):
        if self.title:
            stem = safe_name(self.title)
        else:
            p = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(self.url).path))
            stem = safe_name(os.path.splitext(p)[0])
        if stem.lower() in ("", "index", "master", "playlist", "manifest", "stream",
                            "chunklist", "main"):
            stem = "video"

        url, pl, need_ffmpeg = self.url, None, False
        for _ in range(3):
            text, url = self._fetch_text(url)
            if "<MPD" in text[:4096]:
                need_ffmpeg, pl = True, None
                break
            pl = parse_m3u8(text, url)
            if not pl["variants"]:
                break
            if pl["audio_group"] and pl["audio_uri"]:
                need_ffmpeg = True  # صوت منفصل: يحتاج دمجاً
                break
            url = max(pl["variants"])[1]  # أعلى جودة

        ext = ".mp4"
        if not need_ffmpeg:
            if pl is None or not pl["segments"]:
                raise IOError("قائمة التشغيل فارغة")
            if not pl["endlist"]:
                raise IOError("البث المباشر غير مدعوم")
            if pl["encrypted"] or pl["byterange"]:
                need_ffmpeg = True

        os.makedirs(self.folder, exist_ok=True)
        if need_ffmpeg:
            self.mode = "ffmpeg"
        else:
            self.mode = "segments"
            segs = ([pl["map"]] if pl["map"] else []) + pl["segments"]
            self.seg_urls = segs
            self.seg_done = [False] * len(segs)
            ext = ".mp4" if pl["map"] else ".ts"
        self.path = unique_path(os.path.join(self.folder, stem + ext))
        self.name = os.path.basename(self.path)
        if self.mode == "segments":
            self.workdir = self.path + ".parts"
            os.makedirs(self.workdir, exist_ok=True)
        else:
            open(self.path + ".part", "wb").close()  # حجز الاسم

    # ---- قطع HLS
    def _seg_file(self, i):
        return os.path.join(self.workdir, f"{i:06d}.seg")

    def _get_seg(self, i, ev):
        dst = self._seg_file(i)
        tmp = dst + ".tmp"
        for attempt in range(6):
            got = 0
            try:
                with self._open(url=self.seg_urls[i]) as r, open(tmp, "wb") as f:
                    while not ev.is_set():
                        data = r.read(CHUNK)
                        if not data:
                            break
                        f.write(data)
                        got += len(data)
                        with self._blk:
                            self.bytes_done += len(data)
                    else:
                        raise InterruptedError()
                if got == 0:
                    raise IOError("قطعة فارغة")
                os.replace(tmp, dst)
                self.seg_done[i] = True
                return True
            except Exception as e:
                with self._blk:
                    self.bytes_done -= got
                if ev.is_set():
                    return False
                if attempt == 5:
                    self.error = f"فشلت القطعة {i + 1}: {e}"
                    self.failed = True
                    ev.set()
                    return False
                time.sleep(min(2 * (attempt + 1), 8))
        return False

    def _seg_worker(self, ev, todo, lock):
        while not ev.is_set():
            with lock:
                if not todo:
                    return
                i = todo.pop(0)
            if not self._get_seg(i, ev):
                return

    def _watch(self, ev, alive, estimate=None):
        """حلقة المراقبة: حساب السرعة حتى تنتهي الخيوط/العملية."""
        last = (time.time(), self.bytes_done)
        while alive():
            time.sleep(0.5)
            if estimate:
                estimate()
            now, d = time.time(), self.bytes_done
            inst = (d - last[1]) / max(now - last[0], 1e-6)
            self.speed = inst if self.speed == 0 else self.speed * 0.6 + inst * 0.4
            last = (now, d)
        self.speed = 0

    def _run_segments(self, ev):
        os.makedirs(self.workdir, exist_ok=True)
        n = len(self.seg_urls)
        self.bytes_done = 0
        for i in range(n):  # استئناف: نتحقق من القطع الموجودة فعلاً
            ok = self.seg_done[i] and os.path.exists(self._seg_file(i))
            self.seg_done[i] = bool(ok)
            if ok:
                self.bytes_done += os.path.getsize(self._seg_file(i))
        todo = [i for i in range(n) if not self.seg_done[i]]
        lock = threading.Lock()
        k = max(1, min(int(self.conns), 8, len(todo)))
        workers = [threading.Thread(target=self._seg_worker, args=(ev, todo, lock), daemon=True)
                   for _ in range(k)]
        for t in workers:
            t.start()

        def estimate():
            c = sum(self.seg_done)
            if c:
                self.total = int(self.bytes_done * n / c)

        self._watch(ev, lambda: any(t.is_alive() for t in workers), estimate)
        if self.cancelled:
            self._rm_part()
            self.status = "canceled"
            return
        if self.failed:
            self.status = "error"
            return
        if ev.is_set():
            return  # إيقاف مؤقت
        if not all(self.seg_done):
            raise IOError("بعض القطع ناقصة")
        self._assemble()

    def _assemble(self):
        allf = os.path.join(self.workdir, "_all")
        with open(allf, "wb") as out:
            for i in range(len(self.seg_urls)):
                with open(self._seg_file(i), "rb") as f:
                    shutil.copyfileobj(f, out, 1024 * 1024)
        final = self.path
        exe = find_ffmpeg() if self.path.lower().endswith(".ts") else None
        if exe:  # تحويل .ts إلى .mp4 بدون إعادة ترميز
            mp4 = unique_path(self.path[:-3] + ".mp4")
            try:
                r = subprocess.run(
                    [exe, "-y", "-hide_banner", "-loglevel", "error", "-f", "mpegts",
                     "-i", allf, "-c", "copy", "-f", "mp4", mp4],
                    creationflags=NOWIN, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=7200)
                if r.returncode == 0 and os.path.exists(mp4) and os.path.getsize(mp4) > 0:
                    final = mp4
                    os.remove(allf)
                elif os.path.exists(mp4):
                    os.remove(mp4)
            except Exception:
                if os.path.exists(mp4):
                    os.remove(mp4)
        if final == self.path:
            os.replace(allf, final)
        self.path = final
        self.name = os.path.basename(final)
        shutil.rmtree(self.workdir, ignore_errors=True)
        self.total = os.path.getsize(final)
        self.bytes_done = self.total
        self.status = "done"

    # ---- ffmpeg
    def _run_ffmpeg(self, ev):
        tmp = self.path + ".part"
        exe = find_ffmpeg()
        if not exe:
            self._rm_part()
            raise IOError("يحتاج ffmpeg: ضع ffmpeg.exe بجانب PyDM.exe")
        hdr = "".join(f"{k}: {v}\r\n" for k, v in self.headers.items() if k.lower() != "user-agent")
        cmd = [exe, "-y", "-hide_banner", "-loglevel", "error",
               "-user_agent", self.headers.get("User-Agent", DEFAULT_UA)]
        if hdr:
            cmd += ["-headers", hdr]
        cmd += ["-i", self.url, "-c", "copy", "-f", "mp4", tmp]
        self.bytes_done = 0
        proc = subprocess.Popen(cmd, creationflags=NOWIN, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.proc = proc

        def alive():
            if ev.is_set() and proc.poll() is None:
                proc.kill()
            return proc.poll() is None

        def size():
            try:
                self.bytes_done = os.path.getsize(tmp)
            except OSError:
                pass

        self._watch(ev, alive, size)
        err = proc.stderr.read().decode("utf-8", "replace").strip()
        proc.stderr.close()
        self.proc = None
        if self.cancelled:
            self._rm_part()
            self.status = "canceled"
            return
        if ev.is_set():  # إيقاف مؤقت: لا استئناف جزئي مع ffmpeg
            if os.path.exists(tmp):
                open(tmp, "wb").close()
            self.bytes_done = 0
            return
        if proc.returncode != 0:
            self._rm_part()
            last = err.splitlines()[-1] if err else f"رمز الخروج {proc.returncode}"
            raise IOError("ffmpeg: " + last[:120])
        os.replace(tmp, self.path)
        self.total = os.path.getsize(self.path)
        self.bytes_done = self.total
        self.status = "done"

    # ---- تشغيل
    def _run(self, ev, prev):
        if prev is not None:
            prev.join()
        try:
            self.failed = False
            self.error = ""
            self.status = "downloading"
            if not self.mode:
                self._resolve()
                if ev.is_set():
                    if self.cancelled:
                        self._rm_part()
                    return
            if self.mode == "ffmpeg":
                self._run_ffmpeg(ev)
            else:
                self._run_segments(ev)
        except Exception as e:
            self.error = str(e)
            self.status = "error"

    def _rm_part(self):
        try:
            if self.workdir:
                shutil.rmtree(self.workdir, ignore_errors=True)
            if self.path and os.path.exists(self.path + ".part"):
                os.remove(self.path + ".part")
        except Exception:
            pass


class YtdlpDownload(Download):
    """مواقع الفيديو (يوتيوب، فيسبوك، تيك توك ...) عبر yt-dlp. الإيقاف المؤقت يوقف العملية،
    والاستئناف يعيد تشغيلها فيكمل yt-dlp من الملفات الجزئية."""

    TEMPLATE = ("download:PYDM %(progress.downloaded_bytes)s %(progress.total_bytes)s "
                "%(progress.total_bytes_estimate)s %(progress.speed)s")

    def __init__(self, url, folder, headers=None, conns=8, title="", media="video"):
        super().__init__(url, folder, headers, conns)
        self.title = title
        self.media = "audio" if media == "audio" else "video"
        self.bytes_done = 0
        self.proc = None

    def to_dict(self):
        d = super().to_dict()
        d.update(kind="ytdlp", title=self.title, media=self.media)
        return d

    @classmethod
    def from_dict(cls, d):
        o = super().from_dict(d)
        o.title = d.get("title", "")
        o.media = "audio" if d.get("media") == "audio" else "video"
        if o.status == "done":
            o.bytes_done = o.total
        return o

    def done_bytes(self):
        return self.bytes_done

    def has_progress(self):
        return self.total > 0

    def title_or_url(self):
        return self.title or self.url[:60]

    def percent(self):
        if self.status == "done":
            return 100
        return min(99, int(self.bytes_done * 100 / self.total)) if self.total else 0

    @staticmethod
    def _num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0.0

    def _parse(self, line):
        if line.startswith("PYDM "):
            f = line.split()[1:5]
            if len(f) == 4:
                self.bytes_done = int(self._num(f[0]))
                self.total = int(self._num(f[1]) or self._num(f[2]))
                self.speed = self._num(f[3])
        elif line.startswith("PYDMFILE "):
            self.path = line[9:].strip()
            self.name = os.path.basename(self.path)
        elif line.startswith("[download] Destination:"):
            self.path = line.split(":", 1)[1].strip()
            self.name = os.path.basename(self.path)
        elif line.startswith("ERROR:"):
            self.error = line[6:].strip()[:160]

    def _command(self):
        exe = find_ytdlp()
        if not exe:
            raise IOError("يحتاج yt-dlp: ضع yt-dlp.exe بجانب PyDM.exe")
        ff = find_ffmpeg()
        tpl = os.path.join(self.folder.replace("%", "%%"), "%(title).150B.%(ext)s")
        cmd = exe + ["--no-playlist", "--newline", "--no-colors", "--progress",
                     "--progress-template", self.TEMPLATE,
                     "--print", "after_move:PYDMFILE %(filepath)s", "-o", tpl,
                     "--user-agent", self.headers.get("User-Agent", DEFAULT_UA)]
        for k, v in self.headers.items():
            if k.lower() != "user-agent":
                cmd += ["--add-header", f"{k}:{v}"]
        if ff:
            cmd += ["--ffmpeg-location", ff]
        if self.media == "audio":
            cmd += ["-x", "--audio-format", "mp3"] if ff else ["-f", "ba/b"]
        else:
            cmd += ["-f", "bv*+ba/b", "--merge-output-format", "mp4"] if ff else ["-f", "b"]
        return cmd + [self.url]

    def _run(self, ev, prev):
        if prev is not None:
            prev.join()
        try:
            self.failed = False
            self.error = ""
            self.status = "downloading"
            os.makedirs(self.folder, exist_ok=True)
            cmd = self._command()
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            self.bytes_done = 0
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, creationflags=NOWIN, env=env)
            self.proc = proc

            def reader():
                for raw in proc.stdout:
                    self._parse(raw.decode("utf-8", "replace").strip())

            t = threading.Thread(target=reader, daemon=True)
            t.start()
            while proc.poll() is None:
                if ev.is_set():
                    proc.kill()
                    break
                time.sleep(0.3)
            proc.wait()
            t.join(3)
            self.proc = None
            self.speed = 0
            if self.cancelled:
                self._rm_part()
                self.status = "canceled"
                return
            if ev.is_set():
                return  # إيقاف مؤقت
            if proc.returncode != 0:
                raise IOError(self.error or f"yt-dlp رمز الخروج {proc.returncode}")
            if not (self.path and os.path.isfile(self.path)):
                raise IOError("لم يُنشأ الملف")
            self.total = os.path.getsize(self.path)
            self.bytes_done = self.total
            self.status = "done"
        except Exception as e:
            self.error = str(e)
            self.status = "error"

    def _rm_part(self):
        try:
            for ext in (".part", ".ytdl"):
                if self.path and os.path.exists(self.path + ext):
                    os.remove(self.path + ext)
        except Exception:
            pass


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
            return self._send(200, {"ok": True, "app": "pydm", "ui": UI["ready"]})
        if self.path.startswith("/show"):  # تشغيل ثانٍ للبرنامج: نُظهر النافذة الموجودة
            show_window()
            return self._send(200, {"ok": True})
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
            kind = data.get("kind")
            meta = {"kind": kind if kind in ("stream", "ytdlp") else "",
                    "mode": "audio" if data.get("mode") == "audio" else "video",
                    "title": str(data.get("title", ""))[:200]}
            incoming.put((url, hdrs, meta))
            self._send(200, {"ok": True})
        except Exception:
            self._send(400)


# ----------------------------------------------------------------- إدارة القائمة (بلا واجهة)
VIDEO_EXT = {"mp4", "mkv", "webm", "mov", "avi", "flv", "wmv", "m4v", "3gp", "ts"}
AUDIO_EXT = {"mp3", "m4a", "aac", "ogg", "opus", "wav", "flac", "wma", "weba"}
ARCHIVE_EXT = {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso"}
APP_EXT = {"exe", "msi", "apk", "dmg", "appx", "msix"}
DOC_EXT = {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "epub", "csv"}
IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "webp", "svg", "psd", "ai", "bmp", "heic"}


def category_of(d):
    if isinstance(d, YtdlpDownload):
        return "audio" if d.media == "audio" else "video"
    if isinstance(d, StreamDownload):
        return "video"
    ext = os.path.splitext(d.name or urllib.parse.urlparse(d.url).path)[1].lower().lstrip(".")
    for cat, exts in (("video", VIDEO_EXT), ("audio", AUDIO_EXT), ("archive", ARCHIVE_EXT),
                      ("app", APP_EXT), ("doc", DOC_EXT), ("image", IMAGE_EXT)):
        if ext in exts:
            return cat
    return "other"


def source_of(d):
    if isinstance(d, YtdlpDownload):
        return (urllib.parse.urlparse(d.url).hostname or "").replace("www.", "")
    if isinstance(d, StreamDownload):
        return "HLS / DASH"
    return (urllib.parse.urlparse(d.url).hostname or "").replace("www.", "")


def open_path(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception:
        return False


def reveal_path(path):
    """فتح المجلد مع تحديد الملف."""
    if sys.platform.startswith("win") and path and os.path.exists(path):
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            return True
        except Exception:
            pass
    return open_path(os.path.dirname(path) if path and os.path.isfile(path) else path)


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def startup_get():
    if not sys.platform.startswith("win"):
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "PyDM")
            return True
    except Exception:
        return False


def startup_set(on):
    """تشغيل مع الويندوز عبر HKCU: لا يحتاج صلاحيات مدير."""
    if not sys.platform.startswith("win"):
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                exe = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(sys.argv[0])
                winreg.SetValueEx(k, "PyDM", 0, winreg.REG_SZ, f'"{exe}"')
            else:
                try:
                    winreg.DeleteValue(k, "PyDM")
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False


def clipboard_text():
    """نص الحافظة في ويندوز عبر ctypes (بلا مكتبات)."""
    if not sys.platform.startswith("win"):
        return ""
    try:
        import ctypes
        from ctypes import wintypes
        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        u32.GetClipboardData.restype = wintypes.HANDLE
        k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalLock.restype = ctypes.c_void_p
        k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        if not u32.OpenClipboard(None):
            return ""
        try:
            h = u32.GetClipboardData(13)  # CF_UNICODETEXT
            if not h:
                return ""
            ptr = k32.GlobalLock(h)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)[:4000]
            finally:
                k32.GlobalUnlock(h)
        finally:
            u32.CloseClipboard()
    except Exception:
        return ""


class Manager:
    def __init__(self):
        default_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        self.cfg = load_json(CONF_FILE, {})
        self.cfg.setdefault("folder", default_dir)
        self.cfg.setdefault("conns", 8)
        self.items = []
        self.lock = threading.RLock()
        self._uid = 0
        for d in load_json(LIST_FILE, []):
            try:
                cls = {"stream": StreamDownload, "ytdlp": YtdlpDownload}.get(d.get("kind"), Download)
                self._insert(cls.from_dict(d))
            except Exception:
                pass

    def _insert(self, d):
        self._uid += 1
        d.uid = str(self._uid)
        d.added = getattr(d, "added", time.time())
        self.items.append(d)
        return d

    def add(self, url, headers=None, meta=None):
        meta = meta or {}
        conns = int(self.cfg.get("conns", 8))
        folder = self.cfg["folder"]
        kind = meta.get("kind")
        if kind == "ytdlp" or (kind != "stream" and is_ytdlp_url(url)):
            d = YtdlpDownload(url, folder, headers, conns, meta.get("title", ""), meta.get("mode", "video"))
        elif kind == "stream" or is_stream_url(url):
            d = StreamDownload(url, folder, headers, conns, meta.get("title", ""))
        else:
            d = Download(url, folder, headers, conns)
        with self.lock:
            self._insert(d)
        d.start()
        return d

    def get(self, uid):
        with self.lock:
            for d in self.items:
                if d.uid == uid:
                    return d
        return None

    def remove(self, uid, delete_file=False):
        d = self.get(uid)
        if not d:
            return False
        if d.status != "done":
            d.cancel()
        elif delete_file and d.path and os.path.isfile(d.path):
            try:
                os.remove(d.path)
            except Exception:
                pass
        with self.lock:
            if d in self.items:
                self.items.remove(d)
        return True

    def pump(self):
        """تُستدعى دورياً: روابط الإضافة + تنظيف الملغى."""
        added = 0
        try:
            while True:
                url, hdrs, meta = incoming.get_nowait()
                self.add(url, hdrs, meta)
                added += 1
        except queue.Empty:
            pass
        with self.lock:
            for d in list(self.items):
                if d.status == "canceled" and not (d.thread and d.thread.is_alive()):
                    self.items.remove(d)
        return added

    def row(self, d):
        done = d.done_bytes()
        eta = 0
        if d.status == "downloading" and d.speed > 1 and d.total:
            eta = max(0, d.total - done) / d.speed
        return {
            "id": d.uid, "name": d.name or d.title_or_url(), "url": d.url,
            "status": d.status, "error": d.error, "category": category_of(d),
            "source": source_of(d), "percent": d.percent(),
            "indeterminate": d.status == "downloading" and not d.has_progress(),
            "done": done, "total": d.total, "speed": d.speed if d.status == "downloading" else 0,
            "eta": eta, "path": d.path, "added": d.added,
        }

    def snapshot(self):
        with self.lock:
            items = list(self.items)
        rows = [self.row(d) for d in reversed(items)]
        return {
            "items": rows,
            "speed": sum(r["speed"] for r in rows),
            "settings": {"folder": self.cfg["folder"], "conns": int(self.cfg.get("conns", 8)),
                         "startup": startup_get()},
            "tools": {"ytdlp": bool(find_ytdlp()), "ffmpeg": bool(find_ffmpeg())},
        }

    def save(self):
        with self.lock:
            data = [d.to_dict() for d in self.items if d.status != "canceled"]
        save_json(CONF_FILE, self.cfg)
        save_json(LIST_FILE, data)

    def shutdown(self):
        with self.lock:
            items = list(self.items)
        for d in items:
            d.pause()
        time.sleep(0.2)
        self.save()


class Api:
    """الدوال التي تستدعيها الواجهة عبر window.pywebview.api."""

    def __init__(self, mgr):
        self._mgr = mgr

    def ready(self):
        UI["ready"] = True
        return True

    def state(self):
        self._mgr.pump()
        return self._mgr.snapshot()

    def clipboard(self):
        m = re.search(r"https?://\S+", clipboard_text())
        return m.group(0) if m else ""

    def detect(self, url):
        url = (url or "").strip()
        if is_ytdlp_url(url):
            return "site"
        if is_stream_url(url):
            return "stream"
        return "file" if url.startswith(("http://", "https://")) else ""

    def add(self, text, mode="video"):
        urls = re.findall(r"https?://[^\s\"'<>]+", text or "")
        for url in urls:
            self._mgr.add(url, None, {"mode": "audio" if mode == "audio" else "video"})
        return len(urls)

    def _with(self, uid, fn):
        d = self._mgr.get(str(uid))
        if not d:
            return False
        fn(d)
        return True

    def pause(self, uid):
        return self._with(uid, lambda d: d.pause())

    def resume(self, uid):
        return self._with(uid, lambda d: d.start() if d.status in ("paused", "error") else None)

    def remove(self, uid, delete_file=False):
        return self._mgr.remove(str(uid), bool(delete_file))

    def open_file(self, uid):
        d = self._mgr.get(str(uid))
        return bool(d and d.status == "done" and open_path(d.path))

    def open_folder(self, uid=None):
        d = self._mgr.get(str(uid)) if uid else None
        if d and d.path:
            return reveal_path(d.path if os.path.exists(d.path) else os.path.dirname(d.path))
        return open_path(self._mgr.cfg["folder"])

    def pause_all(self):
        for d in list(self._mgr.items):
            d.pause()
        return True

    def resume_all(self):
        for d in list(self._mgr.items):
            if d.status == "paused":
                d.start()
        return True

    def clear_done(self):
        with self._mgr.lock:
            self._mgr.items = [d for d in self._mgr.items if d.status != "done"]
        return True

    def choose_folder(self):
        w = UI["window"]
        if w is None:
            return ""
        import webview
        res = w.create_file_dialog(webview.FOLDER_DIALOG, directory=self._mgr.cfg["folder"])
        if res:
            self._mgr.cfg["folder"] = res[0] if isinstance(res, (list, tuple)) else res
            self._mgr.save()
            return self._mgr.cfg["folder"]
        return ""

    def set_conns(self, n):
        try:
            self._mgr.cfg["conns"] = max(1, min(16, int(n)))
        except (TypeError, ValueError):
            return False
        self._mgr.save()
        return True

    def set_startup(self, on):
        return startup_set(bool(on))


def show_window():
    w = UI["window"]
    if w is None:
        return
    try:
        w.restore()
        w.show()
        w.on_top = True
        w.on_top = False
    except Exception:
        pass


# ----------------------------------------------------------------- التشغيل
LOG_FILE = os.path.join(APP_DIR, "error.log")


def log_error(where, exc=None):
    """يسجّل الخطأ في ~/.pydm/error.log لأن النسخة المبنية لا تعرض نافذة سطر أوامر."""
    import traceback
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} {where}\n")
            f.write("".join(traceback.format_exception(*exc)) if exc else traceback.format_exc())
    except Exception:
        pass


def fatal(msg):
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, "PyDM", 0x10)
            return
        except Exception:
            pass
    print(msg, file=sys.stderr)


def resource(*parts):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def main():
    os.makedirs(APP_DIR, exist_ok=True)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        # البرنامج يعمل بالفعل: نُظهر نافذته ونخرج
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/show", timeout=3).close()
        except Exception:
            fatal("المنفذ %d مشغول ببرنامج آخر، فلا يمكن تشغيل PyDM." % PORT)
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()

    mgr = Manager()

    def autosave():
        while True:
            time.sleep(3)
            try:
                mgr.pump()
                mgr.save()
            except Exception:
                log_error("autosave")

    threading.Thread(target=autosave, daemon=True).start()
    try:
        import webview
        window = webview.create_window(
            "PyDM", resource("ui", "index.html"), js_api=Api(mgr),
            width=1080, height=660, min_size=(760, 480), background_color="#061233")
        UI["window"] = window
        window.events.closing += lambda: mgr.shutdown()
        webview.start(gui="edgechromium" if sys.platform.startswith("win") else None,
                      icon=resource("assets", "icon.png"), private_mode=False,
                      storage_path=os.path.join(APP_DIR, "webview"))
    except Exception:
        log_error("startup")
        mgr.shutdown()
        fatal("تعذّر فتح واجهة PyDM. تأكد من وجود Microsoft Edge WebView2.\nالتفاصيل في:\n" + LOG_FILE)


if __name__ == "__main__":
    main()
