#!/usr/bin/env python3
import sys, os, time, json
args = sys.argv[1:]
open(os.environ["FAKE_LOG"], "a").write(json.dumps(args) + "\n")
if "--flat-playlist" in args:
    if os.environ.get("FAKE_PL") == "empty":
        sys.stderr.write("ERROR: [youtube:tab] PLx: The playlist does not exist.\n"); sys.exit(1)
    rows = [("قائمة: دروس/المونتاج", "https://www.youtube.com/watch?v=aaa", "الدرس الأول"),
            ("قائمة: دروس/المونتاج", "https://www.youtube.com/watch?v=bbb", "[Private video]"),
            ("قائمة: دروس/المونتاج", "https://www.youtube.com/watch?v=ccc", "الدرس الثاني"),
            ("قائمة: دروس/المونتاج", "https://www.youtube.com/watch?v=aaa", "الدرس الأول"),
            ("قائمة: دروس/المونتاج", "https://www.youtube.com/watch?v=ddd", "Lesson 3")]
    sys.stdout.buffer.write("".join("\t".join(r) + "\n" for r in rows).encode("utf-8")); sys.exit(0)
out = args[args.index("-o") + 1].replace("%%", "%")
mode = os.environ.get("FAKE_MODE", "ok")
if "--cookies" in args and os.environ.get("FAKE_COPY_COOKIES"):
    open(os.environ["FAKE_COPY_COOKIES"], "w").write(open(args[args.index("--cookies") + 1]).read())
url = args[-1]
audio = "-x" in args
ext = "mp3" if audio else "mp4"
path = out.replace("%(title).150B", "عنوان " + url.split("=")[-1]).replace("%(ext)s", ext)
if mode == "fail":
    print("[youtube] abc: Downloading webpage", flush=True)
    print("ERROR: [youtube] abc: Video unavailable", flush=True)
    sys.exit(1)
print("[download] Destination: " + path, flush=True)
total = 1000000
for i in range(1, 11):
    print(f"PYDM {i*100000} {total} NA {123456}", flush=True)
    time.sleep(float(os.environ.get("FAKE_DELAY", "0.05")))
open(path, "wb").write(b"x" * total)
print("PYDMFILE " + path, flush=True)
