#!/usr/bin/env python3
import os, uuid
sid = None
for part in os.environ.get("HTTP_COOKIE", "").split(";"):
    part = part.strip()
    if part.startswith("sid="):
        sid = part[4:]
base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions")
os.makedirs(base, exist_ok=True)
new = False
if not sid or not sid.isalnum():
    sid = uuid.uuid4().hex
    new = True
f = os.path.join(base, sid)
count = int(open(f).read() or 0) if os.path.exists(f) else 0
count += 1
open(f, "w").write(str(count))
print("Content-Type: text/plain")
if new:
    print("Set-Cookie: sid=%s; Path=/; HttpOnly" % sid)
print()
print("SESSION=%s" % sid)
print("VISITS=%d" % count)
