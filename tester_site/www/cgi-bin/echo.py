#!/usr/bin/env python3
import os, sys
body = sys.stdin.read() if os.environ.get("REQUEST_METHOD") in ("POST", "PUT") else ""
print("Content-Type: text/plain")
print()
for k in ("REQUEST_METHOD", "QUERY_STRING", "CONTENT_LENGTH", "CONTENT_TYPE", "SERVER_PROTOCOL",
          "SERVER_PORT", "SERVER_NAME", "GATEWAY_INTERFACE", "SCRIPT_NAME", "SCRIPT_FILENAME",
          "PATH_INFO", "REMOTE_ADDR", "HTTP_HOST", "HTTP_COOKIE", "HTTP_X_CUSTOM"):
    print("%s=%s" % (k, os.environ.get(k, "")))
print("LEN=%d" % len(body))
print("BODY=" + body)
