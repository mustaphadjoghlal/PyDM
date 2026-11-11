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


class SpeedLimiter:
    """حد سرعة مشترك لكل التحميلات (دلو رموز). rate = 0 يعني بلا حد."""

    def __init__(self):
        self.rate = 0
        self.lock = threading.Lock()
        self.allow = 0.0
        self.stamp = time.monotonic()

    def set(self, rate):
        with self.lock:
            self.rate = max(0, int(rate or 0))
            self.allow = 0.0
            self.stamp = time.monotonic()

    def take(self, n):
        while True:
            with self.lock:
                if not self.rate:
                    return
                now = time.monotonic()
                self.allow = min(self.rate, self.allow + (now - self.stamp) * self.rate)  # رصيد ثانية واحدة كحد أقصى
                self.stamp = now
                if self.allow >= n or self.allow >= self.rate:
                    self.allow -= n
                    return
                wait = (n - self.allow) / self.rate
            time.sleep(min(wait, 0.25))


LIMITER = SpeedLimiter()
MIN_STEAL = 1024 * 1024  # أصغر جزء يستحق أن يُقسم لاتصال متفرغ
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
        path = f"{base} {i}{ext}"
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


def _tool_dirs():
    dirs = []
    try:
        dirs.append(os.path.dirname(os.path.abspath(sys.argv[0])))
    except Exception:
        pass
    if sys.executable:
        dirs.append(os.path.dirname(sys.executable))
    dirs.append(APP_DIR)
    return dirs


def find_js_runtime():
    """يوتيوب يتطلب محرك جافاسكربت ليفك تحدّيه، وإلا تختفي الصيغ ويظهر
    'Requested format is not available'. Deno يُضمَّن مع المثبّت، و Node بديل إن وُجد."""
    for d in _tool_dirs():
        for n in ("deno.exe", "deno"):
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return f"deno:{p}"
    p = shutil.which("deno")
    if p:
        return f"deno:{p}"
    p = shutil.which("node")
    if p:
        return f"node:{p}"
    return None


_ytdlp_updated = threading.Event()


def update_ytdlp_once():
    """تحديث yt-dlp مرة في كل تشغيل للبرنامج: يوتيوب يتغير كثيراً والنسخة القديمة تتعطل."""
    if _ytdlp_updated.is_set():
        return
    _ytdlp_updated.set()
    exe = find_ytdlp()
    if not exe or len(exe) != 1 or not os.access(os.path.dirname(exe[0]), os.W_OK):
        return
    try:
        subprocess.run(exe + ["-U"], creationflags=NOWIN, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    except Exception:
        pass


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


QUALITIES = ("best", "2160", "1440", "1080", "720", "480", "360")

YTDLP_HOSTS = re.compile(
    r"(^|\.)(youtube\.com|youtu\.be|facebook\.com|fb\.watch|instagram\.com|tiktok\.com|"
    r"twitter\.com|x\.com|vimeo\.com|dailymotion\.com|twitch\.tv|soundcloud\.com|reddit\.com)$", re.I)


def is_ytdlp_url(url):
    """صفحة فيديو في موقع معروف (وليس ملفاً مباشراً)."""
    p = urllib.parse.urlparse(url)
    if not YTDLP_HOSTS.search(p.hostname or ""):
        return False
    return not re.search(r"\.(mp4|webm|m4a|mp3|jpe?g|png|gif|m3u8|mpd)$", p.path, re.I)


def is_playlist_url(url):
    """رابط قائمة تشغيل: /playlist?list=... أو فيديو داخل قائمة (watch?v=..&list=..)."""
    p = urllib.parse.urlparse(url)
    if not YTDLP_HOSTS.search(p.hostname or ""):
        return False
    q = urllib.parse.parse_qs(p.query)
    lst = (q.get("list") or [""])[0]
    if lst and not lst.startswith("RD"):     # RD = "ميكس" لا نهائي يولّده يوتيوب، ليس قائمة حقيقية
        return True
    return bool(re.search(r"/(playlist|sets|albums?)(/|$)", p.path, re.I))


PLAYLIST_MAX = 500


def list_playlist(url):
    """يقرأ عناصر القائمة عبر yt-dlp بلا تحميل. يعيد (عنوان القائمة، [(رابط، عنوان)])."""
    exe = find_ytdlp()
    if not exe:
        raise IOError("يحتاج yt-dlp: ضع yt-dlp.exe بجانب PyDM.exe")
    update_ytdlp_once()
    cmd = exe + ["--flat-playlist", "--yes-playlist", "--no-warnings", "--no-colors",
                 "--playlist-end", str(PLAYLIST_MAX),
                 "--print", "%(playlist_title)s\t%(url)s\t%(title)s", url]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                       creationflags=NOWIN, env=env, timeout=180)
    out = r.stdout.decode("utf-8", "replace")
    title, items, seen = "", [], set()
    for line in out.splitlines():
        parts = line.rstrip("\r").split("\t")
        if len(parts) < 3 or not parts[1].startswith("http"):
            continue
        name = parts[2].strip()
        if name in ("[Private video]", "[Deleted video]") or parts[1] in seen:
            continue
        seen.add(parts[1])
        title = title or ("" if parts[0] in ("NA", "") else parts[0])
        items.append((parts[1], "" if name == "NA" else name))
    if not items:
        err = [l for l in r.stderr.decode("utf-8", "replace").splitlines() if l.startswith("ERROR")]
        raise IOError(err[-1][6:].strip()[:160] if err else "لم أجد فيديوهات في هذه القائمة")
    return title, items


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
        self._seglock = threading.Lock()
        self.verify_url = False   # بعد "تحديث الرابط": نتأكد أن الرابط الجديد للملف نفسه
        self.category = ""         # مجلد الفئة الفرعي (Video, Music ...) أو "" للمجلد الرئيسي
        self.auto_category = False
        self.in_queue = False      # ينتظر دوره في الطابور
        self.on_finish = None
        self.force_name = ""       # اسم اختاره المستخدم في نافذة معلومات الملف

    # ---- حفظ/استرجاع
    def to_dict(self):
        return {k: getattr(self, k) for k in
                ("url", "folder", "headers", "conns", "name", "path", "total",
                 "ranged", "segs", "status", "error", "auto_category", "force_name")}

    @classmethod
    def from_dict(cls, d):
        o = cls(d["url"], d["folder"], d.get("headers"), d.get("conns", 8))
        for k in ("name", "path", "total", "ranged", "segs", "error", "auto_category", "force_name"):
            setattr(o, k, d.get(k, getattr(o, k)))
        st = d.get("status", "paused")
        o.status = st if st in ("done", "error") else "paused"
        return o

    @property
    def part(self):
        return self.path + ".part"

    def done_bytes(self):
        # بعد تقسيم جزء أثناء تحميله قد يكتب الاتصال القديم قطعة واحدة بعد الحد الجديد (البيانات نفسها)
        return sum(max(0, min(s[2], s[1] + 1 if s[1] >= 0 else s[2]) - s[0]) for s in self.segs)

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

    def _check_new_url(self):
        old = (self.name, self.total, self.ranged)
        self._probe()
        new_total = self.total
        self.name, self.total, self.ranged = old
        if old[1] and new_total and new_total != old[1]:
            raise IOError(f"الرابط الجديد لملف مختلف (الحجم {fmt_size(new_total)} بدل {fmt_size(old[1])})")
        self.verify_url = False

    def _prepare(self):
        if self.auto_category:
            sub = CATEGORY_DIRS.get(category_for_name(self.name), "")
            if sub:
                self.folder = os.path.join(self.folder, sub)
            self.auto_category = False
        if self.force_name:
            self.name = safe_name(self.force_name) or self.name
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

    def _steal(self):
        """تقسيم ديناميكي (مثل IDM): اتصال أنهى جزءه يأخذ النصف الثاني من أكبر جزء متبقٍّ."""
        with self._seglock:
            best, left = None, 0
            for s in self.segs:
                r = s[1] - s[2] + 1
                if s[1] >= 0 and r > left:
                    best, left = s, r
            if best is None or left < 2 * MIN_STEAL:
                return None
            mid = best[2] + left // 2
            new = [mid, best[1], mid]
            best[1] = mid - 1
            self.segs.append(new)
            return new

    def _worker(self, seg, ev):
        while seg is not None and not ev.is_set():
            self._work(seg, ev)
            seg = self._steal() if (self.ranged and not ev.is_set() and not self._remaining(seg)) else None

    def _work(self, seg, ev):
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
                            with self._seglock:
                                want = CHUNK if seg[1] < 0 else min(CHUNK, seg[1] - seg[2] + 1)
                            if want <= 0:
                                break
                            LIMITER.take(want)
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
            elif self.verify_url:
                self._check_new_url()
            if self.segs and not self.ranged:
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
        self.in_queue = False
        if self.status == "done":
            return
        if self.thread and self.thread.is_alive() and not self.stop.is_set():
            return
        prev = self.thread
        self.stop = threading.Event()
        self.cancelled = False
        self.attempt = getattr(self, "attempt", 0) + 1
        self.status = "queued"
        self.thread = threading.Thread(target=self._run_and_notify, args=(self.stop, prev), daemon=True)
        self.thread.start()

    def _run_and_notify(self, ev, prev):
        try:
            self._run(ev, prev)
        finally:
            cb = self.on_finish        # الطابور: انتهى هذا التحميل، فليبدأ التالي فوراً
            if cb:
                try:
                    cb()
                except Exception:
                    pass

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
                        LIMITER.take(CHUNK)
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
        self.quality = "best"     # best | 2160 | 1440 | 1080 | 720 | 480 | 360
        self.prefix = ""          # ترقيم عناصر القائمة: "01 - "
        self.work = ""            # مجلد عمل مؤقت: يمنع التصادم مع ملف موجود بالاسم نفسه
        self.bytes_done = 0
        self.proc = None
        self.cookies = []      # من المتصفح، في الذاكرة فقط (لا تُحفظ على القرص)
        self.compat = True     # H.264 + AAC: صيغة يقبلها البريمير وكل البرامج
        self._cookie_file = ""

    def to_dict(self):
        d = super().to_dict()
        d.update(kind="ytdlp", title=self.title, media=self.media, quality=self.quality, prefix=self.prefix,
                 work=self.work)
        return d

    @classmethod
    def from_dict(cls, d):
        o = super().from_dict(d)
        o.title = d.get("title", "")
        o.media = "audio" if d.get("media") == "audio" else "video"
        o.quality = d.get("quality") if d.get("quality") in QUALITIES else "best"
        o.work = str(d.get("work") or "")
        o.prefix = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(d.get("prefix") or ""))[:12]
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

    def _is_youtube(self):
        return bool(re.search(r"(^|\.)(youtube\.com|youtube-nocookie\.com|youtu\.be)$",
                              urllib.parse.urlparse(self.url).hostname or ""))

    def _write_cookies(self):
        """ملف كوكيز بصيغة Netscape لـ yt-dlp. فيسبوك وإنستغرام يتطلبان غالباً تسجيل الدخول."""
        if not self.cookies or self._is_youtube():
            return ""
        path = os.path.join(APP_DIR, f"cookies-{os.getpid()}-{id(self)}.txt")
        lines = ["# Netscape HTTP Cookie File"]
        for c in self.cookies:
            dom = c["domain"]
            lines.append("\t".join([
                ("#HttpOnly_" if c.get("httpOnly") else "") + dom,
                "TRUE" if dom.startswith(".") else "FALSE",
                c.get("path") or "/",
                "TRUE" if c.get("secure") else "FALSE",
                str(int(c.get("expirationDate") or 0)),
                c["name"], c["value"]]))
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(lines) + "\n")
            self._cookie_file = path
            return path
        except Exception:
            return ""

    def _drop_cookies(self):
        if self._cookie_file:
            try:
                os.remove(self._cookie_file)
            except Exception:
                pass
            self._cookie_file = ""

    def _workdir(self):
        if not self.work:
            self.work = os.path.join(self.folder, ".pydm-" + os.urandom(4).hex())
        os.makedirs(self.work, exist_ok=True)
        if sys.platform.startswith("win"):
            try:
                import ctypes
                ctypes.windll.kernel32.SetFileAttributesW(self.work, 2)   # مخفي
            except Exception:
                pass
        return self.work

    def _finish_file(self):
        """ينقل الملف من مجلد العمل إلى مجلد التحميل؛ إن وُجد ملف بالاسم نفسه يصير "الاسم 1"."""
        final = unique_path(os.path.join(self.folder, os.path.basename(self.path)))
        os.replace(self.path, final)
        self.path = final
        self.name = os.path.basename(final)
        shutil.rmtree(self.work, ignore_errors=True)
        self.work = ""

    def _command(self):
        exe = find_ytdlp()
        if not exe:
            raise IOError("يحتاج yt-dlp: ضع yt-dlp.exe بجانب PyDM.exe")
        ff = find_ffmpeg()
        stem = safe_name(self.force_name).replace("%", "%%") if self.force_name else "%(title).150B"
        tpl = os.path.join(self._workdir().replace("%", "%%"),
                           self.prefix.replace("%", "%%") + stem + ".%(ext)s")
        cmd = exe + ["--no-playlist", "--newline", "--no-colors", "--progress",
                     "--progress-template", self.TEMPLATE,
                     "--print", "after_move:PYDMFILE %(filepath)s", "-o", tpl]
        # لا نمرر وكيل المتصفح ولا الكوكيز: yt-dlp يختار عملاء يوتيوب المناسبين بنفسه،
        # وتمرير ترويسات المتصفح يدفعه لعملاء تحتاج رموزاً إضافية فتختفي الصيغ.
        ref = self.headers.get("Referer")
        if ref and not self._is_youtube():
            cmd += ["--referer", ref]
        ck = self._write_cookies()
        if ck:
            cmd += ["--cookies", ck]
        js = find_js_runtime()
        if js:
            cmd += ["--js-runtimes", js]
        if ff:
            cmd += ["--ffmpeg-location", ff]
        if self.media == "audio":
            cmd += ["-x", "--audio-format", "mp3"] if ff else ["-f", "ba/b"]
        else:
            res = "res" if self.quality == "best" else f"res:{self.quality}"   # res:720 = أفضل جودة لا تتجاوز 720
            if self.compat:
                # H.264 + AAC أولاً: VP9/AV1/Opus داخل mp4 يرفضها البريمير ("unsupported compression type")
                cmd += ["-S", f"vcodec:h264,{res},acodec:m4a"]
            elif res != "res":
                cmd += ["-S", res]
            cmd += ["-f", "bv*+ba/b", "--merge-output-format", "mp4"] if ff else ["-f", "b"]
        if LIMITER.rate:
            cmd += ["--limit-rate", str(LIMITER.rate)]
        return cmd + [self.url]

    def _run(self, ev, prev):
        if prev is not None:
            prev.join()
        try:
            self.failed = False
            self.error = ""
            self.status = "downloading"
            os.makedirs(self.folder, exist_ok=True)
            update_ytdlp_once()
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
            self._drop_cookies()
            self.speed = 0
            if self.cancelled:
                self._rm_part()
                self.status = "canceled"
                return
            if ev.is_set():
                return  # إيقاف مؤقت
            if proc.returncode != 0:
                err = self.error or f"yt-dlp رمز الخروج {proc.returncode}"
                if "Requested format is not available" in err and not find_js_runtime():
                    err = "يوتيوب يحتاج محرك Deno: أعد تثبيت PyDM بآخر نسخة. (" + err[:80] + ")"
                elif "not a bot" in err or "Sign in to confirm" in err:
                    err = "يوتيوب طلب تسجيل الدخول للتحقق. جرّب لاحقاً أو من شبكة أخرى. (" + err[:60] + ")"
                raise IOError(err)
            if not (self.path and os.path.isfile(self.path)):
                raise IOError("لم يُنشأ الملف")
            self._finish_file()
            self.total = os.path.getsize(self.path)
            self.bytes_done = self.total
            self.status = "done"
        except Exception as e:
            self.error = str(e)
            self.status = "error"

    def _rm_part(self):
        try:
            if self.work:
                shutil.rmtree(self.work, ignore_errors=True)
                self.work = ""
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
            if n <= 0 or n > 262144:
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
                    "quality": data.get("quality") if data.get("quality") in QUALITIES else "best",
                    "playlist": bool(data.get("playlist")),
                    "title": str(data.get("title", ""))[:200]}
            cookies = []
            for c in (data.get("cookies") or [])[:300]:
                if isinstance(c, dict) and c.get("name") and c.get("domain"):
                    cookies.append({k: c.get(k) for k in
                                    ("name", "value", "domain", "path", "secure", "httpOnly", "expirationDate")})
                    cookies[-1]["value"] = str(cookies[-1]["value"] or "")
            meta["cookies"] = cookies
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


CATEGORY_DIRS = {"video": "Video", "audio": "Music", "archive": "Compressed",
                 "app": "Programs", "doc": "Documents", "image": "Images"}


def category_for_name(name):
    ext = os.path.splitext(name or "")[1].lower().lstrip(".")
    for cat, exts in (("video", VIDEO_EXT), ("audio", AUDIO_EXT), ("archive", ARCHIVE_EXT),
                      ("app", APP_EXT), ("doc", DOC_EXT), ("image", IMAGE_EXT)):
        if ext in exts:
            return cat
    return "other"


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


def run_when_done(action):
    """عند انتهاء كل التحميلات: إيقاف التشغيل (بعد دقيقة، يُلغى بـ shutdown /a) أو السكون."""
    trace(f"when done: {action}")
    if not sys.platform.startswith("win"):
        return
    try:
        if action == "shutdown":
            subprocess.Popen(["shutdown", "/s", "/t", "60", "/c",
                              "PyDM: اكتملت التحميلات. سيُطفأ الجهاز خلال دقيقة. للإلغاء: shutdown /a"],
                             creationflags=NOWIN)
        elif action == "sleep":
            subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], creationflags=NOWIN)
    except Exception:
        log_error("when_done")


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
        self.cfg.setdefault("compat", True)   # فيديو H.264 يقبله البريمير
        self.cfg.setdefault("notify", True)   # إشعار عند بدء التحميل وانتهائه
        self.cfg.setdefault("max_parallel", 3)   # تحميلات في الوقت نفسه، والباقي ينتظر دوره
        self.cfg.setdefault("speed_limit", 0)    # بايت/ثانية، 0 = بلا حد
        self.cfg.setdefault("categories", True)  # مجلدات فرعية حسب النوع مثل IDM
        self.cfg.setdefault("ask", True)         # نافذة "معلومات الملف" قبل كل تحميل، مثل IDM
        self.info_ui = None                      # يُعيَّن عند فتح النافذة الرئيسية
        self.pending = {}
        self.cfg["when_done"] = "none"           # لا يُحفظ بين التشغيلات (مثل IDM)
        LIMITER.set(self.cfg["speed_limit"])
        self._was_active = False
        self.notice = ""                         # رسالة قصيرة تعرضها الواجهة مرة واحدة
        self.items = []
        self.notifier = None                  # يُعيَّن عند فتح النافذة
        self._last_status = {}
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
        d.on_finish = self.schedule
        d.added = getattr(d, "added", time.time())
        self.items.append(d)
        return d

    def add_playlist(self, url, headers=None, meta=None):
        """يقرأ القائمة في الخلفية ثم يضيف كل فيديو إلى الطابور داخل مجلد باسم القائمة."""
        meta = dict(meta or {})
        meta.pop("playlist", None)

        def work():
            try:
                title, items = list_playlist(url)
            except Exception as e:
                self.notice = "تعذّرت قراءة القائمة: " + str(e)[:140]
                return
            sub = safe_name(title) or "Playlist"
            width = max(2, len(str(len(items))))
            for i, (u, name) in enumerate(items, 1):
                m = dict(meta, kind="ytdlp", title=name, subfolder=sub, prefix=f"{i:0{width}d} - ")
                self.add(u, headers, m)
            self.notice = f"أُضيف {len(items)} فيديو من القائمة: {title or sub}"

        self.notice = "جارٍ قراءة القائمة..."
        t = threading.Thread(target=work, daemon=True)
        t.start()
        return t

    def kind_of(self, url, meta):
        kind = (meta or {}).get("kind")
        if kind == "ytdlp" or (kind != "stream" and is_ytdlp_url(url)):
            return "ytdlp"
        if kind == "stream" or is_stream_url(url):
            return "stream"
        return "file"

    def category_folder(self, cat):
        sub = CATEGORY_DIRS.get(cat, "") if self.cfg.get("categories", True) else ""
        return os.path.join(self.cfg["folder"], sub) if sub else self.cfg["folder"]

    def request(self, url, headers=None, meta=None):
        """طلب تحميل جديد: يعرض نافذة معلومات الملف (إن كانت مفعّلة) أو يبدأ مباشرة."""
        meta = dict(meta or {})
        if (not self.cfg.get("ask", True) or self.info_ui is None or meta.get("subfolder")
                or (meta.get("playlist") and is_ytdlp_url(url))):
            return self.add(url, headers, meta)
        kind = self.kind_of(url, meta)
        title = re.sub(r"\s*[-|–]\s*(YouTube|Facebook|Instagram|TikTok|X|Vimeo)\s*$", "", meta.get("title") or "").strip()
        if kind == "ytdlp":
            cat = "audio" if meta.get("mode") == "audio" else "video"
            name, ext = safe_name(title), (".mp3" if cat == "audio" else ".mp4")
        elif kind == "stream":
            cat, name, ext = "video", safe_name(title) or "video", ".mp4"
        else:
            name = filename_from(None, url)
            cat, ext = category_for_name(name), ""
        self._uid += 1
        p = {"id": "p" + str(self._uid), "url": url, "headers": headers, "meta": meta, "kind": kind,
             "name": name, "ext": ext, "total": 0, "category": cat, "folder": self.category_folder(cat),
             "probing": kind == "file"}
        self.pending[p["id"]] = p
        if kind == "file":
            threading.Thread(target=self._probe_pending, args=(p,), daemon=True).start()
        try:
            self.info_ui.open(p)
        except Exception:
            log_error("info dialog")
            self.pending.pop(p["id"], None)
            return self.add(url, headers, meta)
        return None

    def _probe_pending(self, p):
        try:
            d = Download(p["url"], p["folder"], p["headers"], 1)
            d._probe()
            p["total"] = d.total
            if d.name and d.name != p["name"]:
                p["name"] = d.name
                cat = category_for_name(d.name)
                if cat != p["category"] and not p.get("user_cat"):
                    p["category"], p["folder"] = cat, self.category_folder(cat)
        except Exception as e:
            p["error"] = str(e)[:120]
        p["probing"] = False

    def resolve(self, pid, action, name="", folder="", never=False):
        """نتيجة نافذة معلومات الملف: start | later | cancel."""
        p = self.pending.pop(pid, None)
        if never:
            self.cfg["ask"] = False
            self.save()
        if not p or action == "cancel":
            return None
        meta = dict(p["meta"], kind={"file": ""}.get(p["kind"], p["kind"]))
        name = safe_name(name)
        if name:
            if p["kind"] != "file":                         # الامتداد يحدده yt-dlp / البث
                name = re.sub(r"\.(mp4|mp3|mkv|webm|m4a|ts)$", "", name, flags=re.I)
            meta["name"] = name
        if folder:
            meta["folder"] = folder
        if action == "later":
            meta["later"] = True
        return self.add(p["url"], p["headers"], meta)

    def add(self, url, headers=None, meta=None):
        meta = meta or {}
        if meta.get("playlist") and is_ytdlp_url(url):
            self.add_playlist(url, headers, meta)
            return None
        conns = int(self.cfg.get("conns", 8))
        folder = self.cfg["folder"]
        cats = bool(self.cfg.get("categories", True))
        kind = meta.get("kind")
        if kind == "ytdlp" or (kind != "stream" and is_ytdlp_url(url)):
            media = meta.get("mode", "video")
            if cats:
                folder = os.path.join(folder, CATEGORY_DIRS["audio" if media == "audio" else "video"])
            if meta.get("subfolder"):
                folder = os.path.join(folder, safe_name(meta["subfolder"]))
            d = YtdlpDownload(url, folder, headers, conns, meta.get("title", ""), media)
            d.prefix = meta.get("prefix", "")
            d.cookies = meta.get("cookies") or []
            d.compat = bool(self.cfg.get("compat", True))
            d.quality = meta.get("quality") if meta.get("quality") in QUALITIES else "best"
        elif kind == "stream" or is_stream_url(url):
            if cats:
                folder = os.path.join(folder, CATEGORY_DIRS["video"])
            d = StreamDownload(url, folder, headers, conns, meta.get("title", ""))
        else:
            d = Download(url, folder, headers, conns)
            d.auto_category = cats      # المجلد الفرعي يُحدد بعد معرفة اسم الملف
        if meta.get("folder"):          # اختاره المستخدم في نافذة معلومات الملف
            d.folder = meta["folder"]
            d.auto_category = False
        if meta.get("name"):
            d.force_name = meta["name"]
            if isinstance(d, (StreamDownload, YtdlpDownload)):
                d.title = meta["name"]
        later = bool(meta.get("later"))
        with self.lock:
            self._insert(d)
            self._last_status[d.uid] = None
            d.in_queue = not later
            if later:                   # "حمّل لاحقاً": يبقى في القائمة متوقفاً
                d.status = "paused"
        self.schedule()
        return d

    # ---- الطابور
    def _active(self):
        return [d for d in self.items if not getattr(d, "in_queue", False) and d.status in ("queued", "downloading")]

    def enqueue(self, d):
        if d.status in ("paused", "error"):
            d.status = "queued"
            d.error = ""
            d.in_queue = True
            self.schedule()
            return True
        return False

    def schedule(self):
        """يبدأ المنتظرين حسب الترتيب ما دام عدد التحميلات الجارية أقل من الحد."""
        with self.lock:
            limit = max(1, int(self.cfg.get("max_parallel", 3)))
            free = limit - len(self._active())
            start = []
            for d in self.items:
                if free <= 0:
                    break
                if getattr(d, "in_queue", False) and d.status == "queued":
                    start.append(d)
                    free -= 1
        for d in start:
            d.start()          # يجعل in_queue = False
            # إشعار البدء بعد لحظة: حتى يُعرف اسم الملف بدل الرابط
            t = threading.Timer(1.5, lambda d=d: d.status == "downloading" and self._notify("start", d))
            t.daemon = True
            t.start()

    def _check_when_done(self):
        with self.lock:
            active = any(getattr(d, "in_queue", False) or d.status in ("queued", "downloading") for d in self.items)
        if self._was_active and not active:
            action = self.cfg.get("when_done", "none")
            if action != "none":
                self.cfg["when_done"] = "none"
                self.save()
                run_when_done(action)
        self._was_active = active

    def _notify(self, event, d):
        trace(f"notify {event}: notifier={'yes' if self.notifier else 'no'}")
        if self.notifier and self.cfg.get("notify", True):
            try:
                self.notifier.show(event, self.row(d))
            except Exception:
                log_error("notify")

    def watch_status(self):
        """يكشف اكتمال التحميلات أو فشلها لإظهار الإشعار."""
        with self.lock:
            items = list(self.items)
        for d in items:
            key = (d.status, getattr(d, "attempt", 0))
            if d.uid not in self._last_status:   # عناصر محمّلة من الجلسة السابقة: لا إشعار
                self._last_status[d.uid] = key
                continue
            if key != self._last_status[d.uid]:
                self._last_status[d.uid] = key
                if d.status == "done":
                    self._notify("done", d)
                elif d.status == "error":
                    self._notify("error", d)

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
                self.request(url, hdrs, meta)
                added += 1
        except queue.Empty:
            pass
        with self.lock:
            for d in list(self.items):
                if d.status == "canceled" and not (d.thread and d.thread.is_alive()):
                    self.items.remove(d)
        self.schedule()
        self.watch_status()
        self._check_when_done()
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
            "waiting": bool(getattr(d, "in_queue", False)) and d.status == "queued",
        }

    def snapshot(self):
        with self.lock:
            items = list(self.items)
        rows = [self.row(d) for d in reversed(items)]
        notice, self.notice = self.notice, ""
        return {
            "notice": notice,
            "items": rows,
            "speed": sum(r["speed"] for r in rows),
            "settings": {"folder": self.cfg["folder"], "conns": int(self.cfg.get("conns", 8)),
                         "startup": startup_get(), "compat": bool(self.cfg.get("compat", True)),
                         "notify": bool(self.cfg.get("notify", True)),
                         "max_parallel": int(self.cfg.get("max_parallel", 3)),
                         "speed_limit": int(self.cfg.get("speed_limit", 0)),
                         "categories": bool(self.cfg.get("categories", True)),
                         "ask": bool(self.cfg.get("ask", True)),
                         "when_done": self.cfg.get("when_done", "none")},
            "tools": {"ytdlp": bool(find_ytdlp()), "ffmpeg": bool(find_ffmpeg()),
                      "deno": bool(find_js_runtime())},
        }

    def save(self):
        with self.lock:
            data = [d.to_dict() for d in self.items if d.status != "canceled"]
        save_json(CONF_FILE, self.cfg)
        save_json(LIST_FILE, data)

    def shutdown(self):
        if self.notifier:
            self.notifier.close()
            self.notifier = None
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
        trace("ui ready (javascript bridge works)")
        UI["ready"] = True
        return True

    def state(self):
        self._mgr.pump()
        return self._mgr.snapshot()

    def clipboard(self):
        m = re.search(r"https?://\S+", clipboard_text())
        return m.group(0) if m else ""

    def quality_list(self):
        return list(QUALITIES)

    def detect(self, url):
        url = (url or "").strip()
        if is_playlist_url(url):
            return "playlist"
        if is_ytdlp_url(url):
            return "site"
        if is_stream_url(url):
            return "stream"
        return "file" if url.startswith(("http://", "https://")) else ""

    def add(self, text, mode="video", quality="best", playlist=False):
        urls = re.findall(r"https?://[^\s\"'<>]+", text or "")
        for url in urls:
            self._mgr.request(url, None, {"mode": "audio" if mode == "audio" else "video",
                                      "quality": quality if quality in QUALITIES else "best",
                                      "playlist": bool(playlist) and is_playlist_url(url)})
        return len(urls)

    def _with(self, uid, fn):
        d = self._mgr.get(str(uid))
        if not d:
            return False
        fn(d)
        return True

    def pause(self, uid):
        def stop(d):
            d.in_queue = False
            d.pause()
        ok = self._with(uid, stop)
        self._mgr.schedule()
        return ok

    def resume(self, uid):
        d = self._mgr.get(str(uid))
        return bool(d and self._mgr.enqueue(d))

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
            d.in_queue = False
            d.pause()
        return True

    def resume_all(self):
        for d in list(self._mgr.items):
            if d.status == "paused":
                d.status = "queued"
                d.in_queue = True
        self._mgr.schedule()
        return True

    def refresh_link(self, uid, url):
        """رابط جديد لتحميل انتهت صلاحية رابطه، ثم الاستئناف من حيث توقف."""
        d = self._mgr.get(str(uid))
        url = (url or "").strip()
        if not d or d.status not in ("paused", "error") or urllib.parse.urlparse(url).scheme not in ("http", "https"):
            return False
        d.url = url
        if isinstance(d, StreamDownload):
            d._rm_part()          # قطع البث مرتبطة بالقائمة القديمة: نبدأ البث من جديد
            d.mode, d.seg_urls, d.seg_done, d.workdir, d.bytes_done = "", [], [], "", 0
        elif type(d) is Download:
            d.verify_url = bool(d.segs)
        return self._mgr.enqueue(d)

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

    def set_option(self, key, value):
        cfg = self._mgr.cfg
        if key in ("compat", "notify", "categories", "ask"):
            cfg[key] = bool(value)
        elif key == "max_parallel":
            try:
                cfg[key] = max(1, min(10, int(value)))
            except (TypeError, ValueError):
                return False
            self._mgr.schedule()
        elif key == "speed_limit":
            try:
                cfg[key] = max(0, int(float(value)))
            except (TypeError, ValueError):
                return False
            LIMITER.set(cfg[key])
        elif key == "when_done":
            if value not in ("none", "shutdown", "sleep"):
                return False
            cfg[key] = value
        else:
            return False
        self._mgr.save()
        return True


TOAST_W, TOAST_H = 440, 128
TOAST_TITLE = "PyDM-Toast"


def force_topmost(win, x=None, y=None):
    """يرفع نافذة الإشعار فوق كل النوافذ دون سحب التركيز من البرنامج الذي تعمل عليه."""
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        u32 = ctypes.windll.user32
        hwnd = 0
        try:
            hwnd = int(win.native.Handle.ToInt64())
        except Exception:
            hwnd = u32.FindWindowW(None, TOAST_TITLE)
        if not hwnd:
            return False
        SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x1, 0x2, 0x10, 0x40
        flags = SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW | (SWP_NOMOVE if x is None else 0)
        trace(f"topmost hwnd={hwnd}")
        u32.ShowWindow(hwnd, 4)                                   # SW_SHOWNOACTIVATE
        u32.SetWindowPos(hwnd, -1, int(x or 0), int(y or 0), 0, 0, flags)   # HWND_TOPMOST
        return True
    except Exception:
        log_error("topmost")
        return False


def work_area():
    """مساحة الشاشة بلا شريط المهام (بالبكسل)، أو None."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes
        r = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0):   # SPI_GETWORKAREA
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return None


class ToastApi:
    def __init__(self, notifier):
        self._n = notifier

    def act(self, action, path=""):
        if action == "open" and path:
            open_path(path)
        elif action == "folder" and path:
            reveal_path(path)
        elif action == "show":
            show_window()
        self._n.close()
        return True

    def close(self):
        self._n.close()
        return True


class Notifier:
    """نافذة إشعار صغيرة أسفل يمين الشاشة (مثل IDM)، لا تأخذ التركيز ولا تظهر في شريط المهام."""

    def __init__(self):
        self.win = None
        self.timer = None
        self.lock = threading.Lock()
        self.api = ToastApi(self)
        try:
            with open(resource("ui", "toast.html"), encoding="utf-8") as f:
                self.template = f.read()
        except Exception:
            self.template = ""

    def _pos(self):
        import webview
        try:
            sc = webview.screens[0]
            return sc.width - TOAST_W - 16, sc.height - TOAST_H - 64
        except Exception:
            return None, None

    def _raise(self, w):
        # نكرر الرفع قليلاً: بعض النوافذ (ملء الشاشة، المتصفح) تعود فوق الإشعار عند ظهوره
        for delay in (0, 0.3, 1.0):
            t = threading.Timer(delay, lambda: self.win is w and force_topmost(w))
            t.daemon = True
            t.start()

    def close(self):
        with self.lock:
            w, self.win = self.win, None
            if self.timer:
                self.timer.cancel()
                self.timer = None
        if w is not None:
            try:
                w.destroy()
            except Exception:
                pass

    def show(self, event, row):
        if not self.template:
            return
        import webview
        data = {"event": event, "name": row["name"], "path": row.get("path") or "",
                "error": row.get("error") or "", "size": row.get("total") or 0,
                "source": row.get("source") or ""}
        html = self.template.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False))
        self.close()      # إشعار واحد في كل مرة: الجديد يحل محل القديم
        x, y = self._pos()
        trace(f"toast {event} at {x},{y}")
        w = webview.create_window(
            TOAST_TITLE, html=html, js_api=self.api, width=TOAST_W, height=TOAST_H, x=x, y=y,
            resizable=False, frameless=True, on_top=True, focus=False, shadow=True,
            background_color="#03154A")
        trace("toast window created")
        try:
            w.events.shown += lambda: (trace("toast shown"), self._raise(w))
        except Exception:
            log_error("toast events")
        with self.lock:
            self.win = w
            self.timer = threading.Timer(8 if event == "start" else 15, self.close)
            self.timer.daemon = True
            self.timer.start()


INFO_TITLE = "PyDM - معلومات الملف"
CATEGORY_LABELS = [("other", "عام"), ("video", "فيديو"), ("audio", "موسيقى"), ("archive", "ملفات مضغوطة"),
                   ("app", "برامج"), ("doc", "مستندات"), ("image", "صور")]


def bring_to_front(win, title):
    """يُظهر النافذة أمام كل النوافذ ويعطيها التركيز (نافذة تنتظر قرار المستخدم)."""
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        u32 = ctypes.windll.user32
        try:
            hwnd = int(win.native.Handle.ToInt64())
        except Exception:
            hwnd = u32.FindWindowW(None, title)
        if not hwnd:
            return False
        u32.ShowWindow(hwnd, 5)
        u32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x1 | 0x2 | 0x40)      # فوق الجميع
        u32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        log_error("front")
        return False


class InfoApi:
    def __init__(self, ui, p):
        self._ui, self._p = ui, p

    def get(self):
        p = self._p
        return {"name": p["name"], "ext": p["ext"], "total": p["total"], "category": p["category"],
                "folder": p["folder"], "probing": p["probing"], "error": p.get("error", "")}

    def category(self, cat):
        if cat in dict(CATEGORY_LABELS):
            self._p["category"] = cat
            self._p["user_cat"] = True
            self._p["folder"] = self._ui.mgr.category_folder(cat)
        return self._p["folder"]

    def browse(self, current=""):
        w = self._ui.windows.get(self._p["id"])
        if w is None:
            return ""
        import webview
        res = w.create_file_dialog(webview.FOLDER_DIALOG, directory=current or self._p["folder"])
        if res:
            self._p["folder"] = res[0] if isinstance(res, (list, tuple)) else res
            return self._p["folder"]
        return ""

    def done(self, action, name="", folder="", never=False):
        if action not in ("start", "later", "cancel"):
            return False
        self._ui.mgr.resolve(self._p["id"], action, name, folder, bool(never))
        self._ui.close(self._p["id"])
        return True


class InfoDialogs:
    """نافذة "معلومات الملف" قبل التحميل، مثل IDM: الرابط، الفئة، مكان الحفظ، الحجم."""

    def __init__(self, mgr):
        self.mgr = mgr
        self.windows = {}
        with open(resource("ui", "info.html"), encoding="utf-8") as f:
            self.template = f.read()

    def open(self, p):
        import webview
        data = {"id": p["id"], "url": p["url"], "kind": p["kind"], "name": p["name"], "ext": p["ext"],
                "total": p["total"], "category": p["category"], "folder": p["folder"],
                "probing": p["probing"], "categories": CATEGORY_LABELS}
        html = self.template.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False))
        trace(f"info dialog for {p['kind']}")
        w = webview.create_window(INFO_TITLE, html=html, js_api=InfoApi(self, p), width=620, height=400,
                                  resizable=False, on_top=True, background_color="#061233")
        self.windows[p["id"]] = w
        pid = p["id"]

        def closed():
            self.windows.pop(pid, None)
            self.mgr.resolve(pid, "cancel")     # أُغلقت بزر ×: إلغاء (لا أثر إن حُسم الطلب قبل ذلك)

        def shown():
            for delay in (0, 0.4):
                t = threading.Timer(delay, lambda: bring_to_front(w, INFO_TITLE))
                t.daemon = True
                t.start()

        try:
            w.events.shown += shown
            w.events.closed += closed
        except Exception:
            pass

    def close(self, pid):
        w = self.windows.pop(pid, None)
        if w is not None:
            try:
                w.destroy()
            except Exception:
                pass


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


TRACE_FILE = os.path.join(APP_DIR, "startup.log")


def trace(msg):
    """سجل خطوات بدء التشغيل (يُكتب من جديد في كل تشغيل) لمعرفة أين يتوقف البرنامج إن لم يفتح."""
    try:
        with open(TRACE_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


def resource(*parts):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def window_icon():
    """ويندوز يقبل .ico فقط لأيقونة النافذة (System.Drawing.Icon)؛ PNG يُسقط البرنامج."""
    name = "icon.ico" if sys.platform.startswith("win") else "icon.png"
    path = resource("assets", name)
    return path if os.path.isfile(path) else None


def main():
    os.makedirs(APP_DIR, exist_ok=True)
    try:
        open(TRACE_FILE, "w").close()
    except Exception:
        pass
    trace(f"start frozen={getattr(sys, 'frozen', False)} python={sys.version.split()[0]}")
    try:  # الانهيارات الأصلية (خارج بايثون) تُكتب هنا أيضاً
        import faulthandler
        main._fh = open(os.path.join(APP_DIR, "crash.log"), "w")
        faulthandler.enable(main._fh)
    except Exception:
        pass
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
    trace("server listening")

    mgr = Manager()
    trace(f"manager loaded {len(mgr.items)} items")

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
        trace(f"pywebview {getattr(webview, '__version__', '?')} imported")
        page = resource("ui", "index.html")
        trace(f"ui page {page} exists={os.path.isfile(page)}")
        window = webview.create_window(
            "PyDM", page, js_api=Api(mgr),
            width=1080, height=660, min_size=(760, 480), background_color="#061233")
        UI["window"] = window
        window.events.closing += lambda: mgr.shutdown()

        def start_notifier():
            mgr.notifier = Notifier()
            trace("notifier ready")
            try:
                mgr.info_ui = InfoDialogs(mgr)
                trace("info dialogs ready")
            except Exception:
                log_error("info dialogs")

        window.events.loaded += start_notifier
        window.events.loaded += lambda: trace("page loaded")
        trace("starting webview")
        webview.start(gui="edgechromium" if sys.platform.startswith("win") else None,
                      icon=window_icon(), private_mode=False,
                      storage_path=os.path.join(APP_DIR, "webview"))
        trace("webview closed")
    except Exception:
        log_error("startup")
        mgr.shutdown()
        fatal("تعذّر فتح واجهة PyDM. تأكد من وجود Microsoft Edge WebView2.\nالتفاصيل في:\n" + LOG_FILE)


if __name__ == "__main__":
    main()
