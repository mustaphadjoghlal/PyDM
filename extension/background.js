const API = "http://127.0.0.1:9614";

// ---------- إعدادات التقاط الفيديو والصوت
const MIN_MEDIA_BYTES = 150 * 1024;   // نتجاهل الأصوات والمقاطع الصغيرة (نقرات الواجهة ...)
const MAX_PER_TAB = 60;
const SKIP_HOSTS = /(^|\.)googlevideo\.com$/i;   // يوتيوب: روابط موقّعة ومنفصلة، استخدم yt-dlp
const STREAM_TYPES = /^(application\/(vnd\.apple\.mpegurl|x-mpegurl|dash\+xml)|audio\/(x-)?mpegurl)$/i;
const SEGMENT_TYPES = /^(video\/mp2t|audio\/mp2t|video\/iso\.segment)$/i;
const MEDIA_EXT = /\.(mp4|m4v|webm|mkv|mov|flv|wmv|3gp|mp3|m4a|aac|ogg|oga|opus|wav|flac|weba)$/i;
const STREAM_EXT = /\.(m3u8|mpd)$/i;
const SEGMENT_EXT = /\.(ts|m4s|m4f|cmfv|cmfa)$/i;

async function isEnabled() {
  const { enabled = true } = await chrome.storage.local.get("enabled");
  return enabled;
}

async function cookieHeader(url) {
  try {
    const cs = await chrome.cookies.getAll({ url });
    return cs.map(c => c.name + "=" + c.value).join("; ");
  } catch (e) { return ""; }
}

async function handoff(url, referrer, meta) {
  const headers = { "User-Agent": navigator.userAgent };
  if (referrer) headers["Referer"] = referrer;
  const ck = await cookieHeader(url);
  if (ck) headers["Cookie"] = ck;
  const body = { url, headers };
  if (meta && meta.kind) body.kind = meta.kind;
  if (meta && meta.mode) body.mode = meta.mode;
  if (meta && meta.title) body.title = meta.title;
  const r = await fetch(API + "/add", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  return r.ok;
}

function flagError() {
  chrome.action.setBadgeText({ text: "!" });
  chrome.action.setBadgeBackgroundColor({ color: "#c0392b" });
  setTimeout(() => chrome.action.setBadgeText({ text: "" }), 4000);
}

// ---------- التقاط التحميلات تلقائياً
chrome.downloads.onCreated.addListener(async (item) => {
  if (!(await isEnabled())) return;
  const url = item.url;
  if (!/^https?:/i.test(url)) return;      // نتجاهل blob: و data:
  try {
    if (await handoff(url, item.referrer)) {
      await chrome.downloads.cancel(item.id);
      await chrome.downloads.erase({ id: item.id });
    }
  } catch (e) {
    // البرنامج غير مفتوح: نترك المتصفح يكمل التحميل بنفسه
  }
});

// ---------- قائمة الزر الأيمن
const YTDLP_PAGES = [
  "*://*.youtube.com/*", "*://youtu.be/*", "*://*.facebook.com/*", "*://fb.watch/*",
  "*://*.instagram.com/*", "*://*.tiktok.com/*", "*://*.twitter.com/*", "*://x.com/*",
  "*://*.vimeo.com/*", "*://*.dailymotion.com/*", "*://*.twitch.tv/*",
  "*://*.soundcloud.com/*", "*://*.reddit.com/*"
];

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: "pydm",
      title: "تحميل بواسطة PyDM",
      contexts: ["link", "video", "audio", "image"]
    });
    chrome.contextMenus.create({
      id: "pydm-page",
      title: "تحميل هذه الصفحة بـ yt-dlp (فيديو)",
      contexts: ["page"],
      documentUrlPatterns: YTDLP_PAGES
    });
    chrome.contextMenus.create({
      id: "pydm-page-audio",
      title: "تحميل هذه الصفحة بـ yt-dlp (صوت MP3)",
      contexts: ["page"],
      documentUrlPatterns: YTDLP_PAGES
    });
  });
});

chrome.contextMenus.onClicked.addListener(async (info) => {
  try {
    if (info.menuItemId === "pydm-page" || info.menuItemId === "pydm-page-audio") {
      await handoff(info.pageUrl, null, {
        kind: "ytdlp", mode: info.menuItemId === "pydm-page-audio" ? "audio" : "video"
      });
      return;
    }
    const url = info.linkUrl || info.srcUrl;
    if (url) await handoff(url, info.pageUrl);
  } catch (e) { flagError(); }
});

// ---------- رصد الفيديو والصوت في كل تبويب
function hget(headers, name) {
  const h = (headers || []).find(x => x.name.toLowerCase() === name);
  return h ? h.value : "";
}

// يعيد {url, kind: "media"|"stream", ct, size, name} أو null
function detect(d) {
  if (d.tabId < 0 || d.url.startsWith(API)) return null;
  if (d.statusCode !== 200 && d.statusCode !== 206) return null;
  let u;
  try { u = new URL(d.url); } catch (e) { return null; }
  if (!/^https?:$/.test(u.protocol) || SKIP_HOSTS.test(u.hostname)) return null;

  const ct = hget(d.responseHeaders, "content-type").split(";")[0].trim().toLowerCase();
  const path = u.pathname.toLowerCase();
  if (SEGMENT_TYPES.test(ct) || SEGMENT_EXT.test(path)) return null;   // قطع HLS/DASH

  let kind = null;
  if (STREAM_TYPES.test(ct) || STREAM_EXT.test(path)) kind = "stream";
  else if (ct.startsWith("video/") || ct.startsWith("audio/")) kind = "media";
  else if (MEDIA_EXT.test(path) && (ct === "" || /^(application|binary)\/octet-stream$/.test(ct))) kind = "media";
  if (!kind) return null;

  let size = 0;
  const m = hget(d.responseHeaders, "content-range").match(/\/(\d+)\s*$/);
  if (m) size = +m[1];
  else size = +hget(d.responseHeaders, "content-length") || 0;
  if (kind === "media" && size && size < MIN_MEDIA_BYTES) return null;

  let name = u.pathname.split("/").pop() || u.hostname;
  try { name = decodeURIComponent(name); } catch (e) { /* نتركه كما هو */ }
  return { url: d.url.split("#")[0], kind, ct, size, name };
}

// نرتّب القراءة/الكتابة حتى لا تتداخل الطلبات المتزامنة
let chain = Promise.resolve();
function enqueue(fn) { chain = chain.then(fn).catch(() => {}); }

function setBadge(tabId, n) {
  chrome.action.setBadgeText({ tabId, text: n ? String(n) : "" });
  if (n) chrome.action.setBadgeBackgroundColor({ tabId, color: "#1e8449" });
}

async function record(tabId, item) {
  const key = "m" + tabId;
  const list = (await chrome.storage.session.get(key))[key] || [];
  if (list.some(x => x.url === item.url)) return;
  list.push(item);
  if (list.length > MAX_PER_TAB) list.shift();
  await chrome.storage.session.set({ [key]: list });
  setBadge(tabId, list.length);
}

chrome.webRequest.onResponseStarted.addListener((d) => {
  const item = detect(d);
  if (item) enqueue(() => record(d.tabId, item));
}, { urls: ["http://*/*", "https://*/*"] }, ["responseHeaders"]);

// عند فتح صفحة جديدة في التبويب نبدأ قائمة جديدة
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.status === "loading" && info.url) {
    enqueue(async () => {
      await chrome.storage.session.remove("m" + tabId);
      setBadge(tabId, 0);
    });
  }
});
chrome.tabs.onRemoved.addListener((tabId) => {
  enqueue(() => chrome.storage.session.remove("m" + tabId));
});

// ---------- رسائل النافذة المنبثقة
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg && msg.type === "media") {   // الزر العائم يطلب الملفات المرصودة في تبويبه
    const key = "m" + (sender.tab ? sender.tab.id : -1);
    chrome.storage.session.get(key).then(r => reply({ items: r[key] || [] })).catch(() => reply({ items: [] }));
    return true;
  }
  if (msg && msg.type === "send") {
    const kind = msg.kind === "stream" || msg.kind === "ytdlp" ? msg.kind : "";
    handoff(msg.url, msg.referer, { kind, title: msg.title, mode: msg.mode === "audio" ? "audio" : "video" })
      .then(ok => reply({ ok }))
      .catch(() => reply({ ok: false }));
    return true;   // رد غير متزامن
  }
});
