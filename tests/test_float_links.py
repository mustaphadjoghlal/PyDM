import asyncio, json
from playwright.async_api import async_playwright
FLOAT = open("/home/claude/pydm/extension/float.js", encoding="utf-8").read()
MP4 = open("/home/claude/t/www/direct.mp4", "rb").read()
MOCK = r"""<script>window.__sent=[];window.chrome={runtime:{sendMessage(m,cb){if(m.type==="media")return cb({items:[]});__sent.push(m);setTimeout(()=>cb({ok:true}),20);}},
storage:{local:{get(k,cb){cb({})}},onChanged:{addListener(){}}}};</script>"""
def V(id, w=500, h=280): return f'<video id="{id}" src="/clip.mp4" width={w} height={h} muted></video>'
def article(n, vid_link):
    return f'''<div role="article" style="padding:20px;border:1px solid #444;margin:10px 0">
      <a href="/profile.php?id=10000{n}">Page {n}</a> · <a href="{vid_link}">2h</a>
      <div>{V("v"+str(n))}</div><a href="/groups/123456789{n}/">group</a></div>'''
FB_FEED = f'<div id=feed>{article(1,"/watch/?v=111111")}{article(2,"/somepage/videos/222222/")}{article(3,"/reel/333333")}</div>'
FB_IDS = f'<div><div data-video-id="444444"><div>{V("a")}</div></div><div data-video-id="555555"><div>{V("b")}</div></div></div>'
YT_WATCH = f'''<div style="display:flex;gap:20px"><div id=movie_player style="position:relative">{V("main",800,450)}
  <a href="/watch?v=ENDSCREEN1" style="position:absolute;left:300px;top:200px;width:0;height:0;overflow:hidden">end</a></div>
  <div><a href="/watch?v=SIDE1" style="display:block"><div>{V("side",320,180)}</div>Side video</a></div></div>
  <p><a href="/watch?v=MAIN123&t=30">0:30</a></p>'''
cases = [
  ("facebook feed: 2nd post",  "https://www.facebook.com/",               FB_FEED, "#v2", "https://www.facebook.com/somepage/videos/222222/"),
  ("facebook feed: 3rd post",  "https://www.facebook.com/",               FB_FEED, "#v3", "https://www.facebook.com/reel/333333"),
  ("facebook feed: 1st post",  "https://www.facebook.com/",               FB_FEED, "#v1", "https://www.facebook.com/watch/?v=111111"),
  ("facebook Watch tab (/watch/ is not a video)", "https://www.facebook.com/watch/", FB_FEED, "#v2", "https://www.facebook.com/somepage/videos/222222/"),
  ("facebook data-video-id",   "https://www.facebook.com/",               FB_IDS,  "#b",  "https://www.facebook.com/watch/?v=555555"),
  ("youtube watch: main player ignores hidden end-screen", "https://www.youtube.com/watch?v=MAIN123", YT_WATCH, "#main", "https://www.youtube.com/watch?v=MAIN123"),
  ("youtube watch: sidebar preview", "https://www.youtube.com/watch?v=MAIN123", YT_WATCH, "#side", "https://www.youtube.com/watch?v=SIDE1"),
]
async def main():
    ok = True
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
        for name, url, body, sel, want in cases:
            pg = await b.new_page(viewport={"width": 1300, "height": 2400})
            html = f"<!doctype html><html><head><meta charset=utf-8>{MOCK}</head><body style='margin:0;background:#18191a;color:#ccc'>{body}</body></html>"
            def make(page_html):
                async def h(route):
                    if route.request.url.endswith("/clip.mp4"): await route.fulfill(body=MP4, content_type="video/mp4")
                    else: await route.fulfill(body=page_html, content_type="text/html")
                return h
            await pg.route("**/*", make(html))
            await pg.goto(url); await pg.add_script_tag(content=FLOAT); await pg.wait_for_timeout(200)
            r = await pg.eval_on_selector(sel, "e=>{const r=e.getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]}")
            await pg.mouse.move(r[0], r[1]); await pg.wait_for_timeout(300)
            box = await pg.evaluate("(()=>{const r=document.querySelector('pydm-float').getBoundingClientRect();return [r.left,r.top]})()")
            await pg.mouse.click(box[0] + 30, box[1] + 17); await pg.wait_for_timeout(200)
            sent = await pg.evaluate("__sent")
            got = sent[0]["url"] if sent else None
            good = got == want
            ok &= good
            print(("PASS " if good else "FAIL ") + name, "" if good else f"got {got} want {want}")
            await pg.close()
        await b.close()
    print("\nALL OK" if ok else "\nSOME FAILED")
asyncio.run(main())
