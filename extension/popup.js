const API = "http://127.0.0.1:9614";
const $ = id => document.getElementById(id);

async function ping() {
  try {
    const r = await fetch(API + "/ping");
    $("state").textContent = r.ok ? "متصل ✔" : "غير متصل";
    $("state").className = r.ok ? "ok" : "bad";
  } catch (e) {
    $("state").textContent = "غير مفتوح";
    $("state").className = "bad";
  }
}

function fmtSize(n) {
  if (!n) return "";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + " " + u[i];
}

function tagOf(it) {
  if (it.kind === "stream") return /mpd$|dash/i.test(it.name + it.ct) ? "DASH" : "HLS";
  const m = it.name.match(/\.([a-z0-9]{2,4})$/i);
  if (m) return m[1].toUpperCase();
  return (it.ct.split("/")[1] || "MEDIA").toUpperCase().slice(0, 5);
}

async function sendItem(it, tab, btn) {
  btn.disabled = true;
  btn.textContent = "...";
  const res = await chrome.runtime.sendMessage({
    type: "send", url: it.url, kind: it.kind, title: tab.title || "", referer: tab.url || ""
  }).catch(() => null);
  btn.textContent = res && res.ok ? "تم ✔" : "افتح PyDM";
  if (!(res && res.ok)) btn.disabled = false;
}

const SITES = /^https?:\/\/([^/]+\.)?(youtube\.com|youtu\.be|facebook\.com|fb\.watch|instagram\.com|tiktok\.com|twitter\.com|x\.com|vimeo\.com|dailymotion\.com|twitch\.tv|soundcloud\.com|reddit\.com)(\/|$)/i;

function setupSite(tab) {
  if (!tab.url || !SITES.test(tab.url)) return;
  $("site").style.display = "block";
  for (const [id, mode] of [["site-v", "video"], ["site-a", "audio"]]) {
    $(id).addEventListener("click", async () => {
      const b = $(id);
      b.disabled = true;
      const res = await chrome.runtime.sendMessage({
        type: "send", url: tab.url, kind: "ytdlp", mode, title: tab.title || "", referer: ""
      }).catch(() => null);
      b.textContent = res && res.ok ? "تم ✔" : "افتح PyDM";
      if (!(res && res.ok)) b.disabled = false;
    });
  }
}

async function renderList() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) return;
  setupSite(tab);
  const key = "m" + tab.id;
  const items = (await chrome.storage.session.get(key))[key] || [];
  $("empty").style.display = items.length ? "none" : "block";
  const list = $("list");
  list.textContent = "";
  for (const it of items.slice().reverse()) {   // الأحدث أولاً
    const row = document.createElement("div");
    row.className = "item";
    const tag = document.createElement("span");
    tag.className = "tag" + (it.kind === "stream" ? " stream" : "");
    tag.textContent = tagOf(it);
    const nm = document.createElement("span");
    nm.className = "nm";
    nm.textContent = it.name;
    nm.title = it.url;
    const sz = document.createElement("span");
    sz.className = "sz";
    sz.textContent = fmtSize(it.size);
    const btn = document.createElement("button");
    btn.textContent = "تحميل";
    btn.addEventListener("click", () => sendItem(it, tab, btn));
    row.append(tag, nm, sz, btn);
    list.appendChild(row);
  }
}

(async () => {
  const { enabled = true } = await chrome.storage.local.get("enabled");
  $("en").checked = enabled;
  ping();
  renderList();
})();

$("en").addEventListener("change", e => chrome.storage.local.set({ enabled: e.target.checked }));

$("send").addEventListener("click", async () => {
  const url = $("url").value.trim();
  if (!/^https?:\/\//i.test(url)) { $("msg").textContent = "أدخل رابطاً صحيحاً"; return; }
  const kind = /\.(m3u8|mpd)(\?|#|$)/i.test(url) ? "stream" : "";
  const res = await chrome.runtime.sendMessage({ type: "send", url, kind, title: "", referer: "" }).catch(() => null);
  $("msg").textContent = res && res.ok ? "تمت الإضافة ✔" : "افتح برنامج PyDM أولاً";
  if (res && res.ok) $("url").value = "";
});
