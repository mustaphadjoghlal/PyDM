const fs = require("fs");
const store = {};
const listeners = {};
global.chrome = {
  storage: { local: { get: async () => (global.localStore || {}) }, session: {
    get: async k => ({ [k]: store[k] }), set: async o => Object.assign(store, o), remove: async k => { delete store[k]; } } },
  cookies: { getAll: async () => [{ name: "a", value: "b", domain: ".facebook.com", path: "/", secure: true, httpOnly: true }] },
  downloads: { onDeterminingFilename: { addListener: f => listeners.det = f },
               cancel: async id => (global.cancelled = (global.cancelled || []).concat(id)), erase: async () => {} },
  contextMenus: { create() {}, removeAll: cb => cb && cb(), onClicked: { addListener: f => listeners.ctx = f } },
  runtime: { onInstalled: { addListener() {} }, onMessage: { addListener: f => listeners.msg = f } },
  action: { setBadgeText: o => (global.badge = o), setBadgeBackgroundColor() {} },
  tabs: { onUpdated: { addListener: f => listeners.upd = f }, onRemoved: { addListener() {} } },
  webRequest: { onResponseStarted: { addListener: f => listeners.wr = f } },
};
global.navigator = { userAgent: "UA" };
let sent = null, appUp = true;
global.fetch = async (u, o) => { if (!appUp) throw new Error("down"); sent = JSON.parse(o.body); return { ok: true }; };
eval(fs.readFileSync("background.js", "utf8") + "\n;global.detect = detect;");

const H = (ct, extra = {}) => [{ name: "Content-Type", value: ct }, ...Object.entries(extra).map(([name, value]) => ({ name, value }))];
const R = (url, ct, extra, o = {}) => ({ tabId: 5, url, statusCode: 200, responseHeaders: H(ct, extra), ...o });
let ok = true;
const t = (n, c) => { console.log((c ? "PASS " : "FAIL ") + n); ok &&= !!c; };

t("mp4 by content-type", detect(R("https://a.com/v/clip.mp4", "video/mp4", { "Content-Length": "5000000" }))?.kind === "media");
t("mp3 audio", detect(R("https://a.com/s?id=1", "audio/mpeg", { "Content-Length": "4000000" }))?.kind === "media");
t("small ui sound ignored", detect(R("https://a.com/click.mp3", "audio/mpeg", { "Content-Length": "9000" })) === null);
t("206 uses content-range total", detect(R("https://a.com/x.mp4", "video/mp4", { "Content-Range": "bytes 0-1/7000000", "Content-Length": "2" }, { statusCode: 206 }))?.size === 7000000);
t("m3u8 stream (even small)", detect(R("https://a.com/index.m3u8?t=1", "application/vnd.apple.mpegurl", { "Content-Length": "300" }))?.kind === "stream");
t("m3u8 octet-stream by ext", detect(R("https://a.com/x/master.m3u8", "application/octet-stream"))?.kind === "stream");
t("mpd by type", detect(R("https://a.com/m", "application/dash+xml"))?.kind === "stream");
t("ts segment ignored", detect(R("https://a.com/seg1.ts", "video/mp2t", { "Content-Length": "900000" })) === null);
t("m4s segment ignored", detect(R("https://a.com/seg1.m4s", "video/mp4", { "Content-Length": "900000" })) === null);
t("octet-stream .mp4 media", detect(R("https://a.com/f.mp4", "application/octet-stream", { "Content-Length": "9000000" }))?.kind === "media");
t("html .mp4 ignored", detect(R("https://a.com/f.mp4", "text/html"))=== null);
t("youtube skipped", detect(R("https://r1.googlevideo.com/videoplayback?x", "video/mp4", { "Content-Length": "9000000" })) === null);
t("extension's own requests ignored", detect(R("http://127.0.0.1:9614/ping", "video/mp4", {}))=== null);
t("tabId -1 ignored", detect(R("https://a.com/v.mp4", "video/mp4", { "Content-Length": "9000000" }, { tabId: -1 })) === null);
t("404 ignored", detect(R("https://a.com/v.mp4", "video/mp4", {}, { statusCode: 404 })) === null);
t("image ignored", detect(R("https://a.com/i.png", "image/png")) === null);

(async () => {
  const big = { "Content-Length": "5000000" };
  listeners.wr(R("https://a.com/v1.mp4", "video/mp4", big));
  listeners.wr(R("https://a.com/v1.mp4", "video/mp4", big));          // مكرر
  listeners.wr(R("https://a.com/index.m3u8", "application/x-mpegurl"));
  await new Promise(r => setTimeout(r, 50));
  t("dedupe + store (2 items)", store.m5?.length === 2);
  t("badge shows 2", global.badge?.text === "2" && global.badge?.tabId === 5);
  listeners.upd(5, { status: "loading", url: "https://a.com/other" });
  await new Promise(r => setTimeout(r, 50));
  t("navigation clears list", store.m5 === undefined);
  const resp = await new Promise(res => listeners.msg({ type: "send", url: "https://a.com/index.m3u8", kind: "stream", title: "عنوان", referer: "https://a.com/p" }, null, res));
  t("send -> handoff body", resp.ok && sent.kind === "stream" && sent.title === "عنوان" && sent.headers.Referer === "https://a.com/p" && sent.headers.Cookie === "a=b");
  const r2 = await new Promise(res => listeners.msg({ type: "send", url: "https://www.youtube.com/watch?v=1", kind: "ytdlp", mode: "audio", title: "T", referer: "" }, null, res));
  t("ytdlp send body", r2.ok && sent.kind === "ytdlp" && sent.mode === "audio" && sent.url.includes("youtube"));
  await listeners.ctx({ menuItemId: "pydm-page", pageUrl: "https://www.youtube.com/watch?v=2" });
  t("page context menu -> ytdlp video", sent.kind === "ytdlp" && sent.mode === "video" && sent.url.endsWith("v=2"));
  await listeners.ctx({ menuItemId: "pydm", linkUrl: "https://a.com/f.zip", pageUrl: "https://a.com/" });
  t("link context menu plain", !sent.kind && sent.url.endsWith("f.zip") && sent.headers.Referer === "https://a.com/");
  // التقاط التحميلات
  const det = item => new Promise(res => listeners.det(Object.assign({ referrer: "https://a.com/p", filename: "", mime: "" }, item), () => res()));
  global.cancelled = []; sent = null;
  await det({ id: 1, url: "https://a.com/photo.jpg", mime: "image/jpeg" });
  t("image left to the browser", !global.cancelled.length && sent === null);
  await det({ id: 2, url: "https://cdn.x/i?id=9", mime: "image/webp" });
  t("image by mime left to the browser", !global.cancelled.length && sent === null);
  await det({ id: 3, url: "https://a.com/r?go=1", finalUrl: "https://dl.a.com/pack.zip", filename: "C:\\D\\pack.zip", mime: "application/zip" });
  t("zip captured via final url and cancelled in browser", global.cancelled.includes(3) && sent.url === "https://dl.a.com/pack.zip" && sent.title === "pack.zip" && !sent.cookies);
  global.localStore = { skipImages: false }; sent = null;
  await det({ id: 4, url: "https://a.com/photo.png", mime: "image/png" });
  t("images captured when the option is off", global.cancelled.includes(4));
  global.localStore = { enabled: false }; sent = null;
  await det({ id: 5, url: "https://a.com/f.exe" });
  t("nothing captured when disabled", !global.cancelled.includes(5) && sent === null);
  global.localStore = {}; appUp = false;
  await det({ id: 6, url: "https://a.com/f.exe" });
  t("browser keeps the download when PyDM is closed", !global.cancelled.includes(6));
  appUp = true;
  await det({ id: 7, url: "blob:https://a.com/uuid" });
  t("blob left to the browser", !global.cancelled.includes(7));
  // yt-dlp requests carry cookies
  await listeners.ctx({ menuItemId: "pydm-page", pageUrl: "https://www.facebook.com/watch/?v=1" });
  t("yt-dlp handoff includes cookies", Array.isArray(sent.cookies) && sent.cookies[0].name === "a");
  console.log(ok ? "\nALL OK" : "\nSOME FAILED");
})();
