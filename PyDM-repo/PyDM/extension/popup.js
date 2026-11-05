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

(async () => {
  const { enabled = true } = await chrome.storage.local.get("enabled");
  $("en").checked = enabled;
  ping();
})();

$("en").addEventListener("change", e => chrome.storage.local.set({ enabled: e.target.checked }));

$("send").addEventListener("click", async () => {
  const url = $("url").value.trim();
  if (!/^https?:\/\//i.test(url)) { $("msg").textContent = "أدخل رابطاً صحيحاً"; return; }
  try {
    const r = await fetch(API + "/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, headers: {} })
    });
    $("msg").textContent = r.ok ? "تمت الإضافة ✔" : "رفض البرنامج الرابط";
    if (r.ok) $("url").value = "";
  } catch (e) { $("msg").textContent = "افتح برنامج PyDM أولاً"; }
});
