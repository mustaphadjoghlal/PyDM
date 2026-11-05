const API = "http://127.0.0.1:9614";

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

async function handoff(url, referrer) {
  const headers = { "User-Agent": navigator.userAgent };
  if (referrer) headers["Referer"] = referrer;
  const ck = await cookieHeader(url);
  if (ck) headers["Cookie"] = ck;
  const r = await fetch(API + "/add", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url, headers })
  });
  return r.ok;
}

function flagError() {
  chrome.action.setBadgeText({ text: "!" });
  chrome.action.setBadgeBackgroundColor({ color: "#c0392b" });
  setTimeout(() => chrome.action.setBadgeText({ text: "" }), 4000);
}

// التقاط التحميلات تلقائياً
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

// قائمة الزر الأيمن
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "pydm",
    title: "تحميل بواسطة PyDM",
    contexts: ["link", "video", "audio", "image"]
  });
});

chrome.contextMenus.onClicked.addListener(async (info) => {
  const url = info.linkUrl || info.srcUrl;
  if (!url) return;
  try { await handoff(url, info.pageUrl); } catch (e) { flagError(); }
});
