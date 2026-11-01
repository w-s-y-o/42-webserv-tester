#!/usr/bin/env python3
import os, time
q = os.environ.get("QUERY_STRING", "")
t = float(q.split("t=")[1].split("&")[0]) if "t=" in q else 2
time.sleep(t)
print("Content-Type: text/plain")
print()
print("SLEPT")
