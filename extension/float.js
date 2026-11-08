// زر "تحميل" عائم فوق الفيديو، مثل IDM.
(() => {
  if (window.__pydmFloat) return;
  window.__pydmFloat = true;

  const MIN_W = 240, MIN_H = 135;
  const SITES = /(^|\.)(youtube\.com|youtube-nocookie\.com|youtu\.be|facebook\.com|fb\.watch|instagram\.com|tiktok\.com|twitter\.com|x\.com|vimeo\.com|dailymotion\.com|twitch\.tv|soundcloud\.com|reddit\.com)$/i;
  const POST_PATH = /\/(watch|shorts|embed|live|reel|reels|videos?|p|status|clip|v)\b|\/\d{6,}/i;
  const ICON = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.5v10m0 0-4-4m4 4 4-4M5 16.5v2a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-2" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

  let enabled = true;
  chrome.storage.local.get("floatBtn", r => { enabled = r.floatBtn !== false; });
  chrome.storage.onChanged.addListener(ch => {
    if (ch.floatBtn) { enabled = ch.floatBtn.newValue !== false; if (!enabled) hide(true); }
  });

  // ---------- العنصر (داخل Shadow DOM حتى لا تؤثر عليه أنماط الموقع)
  const host = document.createElement("pydm-float");
  host.style.cssText = "all:initial;position:fixed;z-index:2147483647;top:0;left:0;display:none;";
  const root = host.attachShadow({ mode: "closed" });
  root.innerHTML = `
  <style>
    :host { all: initial; }
    * { box-sizing: border-box; font-family: "Segoe UI", Tahoma, sans-serif; }
    .wrap { position: relative; display: inline-block; }
    .pill {
      display: inline-flex; align-items: stretch; height: 34px; border-radius: 10px; overflow: hidden;
      background: #03154A; color: #fff; box-shadow: 0 6px 22px rgba(0,8,40,.45), 0 0 0 1px rgba(255,255,255,.08);
      opacity: 0; transform: translateY(-4px); transition: opacity .16s, transform .16s;
    }
    .pill.show { opacity: 1; transform: none; }
    button { all: unset; box-sizing: border-box; cursor: pointer; display: flex; align-items: center; gap: 7px; color: #fff; font-size: 13px; font-weight: 600; }
    .main { padding: 0 12px 0 11px; direction: rtl; }
    .main svg { width: 17px; height: 17px; color: #19B6FF; }
    .main:hover, .more:hover { background: rgba(25,182,255,.18); }
    .more { padding: 0 9px; border-inline-start: 1px solid rgba(255,255,255,.14); }
    .more svg { width: 12px; height: 12px; }
    .menu {
      display: none; position: absolute; top: 40px; right: 0; min-width: 230px; max-width: 340px; direction: rtl;
      background: #0A1A45; border-radius: 10px; padding: 5px; box-shadow: 0 10px 30px rgba(0,8,40,.5), 0 0 0 1px rgba(255,255,255,.08);
    }
    .menu.open { display: block; }
    .item { display: flex; width: 100%; justify-content: space-between; gap: 12px; padding: 8px 10px; border-radius: 7px; font-weight: 500; }
    .item:hover { background: rgba(25,182,255,.16); }
    .item span:first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; unicode-bidi: plaintext; }
    .item small { color: #8FA3D1; font-size: 11.5px; flex: none; direction: ltr; }
    .label { color: #8FA3D1; font-size: 11.5px; padding: 6px 10px 3px; }
    .ok { background: #12A46A !important; }
    .bad { background: #B4233E !important; }
  </style>
  <div class="wrap">
  <div class="pill" part="pill">
    <button class="main" title="تحميل بواسطة PyDM">${ICON}<span class="txt">تحميل</span></button>
    <button class="more" title="خيارات التحميل" aria-label="خيارات التحميل"><svg viewBox="0 0 12 12"><path d="M2.5 4.5 6 8l3.5-3.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg></button>
  </div>
  <div class="menu" role="menu"></div>
  </div>`;
  const pill = root.querySelector(".pill"), menu = root.querySelector(".menu");
  const mainBtn = root.querySelector(".main"), txt = root.querySelector(".txt");

  let target = null, hideTimer = 0, raf = 0, busy = false;

  function mount() {
    const fs = document.fullscreenElement;
    const parent = fs && fs.tagName !== "VIDEO" ? fs : document.documentElement;
    if (host.parentNode !== parent) parent.appendChild(host);
  }

  function place() {
    raf = 0;
    if (!target || !target.isConnected) return hide(true);
    const r = target.getBoundingClientRect();
    if (r.width < MIN_W || r.height < MIN_H || r.bottom < 0 || r.top > innerHeight) return hide(true);
    const w = pill.offsetWidth || 110;
    host.style.top = Math.max(4, r.top + 12) + "px";
    host.style.left = Math.min(innerWidth - w - 8, Math.max(8, r.right - w - 12)) + "px";
    raf = requestAnimationFrame(place);
  }

  function show(v) {
    if (!enabled || busy && v !== target) return;
    clearTimeout(hideTimer);
    if (target !== v) { target = v; closeMenu(); setLabel("تحميل"); }
    mount();
    host.style.display = "block";
    requestAnimationFrame(() => pill.classList.add("show"));
    if (!raf) raf = requestAnimationFrame(place);
  }

  function hide(now) {
    clearTimeout(hideTimer);
    const go = () => {
      if (menu.classList.contains("open") || busy) return;
      pill.classList.remove("show");
      host.style.display = "none";
      if (raf) cancelAnimationFrame(raf);
      raf = 0; target = null;
    };
    if (now) go(); else hideTimer = setTimeout(go, 2200);
  }

  function setLabel(t, cls) {
    txt.textContent = t;
    pill.classList.remove("ok", "bad");
    if (cls) pill.classList.add(cls);
  }

  // ---------- ماذا نحمّل؟
  function isSite() { return SITES.test(location.hostname); }

  function pageUrlFor(v) {
    // في صفحة منشور واحد نستخدم رابط الصفحة؛ في الخلاصات نبحث عن رابط المنشور حول الفيديو
    if (POST_PATH.test(location.pathname)) return location.href;
    let el = v;
    for (let i = 0; i < 10 && el; i++, el = el.parentElement) {
      const a = el.querySelector && el.querySelector("a[href]");
      const cand = [el.closest && el.closest("a[href]"), a].filter(Boolean);
      for (const c of cand) {
        try {
          const u = new URL(c.href, location.href);
          if (SITES.test(u.hostname) && POST_PATH.test(u.pathname)) return u.href;
        } catch (e) { /* رابط غير صالح */ }
      }
    }
    return location.href;
  }

  function directSrc(v) {
    const s = v.currentSrc || v.src || (v.querySelector("source[src]") || {}).src || "";
    return /^https?:/i.test(s) ? s : "";
  }

  function detected() {
    return new Promise(res => {
      try { chrome.runtime.sendMessage({ type: "media" }, r => res((r && r.items) || [])); }
      catch (e) { res([]); }
    });
  }

  async function bestAction(v) {
    if (isSite()) return { url: pageUrlFor(v), kind: "ytdlp", mode: "video" };
    const list = await detected();
    const stream = list.filter(x => x.kind === "stream").pop();
    if (stream) return { url: stream.url, kind: "stream" };
    const src = directSrc(v);
    if (src) return { url: src, kind: "" };
    const media = list.filter(x => x.kind === "media").sort((a, b) => (b.size || 0) - (a.size || 0))[0];
    if (media) return { url: media.url, kind: "" };
    return { url: location.href, kind: "ytdlp", mode: "video" };   // آخر محاولة: yt-dlp على الصفحة
  }

  function send(action) {
    busy = true;
    setLabel("...");
    const msg = { type: "send", url: action.url, kind: action.kind || "", mode: action.mode || "video",
                  title: document.title, referer: location.href };
    try {
      chrome.runtime.sendMessage(msg, r => {
        busy = false;
        if (r && r.ok) setLabel("تم ✓", "ok"); else setLabel("افتح PyDM", "bad");
        setTimeout(() => { setLabel("تحميل"); hide(); }, 2200);
      });
    } catch (e) {          // الإضافة أُعيد تحميلها: نحتاج تحديث الصفحة
      busy = false; setLabel("حدّث الصفحة", "bad");
    }
  }

  // ---------- القائمة
  function closeMenu() { menu.classList.remove("open"); menu.textContent = ""; }

  function addItem(text, small, action) {
    const b = document.createElement("button");
    b.className = "item"; b.setAttribute("role", "menuitem");
    const t = document.createElement("span"); t.textContent = text; b.appendChild(t);
    if (small) { const s = document.createElement("small"); s.textContent = small; b.appendChild(s); }
    b.addEventListener("click", e => { e.stopPropagation(); closeMenu(); send(action); });
    menu.appendChild(b);
  }

  function addLabel(text) { const d = document.createElement("div"); d.className = "label"; d.textContent = text; menu.appendChild(d); }

  function fmt(n) {
    if (!n) return "";
    const u = ["B", "KB", "MB", "GB"]; let i = 0;
    while (n >= 1024 && i < 3) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n) + " " + u[i];
  }

  async function openMenu() {
    if (menu.classList.contains("open")) return closeMenu();
    menu.textContent = "";
    const v = target;
    if (isSite()) {
      const u = pageUrlFor(v);
      addItem("فيديو بأفضل جودة", "MP4", { url: u, kind: "ytdlp", mode: "video" });
      addItem("صوت فقط", "MP3", { url: u, kind: "ytdlp", mode: "audio" });
    } else {
      const src = v && directSrc(v);
      const list = (await detected()).slice().reverse();
      if (src) addItem("هذا الفيديو", "", { url: src, kind: "" });
      if (list.length) {
        addLabel("ملفات رُصدت في الصفحة");
        for (const it of list.slice(0, 6)) {
          const tag = it.kind === "stream" ? (/mpd/i.test(it.url) ? "DASH" : "HLS") : fmt(it.size);
          addItem(it.name || it.url, tag, { url: it.url, kind: it.kind === "stream" ? "stream" : "" });
        }
      }
      addLabel("محاولة عبر yt-dlp");
      addItem("فيديو من رابط الصفحة", "", { url: location.href, kind: "ytdlp", mode: "video" });
      addItem("صوت MP3 من رابط الصفحة", "", { url: location.href, kind: "ytdlp", mode: "audio" });
    }
    menu.classList.add("open");
  }

  mainBtn.addEventListener("click", async e => {
    e.stopPropagation(); e.preventDefault();
    if (busy || !target) return;
    closeMenu();
    send(await bestAction(target));
  });
  root.querySelector(".more").addEventListener("click", e => { e.stopPropagation(); e.preventDefault(); openMenu(); });
  host.addEventListener("mouseenter", () => clearTimeout(hideTimer));
  host.addEventListener("mouseleave", () => hide());
  document.addEventListener("click", e => { if (!host.contains(e.target)) closeMenu(); }, true);
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeMenu(); });

  // ---------- متى يظهر؟ عند المرور فوق فيديو، أو عند بدء تشغيله
  function videoAt(x, y) {
    for (const v of document.querySelectorAll("video")) {
      const r = v.getBoundingClientRect();
      if (r.width >= MIN_W && r.height >= MIN_H && x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) return v;
    }
    return null;
  }

  let lastMove = 0;
  document.addEventListener("mousemove", e => {
    const now = performance.now();
    if (now - lastMove < 120) return;
    lastMove = now;
    if (host.contains(e.target) || e.composedPath().includes(host)) return;
    const v = videoAt(e.clientX, e.clientY);
    if (v) show(v); else if (target) hide();
  }, { passive: true, capture: true });

  document.addEventListener("play", e => {
    const v = e.target;
    if (v instanceof HTMLVideoElement) { show(v); hide(); }
  }, true);

  document.addEventListener("fullscreenchange", () => { if (host.style.display !== "none") mount(); });
})();
