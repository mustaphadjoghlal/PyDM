import asyncio, json
from playwright.async_api import async_playwright
FLOAT = open("/home/claude/pydm/extension/float.js", encoding="utf-8").read()
MP4 = open("/home/claude/t/www/direct.mp4", "rb").read()
MOCK = r"""
window.__sent = [];
window.chrome = { runtime: { sendMessage(msg, cb) {
    if (msg.type === "media") return cb({ items: window.__media || [] });
    window.__sent.push(msg); setTimeout(() => cb({ ok: true }), 50);
  } },
  storage: { local: { get(k, cb) { cb({}); } }, onChanged: { addListener() {} } } };
"""
def page_html(title, extra=""):
    return f"""<!doctype html><html><head><meta charset=utf-8><title>{title}</title></head>
<body style="margin:0;background:#111;font:16px sans-serif;color:#ccc">
<div style="padding:30px"><video id=v src="/clip.mp4" width=720 height=405 muted controls></video>{extra}
<p>نص الصفحة</p><video id=small src="/clip.mp4" width=160 height=90 muted></video></div></body></html>"""
ok = True
def check(n, c, e=""):
    global ok; print(("PASS " if c else "FAIL ") + n, e); ok &= bool(c)
async def run(b, url, html, media=None, shot=None, menu_shot=None):
    pg = await b.new_page(viewport={"width": 1100, "height": 650})
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    async def handler(route):
        u = route.request.url
        if u.endswith("/clip.mp4"): await route.fulfill(body=MP4, content_type="video/mp4")
        else: await route.fulfill(body=html, content_type="text/html")
    await pg.route("**/*", handler)
    await pg.add_init_script(MOCK + (f"window.__media={json.dumps(media)};" if media else ""))
    await pg.goto(url); await pg.add_script_tag(content=FLOAT); await pg.wait_for_timeout(300)
    await pg.mouse.move(400, 250); await pg.wait_for_timeout(400)
    vis = await pg.evaluate("getComputedStyle(document.querySelector('pydm-float')).display")
    if shot: await pg.screenshot(path=shot, clip={"x": 0, "y": 0, "width": 800, "height": 460})
    # الضغط على الزر الرئيسي: موقعه أعلى يمين الفيديو
    box = await pg.evaluate("(()=>{const r=document.querySelector('pydm-float').getBoundingClientRect();return [r.left,r.top,r.width,r.height]})()")
    await pg.mouse.move(box[0] + 30, box[1] + 17); await pg.mouse.click(box[0] + 30, box[1] + 17); await pg.wait_for_timeout(250)
    sent = await pg.evaluate("window.__sent")
    # القائمة
    await pg.mouse.click(box[0] + box[2] - 12, box[1] + 17); await pg.wait_for_timeout(250)
    if menu_shot: await pg.screenshot(path=menu_shot, clip={"x": 0, "y": 0, "width": 800, "height": 460})
    await pg.mouse.move(150, 600); await pg.wait_for_timeout(200)
    await pg.mouse.move(110, 560); await pg.wait_for_timeout(300)   # فوق الفيديو الصغير
    await pg.close()
    return vis, sent, errs, box

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox", "--autoplay-policy=no-user-gesture-required"])
        vis, sent, errs, box = await run(b, "https://example.org/watch-page", page_html("Some site"), shot="/home/claude/t/shots/float_generic.png")
        check("generic: button visible on hover", vis == "block", vis)
        check("generic: sends direct mp4 src", sent and sent[0]["url"].endswith("/clip.mp4") and sent[0]["kind"] == "", sent)
        check("generic: placed at top-right of video", 30 + 720 - 160 < box[0] + box[2] <= 30 + 720 - 5 and 35 <= box[1] <= 50, box)
        check("no page errors", not errs, errs)
        media = [{"url": "https://cdn.example.org/hls/master.m3u8", "kind": "stream", "name": "master.m3u8", "size": 0},
                 {"url": "https://cdn.example.org/a.mp4", "kind": "media", "name": "a.mp4", "size": 9000000}]
        vis, sent, errs, _ = await run(b, "https://example.org/x", page_html("Streams"), media=media, menu_shot="/home/claude/t/shots/float_menu_generic.png")
        check("stream preferred over src", sent and sent[0]["kind"] == "stream" and sent[0]["url"].endswith("master.m3u8"), sent)
        vis, sent, errs, _ = await run(b, "https://www.youtube.com/watch?v=UgUgz4T3_wc", page_html("Jolene - YouTube"),
                                       shot="/home/claude/t/shots/float_youtube.png", menu_shot="/home/claude/t/shots/float_menu_youtube.png")
        check("youtube: yt-dlp with page url", sent and sent[0]["kind"] == "ytdlp" and sent[0]["url"].endswith("v=UgUgz4T3_wc") and sent[0]["mode"] == "video", sent)
        feed = page_html("YouTube", '<a href="/watch?v=FEED123" style="display:block">thumb</a>')
        feed = feed.replace('<video id=v', '<a href="/watch?v=FEED123"><video id=v').replace('controls></video>', 'controls></video></a>', 1)
        vis, sent, errs, _ = await run(b, "https://www.youtube.com/", feed)
        check("youtube feed: uses the video's own link", sent and sent[0]["url"].endswith("v=FEED123"), sent)
        await b.close()
    print("\nALL OK" if ok else "\nSOME FAILED")
asyncio.run(main())
