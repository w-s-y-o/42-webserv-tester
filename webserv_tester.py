#!/usr/bin/env python3
"""
webserv_tester.py (v5) - black-box tester for the 42 "Webserv" project (subject v24.1)

Python 3.6+, standard library only.

Menu:  1 Mandatory   2 Bonus   3 Both (1 + 2)   4 Extra tests (optional features)

Tests are grouped in titled sections, from the simplest checks to the most intense:
  MANDATORY  1 Checking valid/invalid ports and config file   (starts your binary)
             2 Basic GET and static website
             3 Response format and headers
             4 URL validation
             5 HTTP methods
             6 Request line and header validation
             7 Error pages and status codes
             8 Directories, index, autoindex and redirects
             9 Request body: Content-Length, chunked, max body size
            10 Upload, POST and DELETE
            11 CGI basics            12 CGI advanced
            13 Data arriving in pieces (accumulating requests)
            14 Connection handling
            15 Checking sockets non-blocking
            16 Multiple ports and your own routes
            17 Resilience and stress (intense)
            18 Timeouts: a request must never hang forever
            19 Memory leaks (repeat the same action and watch the server)
            20 Final health check
  BONUS      Cookies / Sessions / Multiple CGI types / Cookie robustness
  EXTRA      Optional HTTP features, pipelining, edge cases, virtual hosting

MEMORY LEAK VERDICT (printed for every test)
  The tester finds your server process (from the listening port, or --pid N) and,
  after each test, checks that it did not keep file descriptors, leave zombie
  children, or keep a large amount of memory. Section 19 repeats one action
  thousands of times and fails if memory/descriptors keep growing.
  On Linux, the one-command launcher automatically uses Valgrind if installed:
  live runtime errors are checked after each case, while heap leaks are checked
  once at process exit. Without Valgrind, FD/zombie/RSS checks still run.

QUICK START (from the Webserv project root)
  make
  ./webserv_tester/run.sh                       # menu, auto configs and launch
  ./webserv_tester/run.sh --choice 1            # Mandatory, tests run once
  ./webserv_tester/run.sh --choice 1 --no-valgrind  # native-speed tests

OPTIONS
  --host/--port        server address (default 127.0.0.1:8080)
  --config FILE        JSON file overriding CONFIG keys
  --bin PATH           your webserv binary   (enables section 1: startup/port tests)
  --conf-template FILE a working config where the port is written {{PORT}}
  --choice 1|2|3|4     skip the menu
  --section TEXT       only run sections whose title contains TEXT (e.g. "URL")
  --quick              optionally skip slow tests (same Mandatory category)
  --no-valgrind        run without Valgrind instrumentation (faster)
  --pid N              PID of the running server (default: found from the port)
  --no-memory          turn the memory leak checks off
  --mem-scale F        multiply the repetitions of the leak hunts (0.2 under valgrind)
  --valgrind-log GLOB  monitor an externally instrumented server (explicit --stop-server to finalize)
  --stop-signal NAME   signal used to stop the server (default SIGINT)
  --leaks              macOS: also run `leaks PID` after every test
  --list               list sections and test counts, then exit
  --gen-samples DIR    write sample site + example config, then exit

Tests needing an unconfigured route/feature are shown as SKIP and not counted.
Rules that need a code review (single poll(), no errno after read/write, fork
only for CGI, C++98, Makefile...) cannot be checked from outside.
"""

import argparse
import atexit
import glob
import json
import os
import random
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid

# --------------------------------------------------------------------------
# CONFIGURATION  (None / empty = "not configured" -> the test is skipped)
# --------------------------------------------------------------------------
CONFIG = {
    "host": "127.0.0.1",
    "port": 8080,
    "extra_ports": [],                 # other ports your server listens on, e.g. [8081]
    "extra_port_expect": {},           # {"8081": "text found in / on that port"}
    "vhosts": [],                      # [["site2.local", "text expected in /"]] (optional)

    "server_bin": None,                # path to ./webserv (section 1)
    "conf_template": None,             # working config with {{PORT}} (and optional {{PORT2}})
    "server_cwd": None,                # cwd used to launch the server (default: template dir)

    "www_root": None,                  # disk path served by "/"
    "index_path": "/",
    "index_contains": None,
    "static_files": [["/index.html", "text/html"], ["/about.html", "text/html"]],
    "download_files": [],              # [["/big.bin", "/abs/disk/path/big.bin"]]
    "missing_path": "/this_page_does_not_exist_12345",
    "custom_404_contains": None,

    "get_only_path": None,             # route refusing POST/DELETE (-> 405)
    "post_path": None,                 # route accepting any POST body (else CGI echo.py)
    "upload_path": None,               # multipart upload route, e.g. "/upload"
    "upload_dir": None,                # disk dir where uploads land (traversal check)
    "upload_fetch_prefix": None,       # where uploaded files can be fetched, e.g. "/uploads/"
    "delete_prefix": None,             # where DELETE works, e.g. "/uploads/"
    "upload_big_size": 200000,         # bytes, must be under your body limit
    "max_body_path": None,             # route with a small client_max_body_size
    "max_body_size": None,

    "autoindex_on_path": None,
    "autoindex_off_path": None,
    "redirect_path": None,
    "redirect_location": None,

    "cgi_base": None,                  # route of the sample scripts, e.g. "/cgi-bin"
    "cgi_big_body": 200000,            # POST size sent to the CGI (pipe deadlock test)
    "cgi_types": [["hello.py", "Hello from CGI (python)"]],  # extra CGI types need configuration

    "custom_tests": [],                # [["GET", "/my/route", 200, "expected text"], ...]

    "stress_clients": [100, 300],
    "siege_seconds": 6,
    "hang_timeout": 65,
    "cgi_timeout_wait": 40,

    # memory / resource checks
    "server_pid": None,                # PID of the running webserv (auto-detected from the port)
    "memory_check": True,
    "mem_settle": 3.0,                 # seconds to wait for the server to release resources
    "mem_fd_tolerance": 0,             # extra open descriptors tolerated after a test
    "mem_rss_jump_kb": 65536,          # per-test: memory growth that stays after the test
    "mem_rss_tolerance_kb": 1024,      # leak hunts: growth over 3 batches (must rise every batch)
    "mem_hunt_scale": 1.0,             # multiplies the repetitions of the leak hunts
    "valgrind_log": None,              # glob of the valgrind log written by your server run
    "stop_signal": "SIGINT",           # used to stop the server before reading the log
    "use_leaks_tool": False,           # macOS: run `leaks PID` after every test
}

MANDATORY, BONUS, EXTRA = "mandatory", "bonus", "extra"
GROUP_ORDER = {MANDATORY: 0, BONUS: 1, EXTRA: 2}
QUICK = False
STATE = {}
MEM_WARNINGS = []   # RSS trends are diagnostic; they do not by themselves prove leaks.
TESTS = []                  # (group, section, name, fn, slow, mem)
SECTIONS = []               # (group, title)
CUR = {"group": MANDATORY, "section": "", "mem": "yes"}
PROCS, TMP = [], []
AUTO_PROCESS = None
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

USE_COLOR = sys.stdout.isatty()


def c(text, code):
    return "\033[%sm%s\033[0m" % (code, text) if USE_COLOR else text


GREEN, RED, YELLOW, CYAN, BOLD = "32", "31", "33", "36", "1"


class Skip(Exception):
    pass


def _cleanup():
    for p in PROCS:
        try:
            if p.poll() is None:
                p.kill()
                p.wait()
        except Exception:
            pass
    for d in TMP:
        shutil.rmtree(d, ignore_errors=True)


atexit.register(_cleanup)


# --------------------------------------------------------------------------
# Tiny HTTP client
# --------------------------------------------------------------------------
class Resp(object):
    def __init__(self):
        self.raw = b""
        self.head = b""
        self.version = ""
        self.status = None
        self.reason = ""
        self.headers = {}
        self.header_list = []
        self.body = b""
        self.extra = b""
        self.closed = False
        self.reset = False
        self.timed_out = False
        self.well_formed = False

    def h(self, name):
        return self.headers.get(name.lower())

    def all_h(self, name):
        return [v for k, v in self.header_list if k.lower() == name.lower()]

    def summary(self):
        if self.status is None:
            if self.reset:
                return "connection reset, no response"
            if self.closed and not self.raw:
                return "connection closed, no response"
            if self.timed_out and not self.raw:
                return "no response (timeout)"
            return "unparsable response: %r" % self.raw[:60]
        return "%d %s" % (self.status, self.reason)


STATUS_RE = re.compile(rb"^HTTP/(1\.[01]) (\d{3})(?: (.*))?$")


def decode_chunked(data):
    out, pos = b"", 0
    while True:
        end = data.find(b"\r\n", pos)
        if end == -1:
            return out, False, 0
        try:
            size = int(data[pos:end].split(b";")[0], 16)
        except ValueError:
            return out, True, len(data)
        pos = end + 2
        if size == 0:
            tail = data.find(b"\r\n", pos)
            if tail == -1:
                return out, False, 0
            return out, True, tail + 2
        if len(data) < pos + size + 2:
            return out, False, 0
        out += data[pos:pos + size]
        pos += size + 2


def read_response(s, timeout=5.0, buf=b"", no_body=False):
    r = Resp()
    deadline = time.time() + timeout

    def recv_some(limit=None):
        remaining = deadline - time.time() if limit is None else limit
        if remaining <= 0:
            r.timed_out = True
            return None
        s.settimeout(remaining)
        try:
            d = s.recv(65536)
        except socket.timeout:
            r.timed_out = True
            return None
        except OSError:
            r.reset = True
            r.closed = True
            return None
        if not d:
            r.closed = True
            return None
        return d

    while b"\r\n\r\n" not in buf:
        d = recv_some()
        if d is None:
            break
        buf += d
    r.raw = buf
    if b"\r\n\r\n" not in buf:
        return r
    head, _, body = buf.partition(b"\r\n\r\n")
    r.head = head
    lines = head.split(b"\r\n")
    m = STATUS_RE.match(lines[0])
    if not m:
        return r
    r.version = m.group(1).decode()
    r.status = int(m.group(2))
    r.reason = (m.group(3) or b"").decode("latin-1")
    r.well_formed = True
    for ln in lines[1:]:
        if b":" not in ln:
            r.well_formed = False
            continue
        k, v = ln.split(b":", 1)
        k, v = k.decode("latin-1").strip(), v.decode("latin-1").strip()
        r.header_list.append((k, v))
        r.headers[k.lower()] = v
    if no_body:
        d = recv_some(0.3)
        r.extra = body + (d or b"")
        r.timed_out = False
        return r
    if r.status == 204 or r.status == 304 or 100 <= r.status < 200:
        r.extra = body
        return r
    te = r.headers.get("transfer-encoding", "").lower()
    cl = r.headers.get("content-length", "")
    if "chunked" in te:
        while True:
            dec, done, used = decode_chunked(body)
            if done:
                r.body, r.extra = dec, body[used:]
                break
            d = recv_some()
            if d is None:
                r.body = dec
                break
            body += d
    elif cl.isdigit():
        n = int(cl)
        while len(body) < n:
            d = recv_some()
            if d is None:
                break
            body += d
        r.body, r.extra = body[:n], body[n:]
    else:
        while True:
            d = recv_some()
            if d is None:
                break
            body += d
        r.body = body
    return r


def open_sock(port=None, timeout=5, rcvbuf=None):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if rcvbuf:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.settimeout(timeout)
    s.connect((CONFIG["host"], port or CONFIG["port"]))
    return s


def send_recv(raw, port=None, timeout=5.0, no_body=False):
    s = open_sock(port)
    try:
        try:
            s.sendall(raw)
        except OSError:
            pass
        return read_response(s, timeout, no_body=no_body)
    finally:
        s.close()


def build(method, path, version="HTTP/1.1", headers=None, body=b"", host=True, port=None):
    if isinstance(body, str):
        body = body.encode()
    hl = list(headers or [])
    names = [k.lower() for k, _ in hl]
    if host and "host" not in names:
        hl.insert(0, ("Host", "%s:%d" % (CONFIG["host"], port or CONFIG["port"])))
    if body and "content-length" not in names and "transfer-encoding" not in names:
        hl.append(("Content-Length", str(len(body))))
    head = "%s %s %s\r\n" % (method, path, version)
    head += "".join("%s: %s\r\n" % (k, v) for k, v in hl) + "\r\n"
    return head.encode("latin-1") + body


def ssend(s, data):
    """sendall that tolerates a server which already closed the connection."""
    try:
        s.sendall(data)
        return True
    except OSError:
        return False


def chunk_encode(data, size=4):
    out = b""
    for i in range(0, len(data), size):
        part = data[i:i + size]
        out += b"%x\r\n" % len(part) + part + b"\r\n"
    return out + b"0\r\n\r\n"


def get(path, headers=None, port=None, timeout=5.0):
    return send_recv(build("GET", path, headers=headers, port=port), port, timeout)


def idx():
    return CONFIG["index_path"]


def alive():
    for _i in range(2):
        try:
            if get(idx(), timeout=5).status is not None:
                return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


def parallel(fn, n, join_timeout=120):
    results = [None] * n

    def w(i):
        try:
            results[i] = fn(i)
        except Exception as e:  # noqa
            results[i] = e
    ths = [threading.Thread(target=w, args=(i,), daemon=True) for i in range(n)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(join_timeout)
    return results


# --------------------------------------------------------------------------
# Memory / resource monitor (watches the running server process)
# --------------------------------------------------------------------------
MON = None
LEAKS = {"count": 0, "bytes": 0}
VALGRIND_ACTIVE = False
VG_POSITIONS = {}
VG_PARTIAL_LINES = {}
VG_RUNTIME_ERRORS = 0
VG_ERROR_LINE = re.compile(
    r"^==\d+==\s+(?:Invalid (?:read|write|free|jump)|Mismatched free|"
    r"Use of uninitialised|Conditional jump or move depends on uninitialised|"
    r"Syscall param|Jump to the invalid|Source and destination overlap|"
    r"Invalid alignment|Unaddressable byte)", re.IGNORECASE)


def valgrind_new_errors():
    """Best-effort live errors since the previous case; heap leaks need process exit.

    Valgrind may merge duplicate errors or emit reports after the triggering
    request has ended. Never claim an exact per-request heap-leak verdict.
    """
    global VG_RUNTIME_ERRORS
    hits = []
    for path in sorted(glob.glob(CONFIG.get("valgrind_log") or "")):
        try:
            with open(path, "rb") as log:
                log.seek(VG_POSITIONS.get(path, 0))
                data = log.read()
                VG_POSITIONS[path] = log.tell()
        except OSError:
            continue
        if not data:
            continue
        buffer = VG_PARTIAL_LINES.get(path, "") + data.decode("utf-8", "replace")
        lines = buffer.splitlines(True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            VG_PARTIAL_LINES[path] = lines.pop()
        else:
            VG_PARTIAL_LINES[path] = ""
        for line in lines:
            if VG_ERROR_LINE.search(line):
                hits.append(line.strip())
    VG_RUNTIME_ERRORS += len(hits)
    return hits


class Monitor(object):
    """Reads open descriptors, resident memory and zombie children of one process."""

    def __init__(self, pid):
        self.pid = pid
        self.linux = os.path.isdir("/proc/%d" % pid)

    def _snap_proc(self):
        pid = self.pid
        try:
            fds = len(os.listdir("/proc/%d/fd" % pid))
            rss = 0
            with open("/proc/%d/status" % pid) as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss = int(line.split()[1])
                        break
        except (OSError, ValueError):
            return None
        zombies = children = 0
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open("/proc/%s/stat" % d) as f:
                    data = f.read()
            except OSError:
                continue
            rest = data.rpartition(")")[2].split()
            if len(rest) > 1 and rest[1] == str(pid):
                children += 1
                if rest[0] == "Z":
                    zombies += 1
        return {"fds": fds, "rss": rss, "zombies": zombies, "children": children}

    def _snap_ps(self):
        try:
            out = subprocess.check_output(["ps", "-A", "-o", "pid=,ppid=,stat=,rss="],
                                          universal_newlines=True)
        except Exception:
            return None
        rss, zombies, children = None, 0, 0
        for line in out.splitlines():
            p = line.split()
            if len(p) < 4:
                continue
            if p[0] == str(self.pid):
                rss = int(p[3])
            if p[1] == str(self.pid):
                children += 1
                if p[2].startswith("Z"):
                    zombies += 1
        if rss is None:
            return None
        try:
            out = subprocess.check_output(["lsof", "-p", str(self.pid)], stderr=subprocess.DEVNULL,
                                          universal_newlines=True)
            fds = max(0, len(out.splitlines()) - 1)
        except Exception:
            fds = -1                      # lsof not available: descriptor check disabled
        return {"fds": fds, "rss": rss, "zombies": zombies, "children": children}

    def snap(self):
        return self._snap_proc() if self.linux else self._snap_ps()

    def settle(self, before, timeout):
        """Snapshot after waiting (up to timeout) for descriptors/zombies to go back down."""
        end = time.time() + timeout
        tol = CONFIG["mem_fd_tolerance"]
        while True:
            s = self.snap()
            if s is None:
                return None
            if (before["fds"] < 0 or s["fds"] <= before["fds"] + tol) and s["zombies"] <= before["zombies"]:
                return s
            if time.time() >= end:
                return s
            time.sleep(0.1)


def find_server_pid(port):
    if CONFIG.get("server_pid"):
        return int(CONFIG["server_pid"])
    inodes = set()                         # Linux: listening socket inode -> process
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(f) as fh:
                next(fh, None)
                for line in fh:
                    p = line.split()
                    if len(p) > 9 and p[3] == "0A" and int(p[1].rsplit(":", 1)[1], 16) == port:
                        inodes.add(p[9])
        except (OSError, ValueError):
            pass
    if inodes:
        found = []
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                for fd in os.listdir("/proc/%s/fd" % d):
                    try:
                        link = os.readlink("/proc/%s/fd/%s" % (d, fd))
                    except OSError:
                        continue
                    if link.startswith("socket:[") and link[8:-1] in inodes:
                        found.append(int(d))
                        break
            except OSError:
                continue
        if found:
            return min(found)
    try:                                   # macOS and others
        out = subprocess.check_output(["lsof", "-ti", "tcp:%d" % port, "-sTCP:LISTEN"],
                                      stderr=subprocess.DEVNULL, universal_newlines=True).split()
        if out:
            return min(int(x) for x in out)
    except Exception:
        pass
    return None


def leaks_run(pid):
    """macOS `leaks PID` -> (leak count, leaked bytes) or None."""
    try:
        r = subprocess.run(["leaks", str(pid)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=180)
    except Exception:
        return None
    m = re.search(r"(\d+) leaks? for (\d+) total leaked bytes", r.stdout)
    return (int(m.group(1)), int(m.group(2))) if m else None


def process_gone(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rpartition(")")[2].split()[0] == "Z"
    except (OSError, IndexError):
        return False


def parse_valgrind(text, pid):
    pre = "==%d==" % pid
    lines = [l for l in text.splitlines() if l.startswith(pre)]
    body = "\n".join(lines) if lines else text
    res = {}
    for kind, n in re.findall(r"(definitely lost|indirectly lost|possibly lost|still reachable):\s*([\d,]+) bytes", body):
        res[kind] = int(n.replace(",", ""))          # the last summary wins
    m = re.findall(r"ERROR SUMMARY:\s*([\d,]+) errors", body)
    errors = int(m[-1].replace(",", "")) if m else None
    return res, errors, "All heap blocks were freed" in body


def valgrind_check():
    """Stop the server and judge its valgrind log -> (ok, expected, got)."""
    exp = "server exits on %s; valgrind: no definite/indirect/possible leak, 0 errors" % CONFIG["stop_signal"]
    pid = MON.pid if MON else (int(CONFIG["server_pid"]) if CONFIG.get("server_pid") else None)
    if not pid:
        return False, exp, "server PID unknown (pass --pid N)"
    sig = getattr(signal, CONFIG["stop_signal"], None)
    if sig is None:
        return False, exp, "unknown signal %r" % CONFIG["stop_signal"]
    try:
        os.kill(pid, sig)
    except OSError as e:
        return False, exp, "cannot signal PID %d: %s" % (pid, e)
    end = time.time() + 20
    while time.time() < end and not process_gone(pid):
        time.sleep(0.2)
    if not process_gone(pid):
        return False, exp, "server still running 20 s after %s (catch the signal, leave the main loop, free everything)" % CONFIG["stop_signal"]
    if AUTO_PROCESS is not None:
        try:
            AUTO_PROCESS.wait(timeout=8)
        except subprocess.TimeoutExpired:
            pass
    time.sleep(0.5)
    files = sorted(glob.glob(CONFIG["valgrind_log"]))
    if not files:
        return False, exp, "no log file matches %r" % CONFIG["valgrind_log"]
    text = ""
    for f in files:
        with open(f, errors="replace") as fh:
            text += fh.read() + "\n"
    res, errors, freed = parse_valgrind(text, pid)
    if not res and not freed:
        return False, exp, "no valgrind leak report in the log (was the server started under valgrind --leak-check=full?)"
    lost = sum(res.get(k, 0) for k in ("definitely lost", "indirectly lost", "possibly lost"))
    got = "definitely lost %d B, indirectly lost %d B, possibly lost %d B, still reachable %d B (not counted), errors %s" % (
        res.get("definitely lost", 0), res.get("indirectly lost", 0), res.get("possibly lost", 0),
        res.get("still reachable", 0), errors if errors is not None else "?")
    if freed:
        got = "All heap blocks were freed, errors %s" % (errors if errors is not None else "?")
    return lost == 0 and errors == 0, exp, got


def warm_up():
    for _i in range(5):
        try:
            get(idx())
            get(CONFIG["missing_path"])
        except OSError:
            pass
    try:
        send_recv(build("POST", post_target(), headers=[("Content-Type", "text/plain")], body=b"warm"), timeout=3)
    except Exception:
        pass
    time.sleep(0.3)


def setup_memory():
    global MON
    if not CONFIG["memory_check"]:
        print("Memory monitor: OFF (--no-memory)")
        return
    pid = find_server_pid(CONFIG["port"])
    if pid is None:
        print(c("Memory monitor: OFF (server PID not found: pass --pid N)", YELLOW))
        return
    mon = Monitor(pid)
    warm_up()
    snap = mon.snap()
    if snap is None:
        print(c("Memory monitor: OFF (cannot read process %d)" % pid, YELLOW))
        return
    if CONFIG["use_leaks_tool"]:
        if not shutil.which("leaks"):
            print(c("`leaks` tool not found: --leaks ignored", YELLOW))
            CONFIG["use_leaks_tool"] = False
        else:
            base = leaks_run(pid)
            if base:
                LEAKS["count"], LEAKS["bytes"] = base
    MON = mon
    STATE["mem_baseline"] = snap
    print("Memory monitor: ON  (server PID %d | open fds %d | RSS %.1f MB | zombies %d)" % (
        pid, snap["fds"], snap["rss"] / 1024.0, snap["zombies"]))


def need_mon():
    if MON is None:
        raise Skip("server PID not found (pass --pid N)")
    return MON


# --------------------------------------------------------------------------
# Test registry and helpers
# --------------------------------------------------------------------------
def section(group, title, mem="yes"):
    CUR["group"], CUR["section"], CUR["mem"] = group, title, mem
    SECTIONS.append((group, title))


def add(name, fn, slow=False, mem=None):
    TESTS.append((CUR["group"], CUR["section"], name, fn, slow, mem or CUR["mem"]))


def test(name, slow=False, mem=None):
    def deco(fn):
        add(name, fn, slow, mem)
        return fn
    return deco


def need(key):
    v = CONFIG.get(key)
    if v in (None, "", [], (), {}):
        raise Skip("config '%s' not set" % key)
    return v


def cgi(name):
    return need("cgi_base").rstrip("/") + "/" + name


def www_file(rel):
    p = os.path.join(need("www_root"), rel)
    if not os.path.exists(p):
        raise Skip("%s not found in www_root (run --gen-samples)" % rel)
    return p


def post_target():
    for k in ("post_path",):
        if CONFIG.get(k):
            return CONFIG[k]
    if CONFIG.get("cgi_base"):
        return cgi("echo.py")
    if CONFIG.get("upload_path"):
        return CONFIG["upload_path"]
    raise Skip("config 'post_path' (or 'cgi_base') not set")


def big_resource():
    if CONFIG["download_files"]:
        url, disk = CONFIG["download_files"][0]
        with open(disk, "rb") as f:
            return url, f.read()
    root = CONFIG.get("www_root")
    if root and os.path.exists(os.path.join(root, "big.bin")):
        with open(os.path.join(root, "big.bin"), "rb") as f:
            return "/big.bin", f.read()
    if CONFIG.get("cgi_base"):
        return cgi("big.py"), b"A" * 1000000
    raise Skip("config 'download_files' / 'www_root' / 'cgi_base' not set")


OK_POST = {200, 201, 202, 204, 302, 303}
ANY_NO_CRASH = set(range(200, 300)) | {301, 302, 303, 307, 308, 400, 403, 404, 405, 411,
                                       413, 414, 415, 417, 431, 501, 505}


def status_in(r, allowed):
    allowed = set(allowed)
    parts = [str(a) for a in sorted(allowed) if a]
    exp = "status " + " or ".join(parts) if parts else "a clean close"
    if 0 in allowed and parts:
        exp += " (or a clean close)"
    ok = r.status in allowed or (0 in allowed and r.status is None and r.closed and not r.timed_out)
    return ok, exp, r.summary()


def survived(ok, exp, got):
    if not alive():
        return False, exp + " and server still alive", got + " / SERVER NO LONGER RESPONDING"
    return ok, exp + " and server still alive", got


def raw_case(name, raw, allowed, alive_check=True, slow=False, timeout=5):
    def fn():
        data = raw() if callable(raw) else raw
        res = status_in(send_recv(data, timeout=timeout), allowed)
        return survived(*res) if alive_check else res
    add(name, fn, slow)


def url_case(name, target, allowed, leak=False):
    def fn():
        t = target() if callable(target) else target
        r = send_recv(build("GET", t))
        ok, e, g = status_in(r, allowed)
        if leak and b"root:" in r.body:
            return False, "no file content leaked", "body contains 'root:'"
        return survived(ok, e, g)
    add(name, fn)


def cl_case(name, header_lines, allowed):
    raw_case(name, lambda: ("POST %s HTTP/1.1\r\nHost: x\r\nContent-Type: text/plain\r\n%s\r\n"
                            % (post_target(), header_lines)).encode() + b"hello", allowed)


# ==========================================================================
#                           MANDATORY TESTS
# ==========================================================================
M = MANDATORY

# --------------------------------------------------------------------------
section(M, "Checking valid/invalid ports and config file", mem="no")
# --------------------------------------------------------------------------
def free_ports(n=2):
    socks = [socket.socket() for _i in range(n)]
    for s in socks:
        s.bind((CONFIG["host"], 0))
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    return ports


def launch_ready():
    need("server_bin")
    need("conf_template")
    if not os.path.exists(CONFIG["server_bin"]):
        raise Skip("server_bin %s not found" % CONFIG["server_bin"])


def server_cwd():
    return CONFIG.get("server_cwd") or os.path.dirname(os.path.abspath(CONFIG["conf_template"]))


def tmp_path(name, data=b""):
    d = tempfile.mkdtemp(prefix="webserv_tester_")
    TMP.append(d)
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        f.write(data)
    return p


def template_text(**subs):
    launch_ready()
    with open(CONFIG["conf_template"]) as f:
        txt = f.read()
    p1, p2 = free_ports(2)
    subs.setdefault("PORT", p1)
    subs.setdefault("PORT2", p2)
    for k, v in subs.items():
        txt = txt.replace("{{%s}}" % k, str(v))
    return txt, subs


def conf_file(**subs):
    txt, s = template_text(**subs)
    return tmp_path("test.conf", txt.encode()), s


def spawn(args):
    launch_ready()
    p = subprocess.Popen([os.path.abspath(CONFIG["server_bin"])] + list(args), cwd=server_cwd(),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    PROCS.append(p)
    return p


def stop(p):
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(2)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


def wait_listen(port, p, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if p.poll() is not None:
            return False
        try:
            socket.create_connection((CONFIG["host"], int(port)), timeout=0.3).close()
            return True
        except OSError:
            time.sleep(0.05)
    return False


def wait_exit(p, timeout=3.0):
    try:
        return p.wait(timeout)
    except subprocess.TimeoutExpired:
        return None


def exit_desc(rc):
    if rc is None:
        return "still running (did not exit)"
    if rc < 0:
        try:
            return "CRASHED: killed by signal %s" % signal.Signals(-rc).name
        except ValueError:
            return "CRASHED: killed by signal %d" % -rc
    return "exit code %d" % rc


def expect_refuse(p):
    rc = wait_exit(p, 3)
    desc = exit_desc(rc)
    stop(p)
    return rc is not None and rc > 0, "exits with an error code (> 0), no crash", desc


def expect_no_crash(p, wait=1.5):
    rc = wait_exit(p, wait)
    desc = exit_desc(rc)
    stop(p)
    return rc is None or rc >= 0, "no crash (keeps running or exits cleanly)", desc


def is_root():
    return hasattr(os, "geteuid") and os.geteuid() == 0


@test("Server answers on the configured port")
def _():
    r = get(idx())
    return r.status is not None, "an HTTP answer", r.summary()


@test("Server starts on a free port and answers requests")
def _():
    conf, s = conf_file()
    p = spawn([conf])
    ok = wait_listen(s["PORT"], p)
    r = None
    if ok:
        r = send_recv(build("GET", "/", port=s["PORT"]), port=s["PORT"], timeout=3)
    desc = exit_desc(p.poll()) if not ok else r.summary()
    stop(p)
    return ok and r.status is not None and r.status < 500, \
        "listens on the port and answers (status < 500)", desc


@test("Server starts on the highest valid port (65535)")
def _():
    launch_ready()
    t = socket.socket()
    try:
        t.bind((CONFIG["host"], 65535))
    except OSError:
        raise Skip("port 65535 is in use on this machine")
    t.close()
    conf, s = conf_file(PORT=65535)
    p = spawn([conf])
    ok = wait_listen(65535, p)
    desc = "listening" if ok else exit_desc(p.poll())
    stop(p)
    return ok, "listens on port 65535", desc


@test("Server listens on two different ports from one config ({{PORT2}})")
def _():
    launch_ready()
    if "{{PORT2}}" not in open(CONFIG["conf_template"]).read():
        raise Skip("template has no {{PORT2}}")
    conf, s = conf_file()
    p = spawn([conf])
    a, b = wait_listen(s["PORT"], p), wait_listen(s["PORT2"], p)
    desc = "ports %s:%s, %s:%s" % (s["PORT"], "up" if a else "down", s["PORT2"], "up" if b else "down")
    stop(p)
    return a and b, "both ports accept connections", desc


@test("Same port written twice in one config does not crash ({{PORT2}})")
def _():
    launch_ready()
    if "{{PORT2}}" not in open(CONFIG["conf_template"]).read():
        raise Skip("template has no {{PORT2}}")
    port = free_ports(1)[0]
    conf, s = conf_file(PORT=port, PORT2=port)
    return expect_no_crash(spawn([conf]))


for _val in ["-1", "0", "65536", "99999", "4294967296", "99999999999999999999",
             "abc", "80.5", "8080abc", ""]:
    def _mk(val):
        def fn():
            conf, s = conf_file(PORT=val)
            return expect_refuse(spawn([conf]))
        return fn
    add("Invalid port %r is refused at startup" % _val, _mk(_val))


@test("Privileged port (80) without root: clean error, no crash")
def _():
    if is_root():
        raise Skip("running as root")
    conf, s = conf_file(PORT=80)
    return expect_no_crash(spawn([conf]))


@test("Port already used by another program: clean error, no crash")
def _():
    launch_ready()
    srv = socket.socket()
    srv.bind((CONFIG["host"], 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        conf, s = conf_file(PORT=port)
        return expect_refuse(spawn([conf]))
    finally:
        srv.close()


@test("Second instance on the same port exits cleanly, first keeps working")
def _():
    conf, s = conf_file()
    a = spawn([conf])
    if not wait_listen(s["PORT"], a):
        stop(a)
        return False, "first instance listens", exit_desc(a.poll())
    b = spawn([conf])
    res = expect_refuse(b)
    r = send_recv(build("GET", "/", port=s["PORT"]), port=s["PORT"], timeout=3)
    stop(a)
    ok = res[0] and r.status is not None
    return ok, "second exits with error, first still answers", \
        "second: %s; first: %s" % (res[2], r.summary())


@test("Restart right after stopping works (SO_REUSEADDR)")
def _():
    conf, s = conf_file()
    a = spawn([conf])
    if not wait_listen(s["PORT"], a):
        stop(a)
        return False, "first instance listens", exit_desc(a.poll())
    send_recv(build("GET", "/", headers=[("Connection", "close")], port=s["PORT"]), port=s["PORT"], timeout=3)
    stop(a)
    b = spawn([conf])
    ok = wait_listen(s["PORT"], b, 3)
    desc = "listening again" if ok else exit_desc(b.poll()) + " (Address already in use?)"
    stop(b)
    return ok, "restarted server listens on the same port at once", desc


@test("Missing config file: clean error exit")
def _():
    launch_ready()
    return expect_refuse(spawn(["/nonexistent/dir/none.conf"]))


@test("Config path is a directory: clean error exit")
def _():
    launch_ready()
    d = tempfile.mkdtemp(prefix="webserv_tester_")
    TMP.append(d)
    return expect_refuse(spawn([d]))


@test("Empty config file: no crash")
def _():
    launch_ready()
    return expect_no_crash(spawn([tmp_path("empty.conf", b"")]))


@test("Binary garbage as config file: no crash")
def _():
    launch_ready()
    random.seed(3)
    return expect_no_crash(spawn([tmp_path("junk.conf", bytes(random.randrange(256) for _i in range(4096)))]))


@test("Config cut in half: no crash")
def _():
    txt, s = template_text()
    return expect_no_crash(spawn([tmp_path("cut.conf", txt[:len(txt) // 2].encode())]))


@test("Config with a missing closing brace: no crash")
def _():
    txt, s = template_text()
    i = txt.rfind("}")
    return expect_no_crash(spawn([tmp_path("brace.conf", (txt[:i] + txt[i + 1:]).encode())]))


@test("Config with an unknown directive and a 1 MB line: no crash")
def _():
    txt, s = template_text()
    return expect_no_crash(spawn([tmp_path("unk.conf", (txt + "\nunknown_directive 1;\n# " + "x" * 1000000 + "\n").encode())]))


@test("Config file without read permission: clean error, no crash")
def _():
    if is_root():
        raise Skip("running as root")
    conf, s = conf_file()
    os.chmod(conf, 0)
    return expect_no_crash(spawn([conf]))


@test("No argument (default config path): no crash")
def _():
    launch_ready()
    return expect_no_crash(spawn([]))


@test("Too many arguments: no crash")
def _():
    conf, s = conf_file()
    return expect_no_crash(spawn([conf, conf, "extra"]))


# --------------------------------------------------------------------------
section(M, "Basic GET and static website")
# --------------------------------------------------------------------------
@test("GET / returns 200 with a non-empty body")
def _():
    r = get(idx())
    ok = r.status == 200 and len(r.body) > 0
    got = "%s, body %d bytes" % (r.summary(), len(r.body))
    if ok and CONFIG["index_contains"] and CONFIG["index_contains"].encode() not in r.body:
        return False, "body contains %r" % CONFIG["index_contains"], got
    return ok, "200 OK with a non-empty body", got


@test("Static files are served with the right Content-Type")
def _():
    for path, ctype in need("static_files"):
        r = get(path)
        if r.status != 200 or not (r.h("content-type") or "").lower().startswith(ctype):
            return False, "200 and Content-Type %s for %s" % (ctype, path), \
                "%s, Content-Type: %s" % (r.summary(), r.h("content-type"))
    return True, "", ""


@test("Download files are byte-identical to the files on disk")
def _():
    url, want = big_resource() if (CONFIG["download_files"] or CONFIG.get("www_root")) else (None, None)
    if url is None:
        raise Skip("config 'download_files' / 'www_root' not set")
    r = get(url, timeout=20)
    return r.status == 200 and r.body == want, "200 and %d identical bytes" % len(want), \
        "%s, %d bytes%s" % (r.summary(), len(r.body), "" if r.body == want else " (content differs)")


@test("File with a space in its name (%20)")
def _():
    www_file("hello world.txt")
    r = get("/hello%20world.txt")
    return r.status == 200 and b"hello world" in r.body, "200 with the file content", \
        "%s, body %r" % (r.summary(), r.body[:40])


@test("Deep path to a file (/sub/dir/deep.html)")
def _():
    www_file("sub/dir/deep.html")
    r = get("/sub/dir/deep.html")
    return r.status == 200 and b"deep" in r.body, "200 with the file content", r.summary()


@test("Same page requested 50 times gives the same answer")
def _():
    base = get(idx())
    for i in range(50):
        r = get(idx())
        if r.status != base.status or r.body != base.body:
            return False, "identical answer each time", "request %d: %s" % (i, r.summary())
    return True, "", ""


# --------------------------------------------------------------------------
section(M, "Response format and headers")
# --------------------------------------------------------------------------
@test("Response is well-formed HTTP (status line 'HTTP/1.x NNN Reason', 'Name: value' lines)")
def _():
    r = get(idx())
    return r.well_formed and r.version in ("1.0", "1.1"), "valid status line and headers", "%r" % r.head[:80]


@test("Headers and line endings use CRLF (no bare LF)")
def _():
    r = get(idx())
    return not re.search(rb"(?<!\r)\n", r.head), "every line ends with CRLF", "%r" % r.head[:100]


@test("Status line has a reason phrase")
def _():
    r = get(idx())
    return bool(r.reason.strip()), "e.g. 'HTTP/1.1 200 OK'", "reason phrase: %r" % r.reason


@test("Body length is announced (Content-Length equal to real size, chunked, or close)")
def _():
    r = get(idx())
    cl, te = r.h("content-length"), r.h("transfer-encoding") or ""
    if cl is not None:
        ok, got = cl.isdigit() and int(cl) == len(r.body), "Content-Length: %s but body is %d bytes" % (cl, len(r.body))
    elif "chunked" in te.lower():
        ok, got = True, "chunked"
    else:
        ok, got = r.closed and not r.timed_out, "no Content-Length/chunked, connection %s" % ("closed" if r.closed else "left open")
    return ok, "Content-Length equals the body size", got


@test("No duplicated Content-Length header in the response")
def _():
    r = get(idx())
    n = len(r.all_h("content-length"))
    return n <= 1, "at most one Content-Length", "%d Content-Length headers" % n


@test("Content-Type header is present on a 200 answer")
def _():
    r = get(idx())
    return bool(r.h("content-type")), "Content-Type: text/html (or similar)", "Content-Type: %s" % r.h("content-type")


@test("Error response also carries Content-Length/Content-Type")
def _():
    r = get(CONFIG["missing_path"])
    ok = r.status == 404 and r.h("content-length") is not None and r.h("content-type")
    return bool(ok), "404 with Content-Length and Content-Type", \
        "%s, Content-Length: %s, Content-Type: %s" % (r.summary(), r.h("content-length"), r.h("content-type"))


@test("HEAD returns headers without a body (or is refused with 405/501)")
def _():
    r = send_recv(build("HEAD", idx()), no_body=True)
    if r.status in (405, 501):
        return True, "", ""
    return r.status == 200 and r.extra == b"", "200 headers only, or 405/501", \
        "%s, %d body bytes after the headers" % (r.summary(), len(r.extra))


# --------------------------------------------------------------------------
section(M, "URL validation")
# --------------------------------------------------------------------------
@test("Percent-encoded path is decoded (/%69ndex.html -> /index.html)")
def _():
    path = need("static_files")[0][0]
    return status_in(get("/%%%02x%s" % (ord(path[1]), path[2:])), {200})


url_case("Empty query string (/?) is fine", lambda: idx() + "?", {200})
url_case("Query string on a static file is ignored", lambda: need("static_files")[0][0] + "?a=1&b=2", {200})
url_case("Query with special characters (?q=%20%26=#)", lambda: need("static_files")[0][0] + "?q=%20%26=", {200})
url_case("Fragment sent by mistake (/index.html#top)", lambda: need("static_files")[0][0] + "#top", {200, 400, 404})
url_case("Dot segments (/./index.html)", lambda: "/." + need("static_files")[0][0], {200, 400, 404})
url_case("Parent segment inside the root (/x/../index.html)", lambda: "/x/.." + need("static_files")[0][0], {200, 400, 403, 404})
url_case("Double slashes (//index.html)", lambda: "/" + need("static_files")[0][0], {200, 301, 308, 400, 404})
url_case("Trailing slash on a file (/index.html/)", lambda: need("static_files")[0][0] + "/", {400, 404})
url_case("Missing nested path (/a/b/c/d.html)", "/a/b/c/d.html", {404})
url_case("Non-ASCII percent-encoded name (/%C3%A9t%C3%A9)", "/%C3%A9t%C3%A9", {404})
url_case("Raw non-ASCII bytes in the URL", "/\xc3\xa9t\xc3\xa9", {400, 404})
url_case("Invalid percent-encoding (/%zz)", "/%zz", {400, 404})
url_case("Truncated percent-encoding (/%2)", "/%2", {400, 404})
url_case("Lone percent sign (/%)", "/%", {400, 404})
url_case("Encoded NUL (/index.html%00.txt)", "/index.html%00.txt", {400, 404})
url_case("Absolute-form target (GET http://host/ HTTP/1.1)",
         lambda: "http://%s:%d%s" % (CONFIG["host"], CONFIG["port"], idx()), {200, 400, 404})
url_case("Asterisk target (GET * HTTP/1.1)", "*", {400, 404, 405, 501})
url_case("Tab character inside the URL", "/index\t.html", {400, 404})
url_case("Traversal: /../../../../etc/passwd", "/../../../../../../etc/passwd", {400, 403, 404}, leak=True)
url_case("Traversal encoded: /%2e%2e/%2e%2e/etc/passwd", "/%2e%2e/%2e%2e/%2e%2e/%2e%2e/etc/passwd", {400, 403, 404}, leak=True)
url_case("Traversal with encoded slash: ..%2f..%2fetc/passwd", "/..%2f..%2f..%2f..%2fetc/passwd", {400, 403, 404}, leak=True)
url_case("Traversal with backslashes: ..\\..\\etc\\passwd", "/..\\..\\..\\..\\etc\\passwd", {400, 403, 404}, leak=True)
url_case("Traversal double-encoded: %252e%252e", "/%252e%252e/%252e%252e/%252e%252e/etc/passwd", {400, 403, 404}, leak=True)
url_case("Traversal below an existing folder (/sub/../../..)", "/sub/../../../../../../etc/passwd", {400, 403, 404}, leak=True)
url_case("URL of 2000 characters", "/" + "a" * 2000, {400, 404, 414})
url_case("Very long URI (10000 chars) is refused", "/" + "a" * 10000, {400, 414})
url_case("Huge URI (200000 chars) is refused", "/" + "a" * 200000, {400, 414})
url_case("1000 path segments (/a/a/a/...)", "/a" * 1000, {400, 404, 414})
url_case("8000-character query string", lambda: idx() + "?" + "q" * 8000, {200, 400, 414})


# --------------------------------------------------------------------------
section(M, "HTTP methods")
# --------------------------------------------------------------------------
@test("GET works")
def _():
    return status_in(get(idx()), {200})


@test("POST is accepted on a POST route")
def _():
    return status_in(send_recv(build("POST", post_target(), headers=[("Content-Type", "text/plain")],
                                     body=b"hello webserv")), OK_POST)


@test("DELETE of a missing file returns 404")
def _():
    return status_in(send_recv(build("DELETE", need("delete_prefix") + "nope_%s.txt" % uuid.uuid4().hex[:6])), {404})


@test("POST and DELETE on a GET-only route return 405")
def _():
    path = need("get_only_path")
    r1 = send_recv(build("POST", path, headers=[("Content-Type", "text/plain")], body=b"x"))
    r2 = send_recv(build("DELETE", path))
    return r1.status == 405 and r2.status == 405, "POST -> 405 and DELETE -> 405", \
        "POST -> %s, DELETE -> %s" % (r1.summary(), r2.summary())


raw_case("Unknown method FOOBAR is refused", build("FOOBAR", "/"), {400, 405, 501})
raw_case("Lowercase method (get) is refused", build("get", "/"), {400, 405, 501})
raw_case("Method name of 10000 characters", lambda: build("A" * 10000, "/"), {400, 405, 414, 501})
for _m in ("PUT", "PATCH", "OPTIONS", "TRACE", "CONNECT", "LINK"):
    raw_case("Method %s (not required) never crashes the server" % _m,
             build(_m, "/", headers=[("Content-Length", "0")]), ANY_NO_CRASH)


@test("GET with a body: body is consumed, next request on the connection still works")
def _():
    s = open_sock()
    try:
        s.sendall(build("GET", idx(), body=b"hello") + build("GET", idx()))
        r1 = read_response(s, 5)
        r2 = read_response(s, 5, buf=r1.extra)
    finally:
        s.close()
    ok = r1.status in (200, 400, 405) and (r1.status != 200 or r2.status == 200 or r2.closed)
    return ok, "200 then 200 (or 400 and close)", "first %s, second %s" % (r1.summary(), r2.summary())


raw_case("POST without Content-Length nor chunked: 411/400 (or accepted), never a hang",
         lambda: build("POST", post_target(), headers=[("Content-Type", "text/plain")]),
         OK_POST | {400, 411})
raw_case("POST with Content-Length: 0",
         lambda: build("POST", post_target(), headers=[("Content-Length", "0")]), OK_POST | {400})
raw_case("DELETE with path traversal is refused",
         lambda: build("DELETE", need("delete_prefix") + "../../etc/passwd"), {400, 403, 404, 405})


# --------------------------------------------------------------------------
section(M, "Request line and header validation")
# --------------------------------------------------------------------------
raw_case("Request line without version", b"GET /\r\n\r\n", {400, 505})
raw_case("Request line with 4 parts", b"GET / HTTP/1.1 extra\r\nHost: x\r\n\r\n", {400})
raw_case("Target not starting with '/'", b"GET index.html HTTP/1.1\r\nHost: x\r\n\r\n", {400})
raw_case("Double space between tokens", b"GET  /  HTTP/1.1\r\nHost: x\r\n\r\n", {400})
raw_case("Leading space before the method", b" GET / HTTP/1.1\r\nHost: x\r\n\r\n", {400})
raw_case("Tab as separator", b"GET\t/\tHTTP/1.1\r\nHost: x\r\n\r\n", {400})
raw_case("Empty method", b" / HTTP/1.1\r\nHost: x\r\n\r\n", {400})
raw_case("Unsupported version HTTP/2.0", build("GET", "/", version="HTTP/2.0"), {400, 505})
raw_case("Unsupported version HTTP/9.9", build("GET", "/", version="HTTP/9.9"), {400, 505})
raw_case("Lowercase protocol (http/1.1)", build("GET", "/", version="http/1.1"), {400, 505})
raw_case("Incomplete version (HTTP/1.)", build("GET", "/", version="HTTP/1."), {400, 505})
raw_case("Bare LF line endings (no CR)", b"GET / HTTP/1.1\nHost: x\n\n", {200, 400})
raw_case("Empty lines before the request line", b"\r\n\r\nGET / HTTP/1.1\r\nHost: x\r\n\r\n", {200, 400})
raw_case("HTTP/1.1 request without Host", lambda: build("GET", "/", host=False), {400})
raw_case("Two Host headers", b"GET / HTTP/1.1\r\nHost: a\r\nHost: b\r\n\r\n", {400})
raw_case("Empty Host header", b"GET / HTTP/1.1\r\nHost:\r\n\r\n", {200, 400})
raw_case("Header names are case-insensitive (hOsT)",
         lambda: ("GET %s HTTP/1.1\r\nhOsT: %s:%d\r\n\r\n" % (idx(), CONFIG["host"], CONFIG["port"])).encode(), {200})
raw_case("Space before the colon ('Host : x')", b"GET / HTTP/1.1\r\nHost : x\r\n\r\n", {400})
raw_case("Header line without a colon", b"GET / HTTP/1.1\r\nHost: x\r\nBrokenHeader\r\n\r\n", {400})
raw_case("Header name with a space", b"GET / HTTP/1.1\r\nHost: x\r\nBad Header: x\r\n\r\n", {400})
raw_case("Empty header name (': value')", b"GET / HTTP/1.1\r\nHost: x\r\n: value\r\n\r\n", {400})
raw_case("Obsolete line folding (continuation line)", b"GET / HTTP/1.1\r\nHost: x\r\nX-A: 1\r\n  continued\r\n\r\n", {200, 400})
raw_case("Header value with NUL byte", b"GET / HTTP/1.1\r\nHost: x\r\nX-A: a\x00b\r\n\r\n", {200, 400})
raw_case("Header value with non-ASCII bytes", b"GET / HTTP/1.1\r\nHost: x\r\nX-A: \xc3\xa9\xff\r\n\r\n", {200, 400})
raw_case("Header with empty value (X-Empty:)", lambda: build("GET", idx(), headers=[("X-Empty", "")]), {200})
raw_case("Header value with trailing spaces", lambda: build("GET", idx(), headers=[("X-A", "value   ")]), {200})
raw_case("Header name of 10000 characters", lambda: build("GET", "/", headers=[("X" * 10000, "v")]), {400, 431})
raw_case("Huge header value (100 KB)", lambda: build("GET", "/", headers=[("X-Big", "a" * 100000)]), {400, 413, 431})
raw_case("1 MB of headers", lambda: build("GET", "/", headers=[("X-%d" % i, "a" * 1000) for i in range(1000)]),
         {400, 413, 431, 0})
raw_case("2000 headers", lambda: build("GET", "/", headers=[("X-H%d" % i, "v") for i in range(2000)]),
         {200, 400, 404, 413, 431, 0})
cl_case("Content-Length negative (-5)", "Content-Length: -5\r\n", {400})
cl_case("Content-Length not a number (abc)", "Content-Length: abc\r\n", {400})
cl_case("Content-Length with trailing junk (5abc)", "Content-Length: 5abc\r\n", {400})
cl_case("Content-Length with two numbers (5 5)", "Content-Length: 5 5\r\n", {400})
cl_case("Content-Length overflow (99999999999999999999)", "Content-Length: 99999999999999999999\r\n", {400, 413})
cl_case("Two different Content-Length headers", "Content-Length: 5\r\nContent-Length: 6\r\n", {400})
cl_case("Content-Length list (5, 5)", "Content-Length: 5, 5\r\n", {400})
cl_case("Content-Length and Transfer-Encoding together", "Content-Length: 5\r\nTransfer-Encoding: chunked\r\n",
        OK_POST | {400, 0})
cl_case("Unknown Transfer-Encoding (gzip)", "Transfer-Encoding: gzip\r\n", {400, 501})


@test("Expect: 100-continue does not hang (100 Continue or wait, then final answer)")
def _():
    s = open_sock()
    try:
        s.sendall(build("POST", post_target(), headers=[("Content-Type", "text/plain"),
                                                          ("Content-Length", "5"), ("Expect", "100-continue")]))
        r1 = read_response(s, 1.5)
        if r1.status is not None and r1.status != 100:
            final = r1
        else:
            ssend(s, b"hello")
            final = read_response(s, 5, buf=r1.extra if r1.status == 100 else b"")
    finally:
        s.close()
    return final.status in OK_POST | {417}, "100 Continue (or silence) then 2xx", \
        "first: %s; final: %s" % (r1.summary(), final.summary())


# --------------------------------------------------------------------------
section(M, "Error pages and status codes")
# --------------------------------------------------------------------------
@test("Missing page returns 404")
def _():
    return status_in(get(CONFIG["missing_path"]), {404})


@test("Default error page: the 404 response has a non-empty HTML body")
def _():
    r = get(CONFIG["missing_path"])
    ok = r.status == 404 and len(r.body) > 0 and "text/html" in (r.h("content-type") or "")
    return ok, "404 with a non-empty text/html body", \
        "%s, %d bytes, Content-Type: %s" % (r.summary(), len(r.body), r.h("content-type"))


@test("Custom 404 page from the config is used")
def _():
    txt = need("custom_404_contains")
    r = get(CONFIG["missing_path"])
    return r.status == 404 and txt.encode() in r.body, "404 whose body contains %r" % txt, \
        "%s, body starts %r" % (r.summary(), r.body[:60])


@test("400 answer has a default error page body")
def _():
    r = send_recv(b"GET / HTTP/1.1 extra\r\nHost: x\r\n\r\n")
    return r.status == 400 and len(r.body) > 0, "400 with a non-empty body", \
        "%s, %d bytes" % (r.summary(), len(r.body))


@test("405 answer has a default error page body")
def _():
    r = send_recv(build("POST", need("get_only_path"), headers=[("Content-Type", "text/plain")], body=b"x"))
    return r.status == 405 and len(r.body) > 0, "405 with a non-empty body", \
        "%s, %d bytes" % (r.summary(), len(r.body))


@test("413 answer has a default error page body")
def _():
    size = need("max_body_size")
    r = send_recv(build("POST", CONFIG.get("max_body_path") or post_target(),
                        headers=[("Content-Type", "text/plain")], body=b"A" * (size + 1)))
    return r.status == 413 and len(r.body) > 0, "413 with a non-empty body", \
        "%s, %d bytes" % (r.summary(), len(r.body))


@test("File without read permission returns 403")
def _():
    root = need("www_root")
    if is_root():
        raise Skip("running as root: permissions are ignored")
    name = "tester_secret_%s.txt" % uuid.uuid4().hex[:6]
    p = os.path.join(root, name)
    with open(p, "w") as f:
        f.write("secret")
    os.chmod(p, 0)
    try:
        r = get("/" + name)
    finally:
        os.chmod(p, 0o644)
        os.remove(p)
    return status_in(r, {403})


@test("Missing directory with trailing slash returns 404")
def _():
    return status_in(get("/no_such_dir_%s/" % uuid.uuid4().hex[:6]), {404})


@test("Status codes are accurate for a series of requests")
def _():
    cases = [(build("GET", CONFIG["missing_path"]), {404}),
             (build("GET", "/", version="HTTP/3.0"), {400, 505}),
             (build("FOO", "/"), {400, 405, 501}),
             (b"garbage\r\n\r\n", {400})]
    bad = []
    for raw, allowed in cases:
        r = send_recv(raw)
        if r.status not in allowed:
            bad.append("%r -> %s" % (raw[:20], r.summary()))
    return not bad, "404 / 505 / 501 / 400 as appropriate", "; ".join(bad)


# --------------------------------------------------------------------------
section(M, "Directories, index, autoindex and redirects")
# --------------------------------------------------------------------------
@test("Default file is served for a directory (GET /)")
def _():
    r = get("/")
    return r.status == 200 and len(r.body) > 0, "200 with the index file", r.summary()


@test("Directory without trailing slash redirects or serves")
def _():
    p = need("autoindex_on_path").rstrip("/")
    return status_in(get(p), {200, 301, 302, 307, 308})


@test("Directory with autoindex ON shows a listing")
def _():
    r = get(need("autoindex_on_path"))
    return r.status == 200 and b"<a" in r.body.lower(), "200 with an HTML listing containing links", \
        "%s, body starts %r" % (r.summary(), r.body[:60])


@test("Autoindex links are usable (first link returns 200)")
def _():
    base = need("autoindex_on_path")
    r = get(base)
    m = re.search(rb'href="([^"#?]+)"', r.body)
    if not m:
        return False, "a link in the listing", "no href found"
    href = m.group(1).decode()
    target = href if href.startswith("/") else base.rstrip("/") + "/" + href
    r2 = get(target)
    return r2.status == 200, "GET %s -> 200" % target, r2.summary()


@test("Directory with autoindex OFF and no index file is refused")
def _():
    return status_in(get(need("autoindex_off_path")), {403, 404})


@test("Redirect route answers 3xx with the right Location")
def _():
    r = get(need("redirect_path"))
    loc = r.h("location") or ""
    ok = r.status in (301, 302, 303, 307, 308) and CONFIG["redirect_location"] in loc
    return ok, "3xx with Location containing %r" % CONFIG["redirect_location"], \
        "%s, Location: %r" % (r.summary(), loc)


@test("Following the redirect leads to a 200 page")
def _():
    r = get(need("redirect_path"))
    loc = r.h("location") or ""
    m = re.match(r"^(?:https?://[^/]+)?(/.*)$", loc)
    if not m:
        return False, "a usable Location", "Location: %r" % loc
    r2 = get(m.group(1))
    return r2.status == 200, "200 at the redirect target", "%s at %s" % (r2.summary(), m.group(1))


# --------------------------------------------------------------------------
section(M, "Request body: Content-Length, chunked, max body size")
# --------------------------------------------------------------------------
@test("Body within client_max_body_size is accepted")
def _():
    size = need("max_body_size")
    r = send_recv(build("POST", CONFIG.get("max_body_path") or post_target(),
                        headers=[("Content-Type", "text/plain")], body=b"A" * max(1, size // 2)))
    return r.status is not None and r.status != 413 and r.status < 500, "any answer except 413/5xx", r.summary()


@test("Body of exactly max_body_size bytes is not rejected")
def _():
    size = need("max_body_size")
    r = send_recv(build("POST", CONFIG.get("max_body_path") or post_target(),
                        headers=[("Content-Type", "text/plain")], body=b"A" * size))
    return r.status is not None and r.status != 413 and r.status < 500, "not 413 at exactly the limit", r.summary()


@test("Body bigger than client_max_body_size returns 413")
def _():
    size = need("max_body_size")
    s = open_sock()
    try:
        try:
            s.sendall(build("POST", CONFIG.get("max_body_path") or post_target(),
                            headers=[("Content-Type", "text/plain")], body=b"A" * (size + 1)))
        except OSError:
            pass
        r = read_response(s, 5)
    finally:
        s.close()
    return survived(*status_in(r, {413}))


@test("Declared body of 10 GB is refused quickly (headers only, no body sent)")
def _():
    t0 = time.time()
    r = send_recv(build("POST", CONFIG.get("max_body_path") or post_target(),
                        headers=[("Content-Type", "text/plain"), ("Content-Length", "10000000000")]), timeout=4)
    dt = time.time() - t0
    ok, e, g = status_in(r, {400, 413})
    return survived(ok, e + " within 4s", "%s after %.1fs" % (g, dt))


@test("Chunked POST body is accepted")
def _():
    raw = build("POST", post_target(), headers=[("Content-Type", "text/plain"), ("Transfer-Encoding", "chunked")],
                body=chunk_encode(b"chunked-data-123"))
    return status_in(send_recv(raw), OK_POST)


@test("Chunked body with uppercase hex size and chunk extension")
def _():
    raw = build("POST", post_target(), headers=[("Content-Type", "text/plain"), ("Transfer-Encoding", "chunked")],
                body=b"A;ext=1\r\n0123456789\r\n0\r\n\r\n")
    return status_in(send_recv(raw), OK_POST)


@test("'Transfer-Encoding: Chunked' (capital C) is understood")
def _():
    raw = build("POST", post_target(), headers=[("Content-Type", "text/plain"), ("Transfer-Encoding", "Chunked")],
                body=chunk_encode(b"abc"))
    return status_in(send_recv(raw), OK_POST)


@test("Empty chunked body (0 chunk only)")
def _():
    raw = build("POST", post_target(), headers=[("Content-Type", "text/plain"), ("Transfer-Encoding", "chunked")],
                body=b"0\r\n\r\n")
    return status_in(send_recv(raw), OK_POST | {400})


@test("Chunked body over client_max_body_size returns 413")
def _():
    size = need("max_body_size")
    raw = build("POST", CONFIG.get("max_body_path") or post_target(),
                headers=[("Content-Type", "text/plain"), ("Transfer-Encoding", "chunked")],
                body=chunk_encode(b"A" * (size + 1), 512))
    s = open_sock()
    try:
        try:
            s.sendall(raw)
        except OSError:
            pass
        r = read_response(s, 5)
    finally:
        s.close()
    return survived(*status_in(r, {413}))


raw_case("Chunked body with an invalid chunk size (ZZ)",
         lambda: build("POST", post_target(), headers=[("Content-Type", "text/plain"), ("Transfer-Encoding", "chunked")],
                       body=b"ZZ\r\nhello\r\n0\r\n\r\n"), {400, 0})
raw_case("Chunked body with a negative chunk size (-1)",
         lambda: build("POST", post_target(), headers=[("Content-Type", "text/plain"), ("Transfer-Encoding", "chunked")],
                       body=b"-1\r\nhello\r\n0\r\n\r\n"), {400, 0})
raw_case("Chunked body with a huge chunk size (FFFFFFFFFFFFFFFFF)",
         lambda: build("POST", post_target(), headers=[("Content-Type", "text/plain"), ("Transfer-Encoding", "chunked")],
                       body=b"FFFFFFFFFFFFFFFFF\r\nhello\r\n0\r\n\r\n"), {400, 413, 0})


@test("Chunk announces more data than sent, then the client disconnects")
def _():
    s = open_sock()
    s.sendall(build("POST", post_target(), headers=[("Transfer-Encoding", "chunked")], body=b"64\r\nshort"))
    s.close()
    time.sleep(0.3)
    return survived(True, "no crash", "ok")


# --------------------------------------------------------------------------
section(M, "Upload, POST and DELETE")
# --------------------------------------------------------------------------
@test("Raw POST uploads a named binary file and GET returns identical bytes")
def _():
    need("upload_path")
    need("upload_fetch_prefix")
    fname = "tester_raw_%s.bin" % uuid.uuid4().hex[:8]
    body = bytes(range(256)) * 4
    r1 = send_recv(build("POST", CONFIG["upload_path"].rstrip("/") + "/" + fname,
                         headers=[("Content-Type", "application/octet-stream")], body=body))
    if r1.status not in OK_POST:
        return status_in(r1, OK_POST)
    r2 = get(CONFIG["upload_fetch_prefix"] + fname)
    STATE["raw_upload"] = fname
    return r2.status == 200 and r2.body == body, "uploaded file can be fetched byte-identically", \
        "POST %s, GET %s, %d bytes" % (r1.summary(), r2.summary(), len(r2.body))


@test("Raw uploaded file can be deleted and is then 404")
def _():
    need("delete_prefix")
    need("upload_fetch_prefix")
    fname = STATE.get("raw_upload")
    if not fname:
        raise Skip("raw upload test did not succeed")
    r1 = send_recv(build("DELETE", CONFIG["delete_prefix"] + fname))
    r2 = get(CONFIG["upload_fetch_prefix"] + fname)
    return r1.status in (200, 202, 204) and r2.status == 404, \
        "DELETE returns 2xx and subsequent GET is 404", \
        "DELETE %s, GET %s" % (r1.summary(), r2.summary())


@test("Raw upload with path traversal never creates a file outside upload directory")
def _():
    dest = need("upload_dir")
    name = "tester_escape_%s.txt" % uuid.uuid4().hex[:8]
    above = os.path.abspath(os.path.join(dest, "..", name))
    r = send_recv(build("POST", CONFIG["upload_path"].rstrip("/") + "/../" + name,
                        body=b"SECRET"))
    leaked = os.path.exists(above)
    if leaked:
        os.remove(above)  # Only our unique file inside the isolated sample site.
    return not leaked, "upload does not escape the configured folder", \
        "POST %s; outside path existed: %s" % (r.summary(), leaked)


def multipart(fname, content, field="file", boundary=None, ctype="text/plain", close=True):
    bd = boundary or "----webservtester" + uuid.uuid4().hex[:8]
    body = ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
            'Content-Type: %s\r\n\r\n' % (bd, field, fname, ctype)).encode() + content
    body += ("\r\n--%s--\r\n" % bd).encode() if close else b""
    return bd, body


def upload(fname, content, **kw):
    bd, body = multipart(fname, content, **kw)
    return send_recv(build("POST", need("upload_path"), headers=[
        ("Content-Type", "multipart/form-data; boundary=" + bd)], body=body), timeout=15)


def fetch_upload(fname):
    return get(need("upload_fetch_prefix") + fname, timeout=15)


@test("Upload a file with multipart/form-data POST")
def _():
    fname = "tester_%s.txt" % uuid.uuid4().hex[:8]
    content = b"webserv tester upload content\n"
    r = upload(fname, content)
    STATE["upload"] = (fname, content) if r.status in OK_POST else None
    return status_in(r, OK_POST)


@test("Uploaded file can be downloaded again with the same content")
def _():
    need("upload_fetch_prefix")
    if not STATE.get("upload"):
        raise Skip("upload test did not succeed")
    fname, content = STATE["upload"]
    r = fetch_upload(fname)
    return r.status == 200 and r.body == content, "200 with body %r" % content, \
        "%s, body %r" % (r.summary(), r.body[:60])


@test("Upload of a binary file (all 256 byte values) is byte-identical")
def _():
    need("upload_fetch_prefix")
    fname = "tester_bin_%s.bin" % uuid.uuid4().hex[:6]
    data = bytes(range(256)) * 8
    r = upload(fname, data, ctype="application/octet-stream")
    if r.status not in OK_POST:
        return status_in(r, OK_POST)
    r2 = fetch_upload(fname)
    STATE["bin"] = fname
    return r2.status == 200 and r2.body == data, "download identical (%d bytes)" % len(data), \
        "%s, %d bytes%s" % (r2.summary(), len(r2.body), "" if r2.body == data else " (content differs)")


@test("Upload of a large file is byte-identical")
def _():
    need("upload_fetch_prefix")
    fname = "tester_big_%s.bin" % uuid.uuid4().hex[:6]
    data = os.urandom(CONFIG["upload_big_size"])
    r = upload(fname, data, ctype="application/octet-stream")
    if r.status not in OK_POST:
        return status_in(r, OK_POST)
    r2 = fetch_upload(fname)
    STATE["big"] = fname
    return r2.status == 200 and r2.body == data, "download identical (%d bytes)" % len(data), \
        "%s, %d bytes%s" % (r2.summary(), len(r2.body), "" if r2.body == data else " (content differs)")


@test("Upload of an empty file does not hang or crash")
def _():
    r = upload("tester_empty_%s.txt" % uuid.uuid4().hex[:6], b"")
    return survived(*status_in(r, OK_POST | {400}))


@test("Same filename upload is handled (success or 409 Conflict)")
def _():
    fname = "tester_dup_%s.txt" % uuid.uuid4().hex[:6]
    r1, r2 = upload(fname, b"one"), upload(fname, b"two")
    return r1.status in OK_POST and r2.status in (OK_POST | {409}), "first succeeds; second succeeds or returns 409 Conflict", \
        "%s then %s" % (r1.summary(), r2.summary())


@test("Upload with a wrong boundary is refused or ignored (no crash)")
def _():
    bd, body = multipart("x.txt", b"data", boundary="realboundary")
    r = send_recv(build("POST", need("upload_path"), headers=[
        ("Content-Type", "multipart/form-data; boundary=otherboundary")], body=body))
    return survived(*status_in(r, OK_POST | {400, 415}))


@test("Truncated multipart body (no closing boundary): no crash")
def _():
    r = upload("tester_trunc.txt", b"abc", close=False)
    return survived(*status_in(r, OK_POST | {400}))


@test("Filename with path traversal does not escape the upload folder")
def _():
    updir = need("upload_dir")
    marker = "tester_evil_%s.txt" % uuid.uuid4().hex[:6]
    upload("../" + marker, b"evil")
    upload("../../" + marker, b"evil")
    leaked = [p for p in (os.path.join(updir, "..", marker), os.path.join(updir, "..", "..", marker))
              if os.path.exists(p)]
    for p in leaked:
        os.remove(p)
    return not leaked, "no file created outside %s" % updir, "created %s" % leaked


@test("20 uploads at the same time are all stored")
def _():
    need("upload_fetch_prefix")

    def one(i):
        fname = "tester_par_%s_%d.txt" % (uuid.uuid4().hex[:4], i)
        data = ("parallel-%d" % i).encode() * 50
        r = upload(fname, data)
        if r.status not in OK_POST:
            return "upload %d: %s" % (i, r.summary())
        r2 = fetch_upload(fname)
        return None if r2.status == 200 and r2.body == data else "download %d: %s" % (i, r2.summary())
    bad = [x for x in parallel(one, 20) if x]
    return survived(not bad, "20 uploads stored and identical", "; ".join(str(b) for b in bad[:3]))


@test("Upload on a GET-only route is refused with 405")
def _():
    bd, body = multipart("x.txt", b"data")
    r = send_recv(build("POST", need("get_only_path"), headers=[
        ("Content-Type", "multipart/form-data; boundary=" + bd)], body=body))
    return status_in(r, {405})


@test("DELETE removes the uploaded file (then GET -> 404)")
def _():
    prefix = need("delete_prefix")
    if not STATE.get("upload"):
        raise Skip("upload test did not succeed")
    fname, _c = STATE["upload"]
    r = send_recv(build("DELETE", prefix + fname))
    if r.status not in (200, 202, 204):
        return False, "DELETE -> 200/202/204", "DELETE -> " + r.summary()
    r2 = get((CONFIG["upload_fetch_prefix"] or prefix) + fname)
    return r2.status == 404, "GET after DELETE -> 404", "GET after DELETE -> " + r2.summary()


@test("DELETE of the same file a second time returns 404")
def _():
    prefix = need("delete_prefix")
    if not STATE.get("upload"):
        raise Skip("upload test did not succeed")
    return status_in(send_recv(build("DELETE", prefix + STATE["upload"][0])), {404})


@test("DELETE of the upload folder itself does not succeed")
def _():
    r = send_recv(build("DELETE", need("delete_prefix")))
    ok, e, g = status_in(r, {400, 403, 404, 405, 409})
    return survived(ok, e, g)


# --------------------------------------------------------------------------
section(M, "CGI basics")
# --------------------------------------------------------------------------
def cgi_get(name, query="", headers=None, timeout=8):
    return get(cgi(name) + query, headers=headers, timeout=timeout)


def cgi_post(name, body, headers=None, timeout=8):
    hs = [("Content-Type", "text/plain")] + list(headers or [])
    return send_recv(build("POST", cgi(name), headers=hs, body=body), timeout=timeout)


@test("GET runs a script (output ends at EOF, no Content-Length)")
def _():
    r = cgi_get("hello.py")
    return r.status == 200 and b"Hello from CGI (python)" in r.body, "200 with 'Hello from CGI (python)'", \
        "%s, body %r" % (r.summary(), r.body[:60])


@test("Content-Type given by the script is forwarded")
def _():
    ct = (cgi_get("hello.py").h("content-type") or "").lower()
    return ct.startswith("text/plain"), "Content-Type: text/plain", "Content-Type: %s" % ct


@test("Script headers are not leaked into the body")
def _():
    r = cgi_get("hello.py")
    return b"Content-Type" not in r.body, "body without the CGI header lines", "body %r" % r.body[:60]


@test("Query string is passed (QUERY_STRING)")
def _():
    r = cgi_get("echo.py", "?a=1&b=2")
    return r.status == 200 and b"QUERY_STRING=a=1&b=2" in r.body, "body contains QUERY_STRING=a=1&b=2", \
        "%s, body %r" % (r.summary(), r.body[:120])


@test("POST body is passed on stdin")
def _():
    r = cgi_post("echo.py", b"hello cgi")
    return r.status == 200 and b"BODY=hello cgi" in r.body, "body contains BODY=hello cgi", \
        "%s, body %r" % (r.summary(), r.body[:120])


@test("Environment: REQUEST_METHOD, CONTENT_LENGTH, CONTENT_TYPE")
def _():
    r = cgi_post("echo.py", b"12345")
    want = [b"REQUEST_METHOD=POST", b"CONTENT_LENGTH=5", b"CONTENT_TYPE=text/plain"]
    missing = [w.decode() for w in want if w not in r.body]
    return not missing, "all of " + ", ".join(w.decode() for w in want), "missing %s in %r" % (missing, r.body[:160])


@test("Script runs in its own directory (relative file access)")
def _():
    r = cgi_get("relative.py")
    return r.status == 200 and b"RELATIVE_OK" in r.body, "200 with RELATIVE_OK", \
        "%s, body %r" % (r.summary(), r.body[:60])


@test("Missing script returns 404")
def _():
    return status_in(cgi_get("nope_%s.py" % uuid.uuid4().hex[:6]), {404})


@test("Script that crashes returns 500/502, server survives")
def _():
    return survived(*status_in(cgi_get("crash.py"), {500, 502}))


@test("Script with a wrong interpreter (bad shebang) returns 500/502, server survives")
def _():
    return survived(*status_in(cgi_get("bad_shebang.py"), {500, 502}))


@test("Requesting the CGI folder itself does not crash")
def _():
    return survived(*status_in(get(need("cgi_base").rstrip("/") + "/"), ANY_NO_CRASH))


# --------------------------------------------------------------------------
section(M, "CGI advanced")
# --------------------------------------------------------------------------
@test("Chunked request is un-chunked before reaching the script (EOF = end of body)")
def _():
    raw = build("POST", cgi("echo.py"), headers=[("Content-Type", "text/plain"), ("Transfer-Encoding", "chunked")],
                body=chunk_encode(b"chunked-data-123"))
    r = send_recv(raw, timeout=8)
    return r.status == 200 and b"BODY=chunked-data-123" in r.body, "body contains BODY=chunked-data-123", \
        "%s, body %r" % (r.summary(), r.body[:120])


@test("Environment: GATEWAY_INTERFACE, SERVER_PROTOCOL, SERVER_PORT, SCRIPT_NAME")
def _():
    r = cgi_get("echo.py")
    want = [b"GATEWAY_INTERFACE=CGI/1.1", b"SERVER_PROTOCOL=HTTP/1.1",
            b"SERVER_PORT=%d" % CONFIG["port"], b"echo.py"]
    missing = [w.decode() for w in want if w not in r.body]
    return not missing, "all of " + ", ".join(w.decode() for w in want), "missing %s in %r" % (missing, r.body[:300])


@test("Request headers are passed to the script (HTTP_X_CUSTOM, HTTP_HOST)")
def _():
    r = cgi_get("echo.py", headers=[("X-Custom", "abc123")])
    missing = [w for w in (b"HTTP_X_CUSTOM=abc123", b"HTTP_HOST=") if w not in r.body]
    return not missing, "HTTP_X_CUSTOM=abc123 and HTTP_HOST=...", "missing %s" % missing


@test("PATH_INFO: /cgi-bin/echo.py/extra/path")
def _():
    r = get(cgi("echo.py") + "/extra/path?x=1", timeout=8)
    return r.status == 200 and b"PATH_INFO=/extra/path" in r.body, "200 with PATH_INFO=/extra/path", \
        "%s, body %r" % (r.summary(), r.body[:200])


@test("Percent-encoded query string reaches the script untouched")
def _():
    r = cgi_get("echo.py", "?name=a%20b%26c")
    return b"QUERY_STRING=name=a%20b%26c" in r.body, "QUERY_STRING=name=a%20b%26c", "body %r" % r.body[:160]


@test("POST with an empty body works")
def _():
    r = cgi_post("echo.py", b"", headers=[("Content-Length", "0")])
    return status_in(r, {200})


@test("Large script output (1 MB, EOF-terminated) arrives complete")
def _():
    r = cgi_get("big.py", timeout=20)
    return r.status == 200 and len(r.body) == 1000000, "200 with exactly 1000000 bytes", \
        "%s, %d bytes" % (r.summary(), len(r.body))


@test("Large POST body round trip (no pipe deadlock)")
def _():
    n = CONFIG["cgi_big_body"]
    r = cgi_post("echo.py", b"B" * n, timeout=25)
    ok = r.status == 200 and ("LEN=%d" % n).encode() in r.body
    return ok, "200 and LEN=%d echoed back within 25s" % n, "%s, %d bytes received" % (r.summary(), len(r.body))


@test("10 slow scripts at once finish together (CGI does not serialise the server)")
def _():
    t0 = time.time()
    res = parallel(lambda i: get(cgi("sleep.py") + "?t=1", timeout=15), 10)
    dt = time.time() - t0
    good = sum(1 for r in res if not isinstance(r, Exception) and r.status == 200 and b"SLEPT" in r.body)
    return good == 10 and dt < 4.5, "10 answers in under 4.5s (parallel)", "%d/10 ok in %.1fs" % (good, dt)


@test("Client disconnects while the script is running: no crash")
def _():
    s = open_sock()
    s.sendall(build("GET", cgi("sleep.py") + "?t=2"))
    time.sleep(0.3)
    s.close()
    time.sleep(2.2)
    return survived(True, "no crash", "ok")


@test("60 scripts in a row (leaked pipes/zombies would show up)")
def _():
    for i in range(60):
        r = cgi_get("hello.py")
        if r.status != 200:
            return False, "200 every time", "request %d: %s" % (i, r.summary())
    return survived(True, "60 x 200", "ok")


@test("Infinite-loop script does not block other clients", mem="defer")
def _():
    s = open_sock()
    try:
        s.sendall(build("GET", cgi("loop.py")))
        time.sleep(0.5)
        t0 = time.time()
        r = get(idx(), timeout=3)
        dt = time.time() - t0
    finally:
        s.close()
    return r.status == 200 and dt < 2.5, "GET / answered 200 quickly while the CGI loops", \
        "%s after %.1fs" % (r.summary(), dt)


@test("Infinite-loop script is cut off (the request never hangs forever)", slow=True)
def _():
    limit = CONFIG["cgi_timeout_wait"]
    need("cgi_base")
    s = open_sock()
    try:
        s.sendall(build("GET", cgi("loop.py")))
        r = read_response(s, limit)
    finally:
        s.close()
    return survived(not r.timed_out, "error page (500/504) or close within %ds" % limit,
                    "client still waiting after %ds" % limit if r.timed_out else r.summary())


# --------------------------------------------------------------------------
section(M, "Data arriving in pieces (accumulating requests)")
# --------------------------------------------------------------------------
def piecewise(parts, delay=0.12, timeout=5):
    s = open_sock()
    try:
        for part in parts:
            if not ssend(s, part):
                break
            time.sleep(delay)
        return read_response(s, timeout)
    finally:
        s.close()


@test("Request line split across several packets")
def _():
    return status_in(piecewise([b"GE", b"T /", b" HTTP/", b"1.1\r\nHo", b"st: x\r\n", b"\r\n"]), {200})


@test("CR and LF of the final blank line arrive separately")
def _():
    return status_in(piecewise([b"GET / HTTP/1.1\r\nHost: x\r\n\r", b"\n"]), {200})


@test("Headers split in the middle of a header name and of a value")
def _():
    return status_in(piecewise([b"GET / HTTP/1.1\r\nHo", b"st: loca", b"lhost\r\nX-A: va", b"lue\r\n\r\n"]), {200})


@test("Request sent one byte at a time")
def _():
    raw = build("GET", idx())
    s = open_sock()
    try:
        for i in range(len(raw)):
            if not ssend(s, raw[i:i + 1]):
                break
            time.sleep(0.004)
        r = read_response(s, 5)
    finally:
        s.close()
    return status_in(r, {200})


@test("Incomplete POST body: server waits, answers once the body is complete")
def _():
    s = open_sock()
    try:
        s.sendall(build("POST", post_target(), headers=[("Content-Type", "text/plain"), ("Content-Length", "10")],
                        body=b"Hello"))
        s.settimeout(1)
        try:
            early = s.recv(4096)
        except socket.timeout:
            early = None
        except OSError:
            early = b""
        if early is not None:
            return False, "no answer while 5 of 10 body bytes are missing", "answered early: %r" % early[:40]
        ssend(s, b"World")
        r = read_response(s, 5)
    finally:
        s.close()
    return status_in(r, OK_POST)


@test("POST body delivered in 5 small pieces")
def _():
    body = b"0123456789"
    head = build("POST", post_target(), headers=[("Content-Type", "text/plain"), ("Content-Length", "10")])
    return status_in(piecewise([head, body[:2], body[2:5], body[5:9], body[9:]], 0.1), OK_POST)


@test("Chunked body delivered byte by byte")
def _():
    head = build("POST", post_target(), headers=[("Content-Type", "text/plain"), ("Transfer-Encoding", "chunked")])
    parts = [head] + [chunk_encode(b"chunkbyte", 3)[i:i + 1] for i in range(len(chunk_encode(b"chunkbyte", 3)))]
    return status_in(piecewise(parts, 0.01, 8), OK_POST)


@test("Two requests in one packet get two answers, in order")
def _():
    s = open_sock()
    try:
        s.sendall(build("GET", idx()) + build("GET", CONFIG["missing_path"]))
        r1 = read_response(s, 5)
        r2 = read_response(s, 5, buf=r1.extra)
    finally:
        s.close()
    return r1.status == 200 and r2.status == 404, "first 200, second 404", \
        "first %s, second %s" % (r1.summary(), r2.summary())


@test("Second pipelined request arrives split in two packets")
def _():
    s = open_sock()
    try:
        second = build("GET", CONFIG["missing_path"])
        ssend(s, build("GET", idx()) + second[:10])
        time.sleep(0.2)
        ssend(s, second[10:])
        r1 = read_response(s, 5)
        r2 = read_response(s, 5, buf=r1.extra)
    finally:
        s.close()
    return r1.status == 200 and r2.status == 404, "first 200, second 404", \
        "first %s, second %s" % (r1.summary(), r2.summary())


@test("POST with body followed by a GET in the same packet")
def _():
    s = open_sock()
    try:
        s.sendall(build("POST", post_target(), headers=[("Content-Type", "text/plain")], body=b"hello")
                  + build("GET", idx()))
        r1 = read_response(s, 5)
        r2 = read_response(s, 5, buf=r1.extra)
    finally:
        s.close()
    return r1.status in OK_POST and r2.status == 200, "2xx then 200", \
        "first %s, second %s" % (r1.summary(), r2.summary())


# --------------------------------------------------------------------------
section(M, "Connection handling")
# --------------------------------------------------------------------------
@test("Keep-alive: two requests on the same connection")
def _():
    s = open_sock()
    try:
        s.sendall(build("GET", idx()))
        r1 = read_response(s, 5)
        try:
            s.sendall(build("GET", idx()))
        except OSError:
            pass
        r2 = read_response(s, 5, buf=r1.extra)
    finally:
        s.close()
    return r1.status == 200 and r2.status == 200, "200 and 200 on one connection", \
        "first %s, second %s" % (r1.summary(), r2.summary())


@test("Keep-alive: 100 requests in a row on one connection")
def _():
    s = open_sock()
    try:
        left = b""
        for i in range(100):
            s.sendall(build("GET", idx()))
            r = read_response(s, 5, buf=left)
            left = r.extra
            if r.status != 200:
                return False, "200 for all 100 requests", "request %d: %s" % (i + 1, r.summary())
    finally:
        s.close()
    return True, "", ""


@test("An error answer (404) does not break the connection for the next request")
def _():
    s = open_sock()
    try:
        s.sendall(build("GET", CONFIG["missing_path"]))
        r1 = read_response(s, 5)
        try:
            s.sendall(build("GET", idx()))
        except OSError:
            pass
        r2 = read_response(s, 5, buf=r1.extra)
    finally:
        s.close()
    ok = r1.status == 404 and (r2.status == 200 or (r2.status is None and r2.closed))
    return ok, "404, then 200 (or a clean close)", "first %s, second %s" % (r1.summary(), r2.summary())


@test("'Connection: close' makes the server close the connection")
def _():
    s = open_sock()
    try:
        s.sendall(build("GET", idx(), headers=[("Connection", "close")]))
        r = read_response(s, 5)
        s.settimeout(2)
        try:
            closed = s.recv(10) == b""
        except socket.timeout:
            closed = False
        except OSError:
            closed = True
    finally:
        s.close()
    return r.status == 200 and closed, "200 then the server closes the socket", \
        "%s, connection %s" % (r.summary(), "closed" if closed else "still open")


@test("HTTP/1.0 request without keep-alive: server closes after the answer")
def _():
    s = open_sock()
    try:
        s.sendall(build("GET", idx(), version="HTTP/1.0", host=False))
        r = read_response(s, 5)
        s.settimeout(2)
        try:
            closed = s.recv(10) == b""
        except socket.timeout:
            closed = False
        except OSError:
            closed = True
    finally:
        s.close()
    return r.status == 200 and closed, "200 then the server closes the socket", \
        "%s, connection %s" % (r.summary(), "closed" if closed else "still open")


@test("Client half-close (shutdown write) after the request: answer is still sent")
def _():
    s = open_sock()
    try:
        s.sendall(build("GET", idx()))
        s.shutdown(socket.SHUT_WR)
        r = read_response(s, 5)
    finally:
        s.close()
    return status_in(r, {200})


@test("Client closes right after sending (never reads the answer): no crash")
def _():
    for _i in range(20):
        s = open_sock()
        s.sendall(build("GET", idx()))
        s.close()
    time.sleep(0.3)
    return survived(True, "no crash", "ok")


@test("Client resets the connection (RST) after sending: no crash")
def _():
    for _i in range(20):
        s = open_sock()
        s.sendall(build("GET", idx()))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.close()
    time.sleep(0.3)
    return survived(True, "no crash", "ok")


@test("Client disconnects in the middle of the headers / the body")
def _():
    s = open_sock()
    s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nX-Part")
    s.close()
    s = open_sock()
    s.sendall(build("POST", post_target(), headers=[("Content-Length", "1000"), ("Content-Type", "text/plain")], body=b"abc"))
    s.close()
    time.sleep(0.3)
    return survived(True, "no crash", "ok")


# --------------------------------------------------------------------------
section(M, "Checking sockets non-blocking")
# --------------------------------------------------------------------------
def slow_get(url, expected_len, rcvbuf=2048, chunk=8192, delay=0.002, timeout=90):
    s = open_sock(rcvbuf=rcvbuf, timeout=timeout)
    try:
        s.sendall(build("GET", url, headers=[("Connection", "close")]))
        buf = b""
        end = time.time() + timeout
        while time.time() < end:
            try:
                d = s.recv(chunk)
            except OSError:
                break  # Report incomplete output instead of an uncaught thread exception.
            if not d:
                break
            buf += d
            time.sleep(delay)
        return buf.partition(b"\r\n\r\n")[2]
    finally:
        s.close()


def fast_check(limit=2.0):
    t0 = time.time()
    r = get(idx(), timeout=limit + 2)
    return r, time.time() - t0


@test("One idle connection (nothing sent) does not block a new client")
def _():
    s = open_sock()
    try:
        r, dt = fast_check()
    finally:
        s.close()
    return r.status == 200 and dt < 2, "200 in < 2s", "%s after %.1fs" % (r.summary(), dt)


@test("200 idle connections do not block a new client")
def _():
    socks = []
    try:
        for _i in range(200):
            socks.append(open_sock())
        r, dt = fast_check()
    finally:
        for s in socks:
            s.close()
    return r.status == 200 and dt < 2, "200 in < 2s", "%s after %.1fs" % (r.summary(), dt)


@test("Slow header trickle (1 byte / 50 ms) does not block a fast client")
def _():
    stop_ev = threading.Event()
    slow = open_sock()

    def trickle():
        try:
            slow.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nX-Slow: ")
            while not stop_ev.is_set():
                slow.sendall(b"a")
                time.sleep(0.05)
        except OSError:
            pass
    th = threading.Thread(target=trickle, daemon=True)
    th.start()
    time.sleep(0.5)
    r, dt = fast_check()
    stop_ev.set()
    slow.close()
    return r.status == 200 and dt < 2, "fast client answered 200 in < 2s", "%s after %.1fs" % (r.summary(), dt)


@test("Slow body trickle (POST, 1 byte / 50 ms) does not block a fast client")
def _():
    stop_ev = threading.Event()
    slow = open_sock()

    def trickle():
        try:
            slow.sendall(build("POST", post_target(), headers=[("Content-Type", "text/plain"),
                                                                 ("Content-Length", "1000")]))
            while not stop_ev.is_set():
                slow.sendall(b"a")
                time.sleep(0.05)
        except OSError:
            pass
    th = threading.Thread(target=trickle, daemon=True)
    th.start()
    time.sleep(0.5)
    r, dt = fast_check()
    stop_ev.set()
    slow.close()
    return r.status == 200 and dt < 2, "fast client answered 200 in < 2s", "%s after %.1fs" % (r.summary(), dt)


@test("A client that never reads a big answer does not block others")
def _():
    url, data = big_resource()
    s = open_sock(rcvbuf=2048)
    try:
        s.sendall(build("GET", url))
        time.sleep(0.7)
        r, dt = fast_check()
    finally:
        s.close()
    return survived(r.status == 200 and dt < 2, "other client answered 200 in < 2s", "%s after %.1fs" % (r.summary(), dt))


@test("A very slow reader still receives the complete, correct file")
def _():
    url, data = big_resource()
    res = {}
    th = threading.Thread(target=lambda: res.setdefault("body", slow_get(url, len(data))), daemon=True)
    th.start()
    worst = 0.0
    for _i in range(10):
        r, dt = fast_check()
        worst = max(worst, dt)
        if r.status != 200:
            return False, "fast clients answered 200 meanwhile", r.summary()
        time.sleep(0.1)
    th.join(90)
    body = res.get("body", b"")
    ok = body == data and worst < 2
    return ok, "slow reader gets %d identical bytes, fast clients < 2s" % len(data), \
        "slow reader got %d bytes%s, slowest fast client %.1fs" % (len(body), "" if body == data else " (differs)", worst)


@test("20 slow readers at the same time, fast clients still served")
def _():
    url, data = big_resource()
    out = [None] * 20

    def w(i):
        out[i] = slow_get(url, len(data), chunk=16384)
    ths = [threading.Thread(target=w, args=(i,), daemon=True) for i in range(20)]
    for t in ths:
        t.start()
    time.sleep(0.5)
    worst = 0.0
    for _i in range(10):
        r, dt = fast_check()
        worst = max(worst, dt)
        if r.status != 200:
            return False, "fast clients answered 200 meanwhile", r.summary()
    for t in ths:
        t.join(120)
    good = sum(1 for b in out if b == data)
    return survived(good == 20 and worst < 2, "20/20 complete files, fast clients < 2s",
                    "%d/20 complete, slowest fast client %.1fs" % (good, worst))


@test("Slow upload (100 KB in small pieces) does not block a fast client")
def _():
    s = open_sock()
    done = {}

    def uploader():
        try:
            body = b"u" * 100000
            s.sendall(build("POST", post_target(), headers=[("Content-Type", "text/plain"),
                                                              ("Content-Length", str(len(body)))]))
            for i in range(0, len(body), 2000):
                if not ssend(s, body[i:i + 2000]):
                    break
                time.sleep(0.02)
            done["r"] = read_response(s, 10)
        except OSError:
            done["r"] = None
    th = threading.Thread(target=uploader, daemon=True)
    th.start()
    time.sleep(0.4)
    worst = 0.0
    for _i in range(5):
        r, dt = fast_check()
        worst = max(worst, dt)
        if r.status != 200:
            return False, "fast clients answered 200 meanwhile", r.summary()
        time.sleep(0.2)
    th.join(20)
    s.close()
    return worst < 2, "fast clients < 2s during the slow upload", "slowest fast client %.1fs" % worst


@test("A sleeping CGI script does not block static requests")
def _():
    s = open_sock()
    try:
        s.sendall(build("GET", cgi("sleep.py") + "?t=3"))
        time.sleep(0.4)
        worst = 0.0
        for _i in range(5):
            r, dt = fast_check()
            worst = max(worst, dt)
            if r.status != 200:
                return False, "200 while the script sleeps", r.summary()
        r2 = read_response(s, 8)
    finally:
        s.close()
    return worst < 1.5 and r2.status == 200, "static answers < 1.5s while the script sleeps, then the script answers", \
        "slowest static %.1fs, script: %s" % (worst, r2.summary())


@test("200 pipelined requests (small answers) are all returned, in order")
def _():
    s = open_sock()
    try:
        s.sendall(build("GET", idx()) * 200)
        left, good = b"", 0
        for _i in range(200):
            r = read_response(s, 10, buf=left)
            left = r.extra
            if r.status != 200:
                break
            good += 1
    finally:
        s.close()
    return good == 200, "200 answers of 200 OK", "%d answers received" % good


@test("100 clients connect and disconnect immediately while others are served")
def _():
    stop_ev = threading.Event()

    def churn():
        while not stop_ev.is_set():
            try:
                open_sock(timeout=1).close()
            except OSError:
                pass
    ths = [threading.Thread(target=churn, daemon=True) for _i in range(10)]
    for t in ths:
        t.start()
    bad = 0
    for _i in range(20):
        r, dt = fast_check(3)
        if r.status != 200 or dt > 3:
            bad += 1
    stop_ev.set()
    return survived(bad == 0, "all 20 requests answered 200 within 3s", "%d slow/failed" % bad)


# --------------------------------------------------------------------------
section(M, "Multiple ports and your own routes")
# --------------------------------------------------------------------------
@test("Server listens on every configured port")
def _():
    bad = []
    for p in [CONFIG["port"]] + list(need("extra_ports")):
        try:
            r = send_recv(build("GET", "/", port=p), port=p, timeout=3)
            if r.status is None or r.status >= 500:
                bad.append("%d: %s" % (p, r.summary()))
        except OSError as e:
            bad.append("%d: %s" % (p, e))
    return not bad, "every port answers (status < 500)", "; ".join(bad)


@test("Different ports deliver different content")
def _():
    need("extra_ports")
    exp = need("extra_port_expect")
    bad = []
    for p, txt in exp.items():
        r = send_recv(build("GET", "/", port=int(p)), port=int(p), timeout=3)
        if r.status != 200 or txt.encode() not in r.body:
            bad.append("port %s: %s, expected %r" % (p, r.summary(), txt))
    return not bad, "each port serves its own content", "; ".join(bad)


@test("Virtual hosts: Host header selects the site")
def _():
    bad = []
    for host, txt in need("vhosts"):
        r = send_recv(build("GET", "/", headers=[("Host", host)]))
        if r.status != 200 or txt.encode() not in r.body:
            bad.append("%s: %s, expected %r" % (host, r.summary(), txt))
    return not bad, "each Host gets its own content", "; ".join(bad)


@test("Your own routes (custom_tests in the config)")
def _():
    bad = []
    for method, path, status, text in need("custom_tests"):
        r = send_recv(build(method, path))
        if r.status != status or (text and text.encode() not in r.body):
            bad.append("%s %s -> %s (expected %s%s)" % (method, path, r.summary(), status,
                                                       ", containing %r" % text if text else ""))
    return not bad, "all custom routes behave as configured", "; ".join(bad)


# --------------------------------------------------------------------------
section(M, "Resilience and stress (intense)")
# --------------------------------------------------------------------------
GARBAGE = [
    ("random binary", lambda: bytes(random.Random(7).randrange(256) for _i in range(2048)) + b"\r\n\r\n"),
    ("only CRLFs", lambda: b"\r\n" * 500),
    ("TLS ClientHello (HTTPS to an HTTP port)",
     lambda: b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03" + bytes(random.Random(1).randrange(256) for _i in range(200))),
    ("a response instead of a request", lambda: b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"),
    ("the word GET repeated 5000 times", lambda: b"GET " * 5000 + b"\r\n\r\n"),
    ("NUL bytes only", lambda: b"\x00" * 1000 + b"\r\n\r\n"),
    ("SQL-looking request", lambda: b"GET /?id=1' OR '1'='1 HTTP/1.1\r\nHost: x\r\n\r\n"),
    ("shell-looking path", lambda: b"GET /;cat%20/etc/passwd HTTP/1.1\r\nHost: x\r\n\r\n"),
]
for _n, _g in GARBAGE:
    raw_case("Garbage input: %s" % _n, _g, {200, 400, 404, 414, 431, 0}, timeout=3)


@test("300 malformed requests in a row, then a normal request")
def _():
    rnd = random.Random(11)
    for _i in range(300):
        try:
            send_recv(bytes(rnd.randrange(256) for _j in range(rnd.randrange(1, 80))) + b"\r\n\r\n", timeout=0.3)
        except OSError:
            pass
    return survived(True, "server keeps answering", "ok")


@test("Fuzzing: 120 mutated copies of a valid request", slow=True)
def _():
    rnd = random.Random(1337)
    base = build("POST", post_target(), headers=[("Content-Type", "text/plain"), ("Cookie", "a=b")], body=b"fuzzbody")
    for i in range(120):
        b = bytearray(base)
        kind = rnd.randrange(4)
        if kind == 0:
            for _k in range(rnd.randrange(1, 6)):
                b[rnd.randrange(len(b))] = rnd.randrange(256)
        elif kind == 1:
            del b[rnd.randrange(len(b)):rnd.randrange(len(b)) + 1]
        elif kind == 2:
            p = rnd.randrange(len(b))
            b[p:p] = bytes(rnd.randrange(256) for _k in range(rnd.randrange(1, 40)))
        else:
            b = b[:rnd.randrange(1, len(b))]
        try:
            send_recv(bytes(b), timeout=0.25)
        except OSError:
            pass
        if i % 20 == 19 and not alive():
            return False, "server alive after every batch", "server dead after %d mutated requests" % (i + 1)
    return survived(True, "server keeps answering", "ok")


@test("50 clients sending 100 KB headers at once")
def _():
    res = parallel(lambda i: send_recv(build("GET", "/", headers=[("X-Big", "a" * 100000)]), timeout=8), 50)
    bad = [r for r in res if isinstance(r, Exception) or (r.status not in (400, 413, 431, None))]
    return survived(not bad, "400/431 (or close) for all", "%d unexpected answers" % len(bad))


@test("Slowloris: 150 connections with unfinished headers, normal client still served")
def _():
    socks = []
    try:
        for _i in range(150):
            s = open_sock()
            s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nX-Slow: ")
            socks.append(s)
        r, dt = fast_check(3)
    finally:
        for s in socks:
            s.close()
    return survived(r.status == 200 and dt < 3, "normal client answered 200 in < 3s", "%s after %.1fs" % (r.summary(), dt))


for _n in (100, 300):
    def _mk(n):
        def fn():
            n_ = n
            res = parallel(lambda i: get(idx(), timeout=20).status, n_)
            good = sum(1 for x in res if x == 200)
            return survived(good >= int(n_ * 0.95), ">= 95%% of %d simultaneous clients get 200" % n_,
                            "%d/%d got 200" % (good, n_))
        return fn
    add("%d simultaneous clients are all served" % _n, _mk(_n))


@test("50 connections x 40 keep-alive requests each")
def _():
    def one(i):
        s = open_sock(timeout=10)
        try:
            left, good = b"", 0
            for _k in range(40):
                s.sendall(build("GET", idx()))
                r = read_response(s, 10, buf=left)
                left = r.extra
                if r.status == 200:
                    good += 1
            return good
        finally:
            s.close()
    res = parallel(one, 50)
    total = sum(x for x in res if isinstance(x, int))
    return survived(total >= 2000 * 0.98, ">= 98%% of 2000 requests answered 200", "%d/2000" % total)


@test("Pipeline flood: 300 requests in a single send")
def _():
    s = open_sock()
    try:
        s.sendall(build("GET", idx()) * 300)
        left, good = b"", 0
        for _i in range(300):
            r = read_response(s, 10, buf=left)
            left = r.extra
            if r.status != 200:
                break
            good += 1
    finally:
        s.close()
    return survived(good == 300, "300 answers", "%d answers" % good)


@test("2000 quick connect/close cycles, then 100 simultaneous clients (fd leak check)")
def _():
    for i in range(2000):
        try:
            s = open_sock(timeout=2)
            if i % 2:
                s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            s.close()
        except OSError:
            pass
    time.sleep(0.5)
    res = parallel(lambda i: get(idx(), timeout=15).status, 100)
    good = sum(1 for x in res if x == 200)
    return survived(good >= 95, ">= 95/100 clients served after 2000 cycles", "%d/100 got 200" % good)


@test("Mixed traffic storm for 6 seconds (GET, 404, POST, bad requests, CGI, pipelines)")
def _():
    stop_at = time.time() + 6
    ops = [lambda: get(idx()), lambda: get(CONFIG["missing_path"]),
           lambda: send_recv(build("POST", post_target(), headers=[("Content-Type", "text/plain")], body=b"storm")),
           lambda: send_recv(b"BAD REQUEST\r\n\r\n", timeout=1),
           lambda: send_recv(build("GET", idx()) * 5, timeout=2)]
    if CONFIG.get("cgi_base"):
        ops.append(lambda: cgi_get("hello.py"))

    def worker(i):
        rnd = random.Random(i)
        n = 0
        while time.time() < stop_at:
            try:
                rnd.choice(ops)()
                n += 1
            except OSError:
                pass
        return n
    res = parallel(worker, 30)
    total = sum(x for x in res if isinstance(x, int))
    r = get(idx())
    return survived(r.status == 200 and total > 0, "server alive and answering after the storm",
                    "%d operations done, final GET: %s" % (total, r.summary()))


@test("Siege-like availability (>= 99.5%) for several seconds")
def _():
    stop_at = time.time() + CONFIG["siege_seconds"]

    def worker(i):
        ok = tot = 0
        while time.time() < stop_at:
            tot += 1
            try:
                if get(idx(), headers=[("Connection", "close")], timeout=5).status == 200:
                    ok += 1
            except OSError:
                pass
        return ok, tot
    res = [x for x in parallel(worker, 20) if isinstance(x, tuple)]
    ok, tot = sum(x[0] for x in res), sum(x[1] for x in res)
    rate = 100.0 * ok / tot if tot else 0
    return survived(rate >= 99.5, "availability >= 99.5%", "%.2f%% (%d/%d requests)" % (rate, ok, tot))


# --------------------------------------------------------------------------
section(M, "Timeouts: a request must never hang forever")
# --------------------------------------------------------------------------
@test("Unfinished headers are closed or answered in time", slow=True)
def _():
    limit = CONFIG["hang_timeout"]
    s = open_sock()
    try:
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n")
        r = read_response(s, limit)
    finally:
        s.close()
    return not r.timed_out, "server times out the request within %ds" % limit, \
        "connection still open and silent after %ds" % limit if r.timed_out else r.summary()


@test("Unfinished body (Content-Length 100, only 10 sent) is closed or answered in time", slow=True)
def _():
    limit = CONFIG["hang_timeout"]
    s = open_sock()
    try:
        s.sendall(build("POST", post_target(), headers=[("Content-Type", "text/plain"), ("Content-Length", "100")],
                        body=b"0123456789"))
        r = read_response(s, limit)
    finally:
        s.close()
    return not r.timed_out, "server times out the request within %ds" % limit, \
        "connection still open and silent after %ds" % limit if r.timed_out else r.summary()


@test("Unfinished chunked body is closed or answered in time", slow=True)
def _():
    limit = CONFIG["hang_timeout"]
    s = open_sock()
    try:
        s.sendall(build("POST", post_target(), headers=[("Content-Type", "text/plain"),
                                                          ("Transfer-Encoding", "chunked")], body=b"5\r\nhello\r\n"))
        r = read_response(s, limit)
    finally:
        s.close()
    return not r.timed_out, "server times out the request within %ds" % limit, \
        "connection still open and silent after %ds" % limit if r.timed_out else r.summary()


# --------------------------------------------------------------------------
section(M, "Memory leaks (repeat the same action and watch the server)")
# --------------------------------------------------------------------------
def hunt(name, action, per_batch=1000, slow=False, needs=()):
    """Repeat `action` in 3 batches; fail if descriptors/zombies stay up or memory rises every batch."""
    def fn():
        for k in needs:
            need(k)
        mon = need_mon()
        n = max(10, int(per_batch * CONFIG["mem_hunt_scale"]))
        warm = max(5, n // 5)
        cnt = {"bad": 0, "total": 0}

        def batch(k):
            for _i in range(k):
                cnt["total"] += 1
                try:
                    if action() is False:
                        cnt["bad"] += 1
                except OSError:
                    cnt["bad"] += 1
        batch(warm)
        time.sleep(0.3)
        s0 = mon.snap()
        if s0 is None:
            return False, "server running", "server process is gone"
        series, last = [s0["rss"]], s0
        for _b in range(3):
            batch(n)
            last = mon.settle(s0, CONFIG["mem_settle"]) or last
            series.append(last["rss"])
        problems = []
        if s0["fds"] >= 0 and last["fds"] > s0["fds"] + CONFIG["mem_fd_tolerance"]:
            problems.append("%d file descriptor(s) never closed (%d -> %d)" % (
                last["fds"] - s0["fds"], s0["fds"], last["fds"]))
        if last["zombies"] > s0["zombies"]:
            problems.append("%d zombie child process(es) not reaped" % (
                last["zombies"] - s0["zombies"]))
        growth = series[-1] - series[0]
        rising = all(b > a for a, b in zip(series, series[1:]))
        rss_warning = ""
        if growth > CONFIG["mem_rss_tolerance_kb"] and rising:
            rss_warning = "RSS rose every batch: +%d KB across %d actions (~%d B/action)" % (
                growth, 3 * n, growth * 1024 // (3 * n))
        info = "%d actions: fds %d -> %d | RSS KB %s" % (
            3 * n, s0["fds"], last["fds"], " -> ".join(str(x) for x in series))
        # RSS is retained physical memory, not an allocation-leak detector.
        # Valgrind's final shutdown report is used to judge actual heap leaks.
        verdict = "fail" if problems else ("warn" if rss_warning else "pass")
        details = problems + ([rss_warning] if rss_warning else [])
        STATE["mem_override"] = (verdict,
            "no unreleased fds or zombie processes; review repeated RSS growth separately",
            "; ".join(details) + "  [" + info + "]" if details else info)
        ok = cnt["bad"] <= max(2, 0.02 * cnt["total"])
        return ok, "the action succeeds every time", "%d of %d actions failed" % (cnt["bad"], cnt["total"])
    add(name, fn, slow)


CLOSE = [("Connection", "close")]


def a_get():
    return get(idx(), headers=CLOSE).status == 200


def a_404():
    return get(CONFIG["missing_path"], headers=CLOSE).status == 404


def a_post():
    return send_recv(build("POST", post_target(), headers=[("Content-Type", "text/plain")] + CLOSE,
                           body=b"x" * 100)).status in OK_POST


def a_chunked():
    raw = build("POST", post_target(), headers=[("Content-Type", "text/plain"), ("Transfer-Encoding", "chunked")] + CLOSE,
                body=chunk_encode(b"chunked-data-123"))
    return send_recv(raw).status in OK_POST


def a_connect_close():
    open_sock(timeout=2).close()
    return True


def a_garbage():
    send_recv(b"BAD REQUEST LINE\r\n\r\n", timeout=2)
    return True


def a_cut_headers():
    s = open_sock()
    ssend(s, b"GET / HTTP/1.1\r\nHost: x\r\nX-Part")
    s.close()
    return True


def a_cut_body():
    s = open_sock()
    ssend(s, build("POST", post_target(), headers=[("Content-Length", "1000"), ("Content-Type", "text/plain")], body=b"abc"))
    s.close()
    return True


def a_reset():
    s = open_sock()
    ssend(s, build("GET", idx()))
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    s.close()
    return True


def a_keepalive():
    s = open_sock()
    ok, left = True, b""
    try:
        for _i in range(20):
            ssend(s, build("GET", idx()))
            r = read_response(s, 5, buf=left)
            left = r.extra
            ok = ok and r.status == 200
            if r.status is None:
                break
    finally:
        s.close()
    return ok


def a_listing():
    return get(CONFIG["autoindex_on_path"], headers=CLOSE).status == 200


def a_cgi():
    return get(cgi("hello.py"), headers=CLOSE, timeout=8).status == 200


def a_cgi_post():
    return send_recv(build("POST", cgi("echo.py"), headers=[("Content-Type", "text/plain")] + CLOSE,
                           body=b"c" * 10000), timeout=8).status == 200


def a_cgi_crash():
    return get(cgi("crash.py"), headers=CLOSE, timeout=8).status in (500, 502)


def a_upload_delete():
    fname = "tester_leak_%s.txt" % uuid.uuid4().hex[:8]
    target = CONFIG["upload_path"].rstrip("/") + "/" + fname
    r = send_recv(build("POST", target, headers=[("Content-Type", "application/octet-stream")],
                        body=b"leak-test" * 50))
    if r.status not in OK_POST:
        return False
    return send_recv(build("DELETE", CONFIG["delete_prefix"] + fname)).status in (200, 202, 204)


def a_big():
    url, data = big_resource()
    r = get(url, timeout=30)
    return r.status == 200 and len(r.body) == len(data)


def a_big_abort():
    url, _d = big_resource()
    s = open_sock(rcvbuf=4096)
    try:
        ssend(s, build("GET", url))
        s.settimeout(3)
        s.recv(4096)
    except OSError:
        pass
    finally:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.close()
    return True


hunt("Leak hunt: GET / (1000 x 3 requests)", a_get, 1000)
hunt("Leak hunt: 404 error pages", a_404, 600)
hunt("Leak hunt: POST with a body", a_post, 500)
hunt("Leak hunt: chunked POST", a_chunked, 400)
hunt("Leak hunt: connect and close without sending anything", a_connect_close, 1000)
hunt("Leak hunt: malformed requests (400 answers)", a_garbage, 600)
hunt("Leak hunt: client leaves in the middle of the headers", a_cut_headers, 600)
hunt("Leak hunt: client leaves in the middle of the body", a_cut_body, 500)
hunt("Leak hunt: client resets the connection (RST)", a_reset, 500)
hunt("Leak hunt: keep-alive connections (20 requests each)", a_keepalive, 60)
hunt("Leak hunt: directory listing", a_listing, 300, needs=("autoindex_on_path",))
hunt("Leak hunt: CGI GET", a_cgi, 150, slow=True, needs=("cgi_base",))
hunt("Leak hunt: CGI POST (10 KB body)", a_cgi_post, 100, slow=True, needs=("cgi_base",))
hunt("Leak hunt: CGI that crashes (children must be reaped)", a_cgi_crash, 100, slow=True, needs=("cgi_base",))
hunt("Leak hunt: upload then DELETE", a_upload_delete, 80, slow=True, needs=("upload_path", "upload_fetch_prefix", "delete_prefix"))
hunt("Leak hunt: big file download (2 MB)", a_big, 8, slow=True)
hunt("Leak hunt: client leaves in the middle of a big download", a_big_abort, 40, slow=True)


@test("After all tests: every descriptor is released and no zombie child remains")
def _():
    mon = need_mon()
    base = STATE.get("mem_baseline")
    if not base:
        raise Skip("no baseline snapshot")
    end = time.time() + CONFIG["cgi_timeout_wait"]
    tol = CONFIG["mem_fd_tolerance"]
    while True:
        s = mon.snap()
        if s is None:
            return False, "server running", "server process is gone"
        ok_fd = base["fds"] < 0 or s["fds"] <= base["fds"] + tol
        ok_z = s["zombies"] <= base["zombies"]
        if (ok_fd and ok_z) or time.time() >= end:
            break
        time.sleep(0.5)
    exp = "descriptors back to %d and no zombie child (waits up to %ds for CGI timeouts)" % (base["fds"], CONFIG["cgi_timeout_wait"])
    got = "fds %d, zombies %d, children %d" % (s["fds"], s["zombies"], s["children"])
    STATE["mem_override"] = (ok_fd and ok_z, exp, got)
    return ok_fd and ok_z, exp, got


# --------------------------------------------------------------------------
section(M, "Final health check")
# --------------------------------------------------------------------------
@test("Server is still alive and answers 200 after all the tests")
def _():
    r = get(idx())
    return r.status == 200, "200 OK (the server never crashed)", r.summary()


# ==========================================================================
#                              BONUS TESTS
# ==========================================================================
B = BONUS


def session_of(r):
    m = re.search(rb"SESSION=(\w+)", r.body)
    v = re.search(rb"VISITS=(\d+)", r.body)
    return (m.group(1).decode() if m else None), (int(v.group(1)) if v else None)


def first_cookie(r):
    cookies = r.all_h("set-cookie")
    return cookies[0].split(";")[0] if cookies else None


# --------------------------------------------------------------------------
section(B, "Cookies")
# --------------------------------------------------------------------------
@test("Server sends a Set-Cookie header")
def _():
    r = cgi_get("session.py")
    sc = r.all_h("set-cookie")
    return r.status == 200 and any("=" in v for v in sc), "200 with a 'Set-Cookie: name=value' header", \
        "%s, Set-Cookie: %s" % (r.summary(), sc)


@test("Set-Cookie is a single, well-formed header line (name=value; attributes)")
def _():
    r = cgi_get("session.py")
    sc = r.all_h("set-cookie")
    ok = bool(sc) and re.match(r"^[^=;\s]+=[^;\s]*(;.*)?$", sc[0]) is not None
    return ok, "name=value[; Path=/; ...]", "Set-Cookie: %s" % sc


@test("Cookie header is forwarded to the CGI (HTTP_COOKIE)")
def _():
    r = cgi_get("echo.py", headers=[("Cookie", "tester=cookie42")])
    return b"HTTP_COOKIE=tester=cookie42" in r.body, "body contains HTTP_COOKIE=tester=cookie42", \
        "%s, body %r" % (r.summary(), r.body[:160])


@test("Several cookies in one Cookie header are forwarded intact")
def _():
    r = cgi_get("echo.py", headers=[("Cookie", "a=1; b=2; c=3")])
    return b"HTTP_COOKIE=a=1; b=2; c=3" in r.body, "HTTP_COOKIE=a=1; b=2; c=3", "body %r" % r.body[:200]


# --------------------------------------------------------------------------
section(B, "Sessions")
# --------------------------------------------------------------------------
@test("The cookie is sent back and the session is recognised")
def _():
    r1 = cgi_get("session.py")
    pair = first_cookie(r1)
    if not pair:
        return False, "Set-Cookie on the first visit", "no Set-Cookie header"
    _s, v1 = session_of(r1)
    cgi_get("session.py", headers=[("Cookie", pair)])
    _s, v3 = session_of(cgi_get("session.py", headers=[("Cookie", pair)]))
    return v1 == 1 and v3 == 3, "VISITS=1, then VISITS=3 on the third visit with the cookie", \
        "first VISITS=%s, third VISITS=%s (cookie sent: %s)" % (v1, v3, pair)


@test("Session is found among several cookies")
def _():
    pair = first_cookie(cgi_get("session.py"))
    if not pair:
        return False, "Set-Cookie on the first visit", "no Set-Cookie header"
    _s, v2 = session_of(cgi_get("session.py", headers=[("Cookie", "a=1; %s; z=9" % pair)]))
    return v2 == 2, "VISITS=2 (session found among 3 cookies)", "VISITS=%s" % v2


@test("A client without the cookie gets a new, separate session")
def _():
    s1, _v = session_of(cgi_get("session.py"))
    s2, v2 = session_of(cgi_get("session.py"))
    return bool(s1 and s2 and s1 != s2 and v2 == 1), "two different session ids, VISITS=1 each", \
        "ids %s / %s, VISITS=%s" % (s1, s2, v2)


@test("Session survives other traffic in between")
def _():
    pair = first_cookie(cgi_get("session.py"))
    if not pair:
        return False, "Set-Cookie on the first visit", "no Set-Cookie header"
    get(idx())
    get(CONFIG["missing_path"])
    cgi_get("hello.py")
    _s, v = session_of(cgi_get("session.py", headers=[("Cookie", pair)]))
    return v == 2, "VISITS=2 after other requests", "VISITS=%s" % v


@test("An invalid session id does not crash the server (new session or error)")
def _():
    r = cgi_get("session.py", headers=[("Cookie", "sid=!!!invalid;;;")])
    return survived(*status_in(r, {200, 400}))


@test("10 clients with their own sessions at the same time stay independent")
def _():
    def one(i):
        r1 = cgi_get("session.py")
        pair = first_cookie(r1)
        for _k in range(2):
            r = cgi_get("session.py", headers=[("Cookie", pair)])
        return session_of(r)[1]
    res = parallel(one, 10)
    return all(v == 3 for v in res), "every client sees VISITS=3", "visits: %s" % res


# --------------------------------------------------------------------------
section(B, "Multiple CGI types")
# --------------------------------------------------------------------------
@test("At least 2 different CGI interpreters work")
def _():
    types = need("cgi_types")
    need("cgi_base")
    good, bad = [], []
    for name, marker in types:
        r = cgi_get(name)
        (good if r.status == 200 and marker.encode() in r.body else bad).append(name)
    ok = len(set(os.path.splitext(n)[1] for n in good)) >= 2
    return ok, "at least 2 extensions served correctly", "working: %s / failing: %s" % (good, bad)


@test("Different CGI types running at the same time")
def _():
    types = need("cgi_types")
    need("cgi_base")
    res = parallel(lambda i: cgi_get(types[i % len(types)][0]), 15)
    bad = [i for i, r in enumerate(res) if isinstance(r, Exception) or r.status != 200 and r.status != 500]
    good = sum(1 for i, r in enumerate(res) if not isinstance(r, Exception) and
               types[i % len(types)][1].encode() in r.body)
    return survived(good >= 10, "most requests (at least the types you support) answered", "%d/15 correct" % good)


@test("Each CGI type receives POST data")
def _():
    need("cgi_base")
    r = cgi_post("echo.py", b"multi-type-body")
    return r.status == 200 and b"BODY=multi-type-body" in r.body, "echo.py returns the body", r.summary()


# --------------------------------------------------------------------------
section(B, "Cookie robustness")
# --------------------------------------------------------------------------
def cookie_case(name, value):
    def fn():
        need("cgi_base")
        r = cgi_get("echo.py", headers=[("Cookie", value() if callable(value) else value)])
        return survived(*status_in(r, {200, 400, 431}))
    add(name, fn)


cookie_case("Malformed Cookie header (;;;===)", ";;;===;")
cookie_case("Empty Cookie header", "")
cookie_case("Cookie with only a name", "novalue")
cookie_case("Cookie of 8 KB", lambda: "big=" + "a" * 8000)
cookie_case("50 cookies in one header", lambda: "; ".join("c%d=v%d" % (i, i) for i in range(50)))
cookie_case("Cookie with non-ASCII bytes", "x=\xe9\xff")


# ==========================================================================
#            EXTRA TESTS (optional features, option 4 of the menu)
# ==========================================================================
EXTRA_MAP = [
    ("Extended HTTP connections (not required explicitly by the subject)", [
        "GET with a body: body is consumed, next request on the connection still works",
        "Keep-alive: two requests on the same connection",
        "Keep-alive: 100 requests in a row on one connection",
        "An error answer (404) does not break the connection for the next request",
        "HTTP/1.0 request without keep-alive: server closes after the answer",
        "50 connections x 40 keep-alive requests each",
        "Leak hunt: keep-alive connections (20 requests each)",
    ]),
    ("Extended client compatibility", [
        "Expect: 100-continue does not hang (100 Continue or wait, then final answer)",
    ]),
    ("Multipart/form-data conventions (upload method not specified in 42 subject)", [
        "Upload a file with multipart/form-data POST",
        "Uploaded file can be downloaded again with the same content",
        "Upload of a binary file (all 256 byte values) is byte-identical",
        "Upload of a large file is byte-identical",
        "Upload of an empty file does not hang or crash",
        "Same filename upload is handled (success or 409 Conflict)",
        "Upload with a wrong boundary is refused or ignored (no crash)",
        "Truncated multipart body (no closing boundary): no crash",
        "Filename with path traversal does not escape the upload folder",
        "20 uploads at the same time are all stored",
        "DELETE removes the uploaded file (then GET -> 404)",
        "DELETE of the same file a second time returns 404",
    ]),
    ("Optional HTTP features", [
        "Status line has a reason phrase",
        "HEAD returns headers without a body (or is refused with 405/501)",
        "Absolute-form target (GET http://host/ HTTP/1.1)",
        "Asterisk target (GET * HTTP/1.1)"]),
    ("HTTP pipelining", [
        "Two requests in one packet get two answers, in order",
        "Second pipelined request arrives split in two packets",
        "POST with body followed by a GET in the same packet",
        "200 pipelined requests (small answers) are all returned, in order",
        "Pipeline flood: 300 requests in a single send"]),
    ("Additional compatibility and boundary tests", [
        "Server starts on the highest valid port (65535)",
        "Privileged port (80) without root: clean error, no crash",
        "Fragment sent by mistake (/index.html#top)",
        "Non-ASCII percent-encoded name (/%C3%A9t%C3%A9)",
        "Raw non-ASCII bytes in the URL",
        "1000 path segments (/a/a/a/...)",
        "8000-character query string",
        "Obsolete line folding (continuation line)",
        "Very long URI (10000 chars) is refused",
        "Header name of 10000 characters"]),
    ("Virtual hosting", [
        "Virtual hosts: Host header selects the site"]),
]


def _move_to_extra():
    index = {t[2]: i for i, t in enumerate(TESTS)}
    for title, names in EXTRA_MAP:
        SECTIONS.append((EXTRA, title))
        for nm in names:
            i = index[nm]                       # KeyError here = a test was renamed
            g, sec, name, fn, slow, mem = TESTS[i]
            TESTS[i] = (EXTRA, title, name, fn, slow, mem)
    order = [t for g, t in SECTIONS if g == EXTRA]
    TESTS.sort(key=lambda t: (GROUP_ORDER[t[0]], order.index(t[1]) if t[0] == EXTRA else 0))


_move_to_extra()


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------
def wrap(label, text, indent=13):
    print("  %s%s" % (label.ljust(indent), text))


def mem_verdict(mem, before):
    """-> (state, expected, got), state is 'pass', 'fail' or 'na'."""
    ov = STATE.pop("mem_override", None)
    if ov is not None:
        state = ov[0] if isinstance(ov[0], str) else ("pass" if ov[0] else "fail")
        return state, ov[1], ov[2]
    if not CONFIG["memory_check"]:
        return "na", "", "disabled with --no-memory"
    if MON is None:
        return "na", "", "server PID unknown: pass --pid N"
    if mem == "no":
        return "na", "", "this test does not use the main server process"
    if mem == "defer":
        return "na", "", "script still running by design; see 'After all tests' check"
    if before is None:
        return "na", "", "no baseline (server process not readable)"
    after = MON.settle(before, CONFIG["mem_settle"])
    if after is None:
        return "fail", "server process still running", "server process is gone"
    probs = []
    if before["fds"] >= 0 and after["fds"] > before["fds"] + CONFIG["mem_fd_tolerance"]:
        probs.append("%d file descriptor(s) not closed (%d -> %d)" % (
            after["fds"] - before["fds"], before["fds"], after["fds"]))
    if after["zombies"] > before["zombies"]:
        probs.append("%d zombie child process(es) not reaped (missing waitpid)" % (after["zombies"] - before["zombies"]))
    jump = after["rss"] - before["rss"]
    if jump > CONFIG["mem_rss_jump_kb"]:
        probs.append("memory grew by %d KB and stayed" % jump)
    if CONFIG["use_leaks_tool"]:
        res = leaks_run(MON.pid)
        if res is not None:
            if res[0] > LEAKS["count"]:
                probs.append("`leaks` reports %d new leak(s), +%d bytes" % (res[0] - LEAKS["count"], res[1] - LEAKS["bytes"]))
            LEAKS["count"], LEAKS["bytes"] = res
    info = "fds %d -> %d | zombies %d | RSS %+d KB" % (before["fds"], after["fds"], after["zombies"], jump)
    if probs:
        return "fail", "descriptors back to %d, no new zombie, no lasting RSS jump > %d KB" % (
            before["fds"], CONFIG["mem_rss_jump_kb"]), "; ".join(probs) + "  [" + info + "]"
    return "pass", "", info


def print_mem(state, exp, got):
    if state == "pass":
        print("Memory Leak : " + c("PASS", GREEN) + "  (%s)" % got)
    elif state == "fail":
        print("Memory Leak : " + c("FAIL", RED))
        print("Memory problem:")
        wrap("  Expected :", exp)
        wrap("  Got      :", got)
    elif state == "warn":
        print("Memory Leak : " + c("WATCH (RSS trend; not confirmed leak)", YELLOW))
        wrap("  Observed :", got)
        print("  Check native RSS and the final Valgrind heap report.")
    else:
        print("Memory Leak : " + c("N/A", YELLOW) + " (%s)" % got)


def run(groups, only=None):
    del MEM_WARNINGS[:]
    selected = [t for t in TESTS if t[0] in groups and (not only or only.lower() in t[1].lower())]
    stats = {}
    for g, title in SECTIONS:
        stats[(g, title)] = [0, 0, 0, 0, 0]       # passed, counted, skipped, mem passed, mem counted
    crashed = False
    last = None
    if VALGRIND_ACTIVE:
        valgrind_new_errors()  # Exclude Valgrind startup and test warm-up.
    for idx_, (group, sec, name, fn, slow, mem) in enumerate(selected, 1):
        key = (group, sec)
        if key != last:
            last = key
            nums = [t for g, t in SECTIONS if g == group]
            n = nums.index(sec) + 1
            cnt = sum(1 for t in selected if t[0] == group and t[1] == sec)
            print(c("\n==== %s %d/%d: %s  (%d tests) ====\n" % (group.upper(), n, len(nums), sec, cnt), BOLD))
        print("Test-Case   : [%03d/%03d] %s" % (idx_, len(selected), name))
        st = stats[key]
        if crashed:
            st[2] += 1
            print("Status      : " + c("SKIP", YELLOW) + " (server process exited; not run)")
            print("Memory Leak : " + c("N/A", YELLOW) + " (not run)\n")
            continue
        STATE.pop("mem_override", None)
        before = MON.snap() if (MON and mem == "yes") else None
        try:
            if QUICK and slow:
                raise Skip("slow test, --quick")
            ok, exp, got = fn()
        except Skip as e:
            STATE.pop("mem_override", None)
            st[2] += 1
            print("Status      : " + c("SKIP", YELLOW) + " (%s)\n" % e)
            continue
        except Exception as e:  # noqa
            ok, exp, got = False, "test runs without error", "%s: %s" % (type(e).__name__, e)
        st[1] += 1
        if ok:
            st[0] += 1
            print("Status      : " + c("PASS", GREEN))
        else:
            print("Status      : " + c("FAIL", RED))
            print("What's wrong:")
            wrap("  Expected :", exp)
            wrap("  Got      :", got)
        if not ok and not alive():
            # A slow or saturated server is not necessarily a crashed process.
            # Stop subsequent tests only when we can establish that it exited.
            pid = MON.pid if MON else CONFIG.get("server_pid")
            dead = bool(AUTO_PROCESS is not None and AUTO_PROCESS.poll() is not None)
            if pid is not None:
                dead = dead or process_gone(int(pid))
            if dead:
                print(c("  !! Server process exited; remaining cases cannot run.", RED))
                crashed = True
                print("Memory Leak : " + c("N/A", YELLOW) + " (server process exited)")
                if VALGRIND_ACTIVE:
                    issues = valgrind_new_errors()
                    print("Valgrind    : %s" % (c("FAIL", RED) if issues else c("PASS", GREEN)))
                    for issue in issues[:3]:
                        print("  " + issue)
                print()
                continue
            print(c("  !! Server did not answer health checks, but no process exit "
                    "was confirmed; continuing with later cases.", YELLOW))
        m_state, m_exp, m_got = mem_verdict(mem, before)
        print_mem(m_state, m_exp, m_got)
        if m_state in ("pass", "fail", "warn"):
            st[4] += 1
            if m_state in ("pass", "warn"):
                st[3] += 1  # RSS warnings are not confirmed heap leaks.
            if m_state == "warn":
                MEM_WARNINGS.append((name, m_got))
        if VALGRIND_ACTIVE:
            if mem == "no":
                print("Valgrind    : %s (separate startup process; not instrumented)" % c("N/A", YELLOW))
                valgrind_new_errors()
            else:
                issues = valgrind_new_errors()
                print("Valgrind    : %s (%s)" % (
                    c("FAIL", RED) if issues else c("PASS", GREEN),
                    "%d newly reported memory error(s)" % len(issues) if issues else
                    "no new runtime errors; heap leaks checked at shutdown"))
                for issue in issues[:3]:
                    print("  " + issue)
                if issues:
                    # Per-case Valgrind errors contribute to the memory result.
                    if m_state in ("pass", "warn"):
                        st[3] -= 1
                    elif m_state == "na":
                        st[4] += 1
        print()
    return stats, selected


def final_report(stats, groups, valgrind_result=None):
    print(c("\n======================== RESULT BY SECTION ========================", BOLD))
    print("       %-58s %-16s %s" % ("Section", "Test result", "Memory Leak"))
    tp = tc = ts = mp = mc = 0
    for g in groups:
        gp = gc = gmp = gmc = 0
        print(c("\n%s" % g.upper(), CYAN))
        for (gg, title), (p, n, sk, m_p, m_n) in stats.items():
            if gg != g or (p + n + sk == 0):
                continue
            mark = c("OK  ", GREEN) if p == n and n else (c("--  ", YELLOW) if n == 0 else c("FAIL", RED))
            mtxt = "%3d out of %-3d" % (m_p, m_n) if m_n else "n/a"
            if m_n and m_p < m_n:
                mtxt = c(mtxt, RED)
            print("  %s %-58s %3d out of %-3d    %s%s" % (mark, title[:58], p, n, mtxt,
                                                          ("   (%d skipped)" % sk) if sk else ""))
            gp, gc, gmp, gmc, ts = gp + p, gc + n, gmp + m_p, gmc + m_n, ts + sk
        mtxt = "%3d out of %-3d" % (gmp, gmc) if gmc else "n/a"
        print("  %-63s %3d out of %-3d    %s" % (g.capitalize() + " total", gp, gc, mtxt))
        tp, tc, mp, mc = tp + gp, tc + gc, mp + gmp, mc + gmc
    if ts:
        print("\n(%d test(s) skipped: not configured / --quick, not counted)" % ts)
    color = GREEN if tp == tc else (YELLOW if tp >= tc * 0.7 else RED)
    print(c("\nTotal Result: %d out of %d" % (tp, tc), color))
    if mc:
        print(c("Memory Leak Result: %d out of %d" % (mp, mc), GREEN if mp == mc else RED))
        if MON is not None:
            print("  (FD/zombie checks; RSS trends reported separately, not proof of a leak)")
    if MEM_WARNINGS:
        print(c("RSS trend warnings: %d (not counted as confirmed memory leaks)" % len(MEM_WARNINGS), YELLOW))
        print("  Recheck the same section with --no-valgrind and inspect final Memcheck results.")
    else:
        print("Memory Leak Result: not measured (server PID unknown or --no-memory)")
    if valgrind_result is not None:
        ok, _expected, details = valgrind_result
        print("Valgrind final heap check: %s" % c("PASS" if ok else "FAIL", GREEN if ok else RED))
        print("  " + details)
        print("  (whole-process shutdown summary; not an additional HTTP test case)")
    elif VALGRIND_ACTIVE:
        print("Valgrind final heap check: N/A (no valid report)")
    return tp == tc and mp == mc and (valgrind_result is None or valgrind_result[0])


# --------------------------------------------------------------------------
# Sample site generator
# --------------------------------------------------------------------------
ECHO_PY = '''#!/usr/bin/env python3
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
'''

SESSION_PY = '''#!/usr/bin/env python3
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
'''

SAMPLES = {
    "www/index.html": ("<html><body><h1>webserv sample index</h1></body></html>\n", 0o644),
    "www/about.html": ("<html><body><h1>About</h1><p>sample page</p></body></html>\n", 0o644),
    "www/hello world.txt": ("hello world\n", 0o644),
    "www/sub/dir/deep.html": ("<html><body>deep page</body></html>\n", 0o644),
    "www/listing/a.txt": ("file a\n", 0o644),
    "www/listing/b.txt": ("file b\n", 0o644),
    "www/empty/.keep": ("", 0o644),
    "www/uploads/.keep": ("", 0o644),
    "www/errors/404.html": ("<h1>TESTER CUSTOM 404</h1>\n", 0o644),
    "second_www/index.html": ("<h1>Second Port OK</h1>\n", 0o644),
    "www/cgi-bin/hello.py": ('#!/usr/bin/env python3\nprint("Content-Type: text/plain")\nprint()\n'
                             'print("Hello from CGI (python)")\n', 0o755),
    "www/cgi-bin/hello.sh": ('#!/bin/sh\necho "Content-Type: text/plain"\necho ""\n'
                             'echo "Hello from CGI (sh)"\n', 0o755),
    "www/cgi-bin/hello.pl": ('#!/usr/bin/perl\nprint "Content-Type: text/plain\\n\\n";\n'
                             'print "Hello from CGI (perl)\\n";\n', 0o755),
    "www/cgi-bin/echo.py": (ECHO_PY, 0o755),
    "www/cgi-bin/crash.py": ('#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n', 0o755),
    "www/cgi-bin/bad_shebang.py": ('#!/nonexistent/interpreter\nprint("never runs")\n', 0o755),
    "www/cgi-bin/loop.py": ('#!/usr/bin/env python3\nimport time\nwhile True:\n    time.sleep(1)\n', 0o755),
    "www/cgi-bin/sleep.py": ('#!/usr/bin/env python3\nimport os, time\n'
                             'q = os.environ.get("QUERY_STRING", "")\n'
                             't = float(q.split("t=")[1].split("&")[0]) if "t=" in q else 2\n'
                             'time.sleep(t)\nprint("Content-Type: text/plain")\nprint()\nprint("SLEPT")\n', 0o755),
    "www/cgi-bin/big.py": ('#!/usr/bin/env python3\nimport sys\nsys.stdout.write("Content-Type: text/plain\\n\\n")\n'
                           'sys.stdout.write("A" * 1000000)\n', 0o755),
    "www/cgi-bin/relative.txt": ("RELATIVE_OK\n", 0o644),
    "www/cgi-bin/relative.py": ('#!/usr/bin/env python3\nprint("Content-Type: text/plain")\nprint()\n'
                                'print(open("relative.txt").read())\n', 0o755),
    "www/cgi-bin/session.py": (SESSION_PY, 0o755),
}

LAYOUT = """
Recommended webserv routes for the sample site (adapt to your config syntax):

  listen 8080 (and 8081 for the multi-port test)
  /            root  <DIR>/www            methods GET        index index.html
  /listing/    root  <DIR>/www/listing    methods GET        autoindex ON
  /empty/      root  <DIR>/www/empty      methods GET        autoindex OFF, no index
  /redirect    return 301 /index.html
  /cgi-bin     root  <DIR>/www/cgi-bin    methods GET POST   CGI: .py -> python3, .sh -> sh, .pl -> perl
  /upload      methods POST               upload store <DIR>/www/uploads
  /uploads/    root  <DIR>/www/uploads    methods GET DELETE
  /limited     methods POST               client_max_body_size 1000

Optional, for section 1 (valid/invalid ports, config errors):
  copy a WORKING config of yours, write its listen port as {{PORT}} (and a second
  port as {{PORT2}} if it has two servers), save it, and run with
      --bin ./webserv --conf-template your_template.conf
"""


def gen_samples(dest):
    for rel, (content, mode) in SAMPLES.items():
        p = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)
        os.chmod(p, mode)
    with open(os.path.join(dest, "www", "big.bin"), "wb") as f:
        f.write(os.urandom(2 * 1024 * 1024))
    cfg = dict(CONFIG)
    cfg.update({
        "extra_ports": [8081],
        "www_root": os.path.abspath(os.path.join(dest, "www")),
        "upload_dir": os.path.abspath(os.path.join(dest, "www", "uploads")),
        "get_only_path": "/index.html",
        "post_path": "/post_body",
        "index_contains": "webserv sample index",
        "custom_404_contains": "TESTER CUSTOM 404",
        "extra_port_expect": {"8081": "Second Port OK"},
        "cgi_types": [["hello.py", "Hello from CGI (python)"]],
        "upload_path": "/upload",
        "upload_fetch_prefix": "/uploads/",
        "delete_prefix": "/uploads/",
        "max_body_path": "/limited",
        "max_body_size": 1000,
        "autoindex_on_path": "/listing/",
        "autoindex_off_path": "/empty/",
        "redirect_path": "/redirect",
        "redirect_location": "/index.html",
        "cgi_base": "/cgi-bin",
    })
    cpath = os.path.join(dest, "tester_config.example.json")
    with open(cpath, "w") as f:
        json.dump(cfg, f, indent=2)
    print("Sample site written to %s" % os.path.abspath(dest))
    print("Example tester config: %s" % cpath)
    print(LAYOUT.replace("<DIR>", os.path.abspath(dest)))


# --------------------------------------------------------------------------
def find_webserv_binary(specified=None):
    """Find a Webserv binary without depending on the user's login or shell cwd."""
    options = [specified] if specified else [
        os.path.join(SCRIPT_DIR, "..", "webserv"),
        os.path.join(os.getcwd(), "webserv"),
        os.path.join(SCRIPT_DIR, "webserv"),
    ]
    for item in options:
        if item and os.path.isfile(os.path.abspath(item)):
            path = os.path.abspath(item)
            if not os.access(path, os.X_OK):
                raise RuntimeError("Webserv is not executable: %s (run make first)" % path)
            return path
    raise RuntimeError("Webserv executable not found. Run make, then provide --bin /path/to/webserv")


def make_test_setup(dest=None, binary=None, port=8080, port2=None):
    """Create an isolated fixture site, Webserv conf, port template, and tester JSON."""
    dest = os.path.abspath(dest or os.path.join(SCRIPT_DIR, "tester_site"))
    port2 = port2 or port + 1
    if port < 1 or port2 > 65535 or port2 == port:
        raise RuntimeError("Choose an ordinary port 1..65534 (a second port is required)")
    for rel, (content, mode) in SAMPLES.items():
        path = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        os.chmod(path, mode)
    big = os.path.join(dest, "www", "big.bin")
    if not os.path.isfile(big):
        with open(big, "wb") as f:
            f.write(os.urandom(2 * 1024 * 1024))
    template_source = os.path.join(SCRIPT_DIR, "config", "webserv.conf.in")
    with open(template_source) as f:
        template = f.read()
    interpreter = os.path.realpath(sys.executable)
    for key, value in (("SITE", dest), ("PYTHON", interpreter)):
        if any(ch.isspace() for ch in value):
            raise RuntimeError("The supplied Webserv config parser cannot handle spaces in %s: %r" % (key, value))
        template = template.replace("{{%s}}" % key, value)
    # Keep placeholders in the dynamic config for the startup/port tests.
    tpath = os.path.join(dest, "webserv_test.template.conf")
    with open(tpath, "w") as f:
        f.write(template)
    conf = template.replace("{{PORT}}", str(port)).replace("{{PORT2}}", str(port2))
    cpath = os.path.join(dest, "webserv_test.conf")
    with open(cpath, "w") as f:
        f.write(conf)
    binary_path = os.path.abspath(binary) if binary else None
    settings = dict(CONFIG)
    settings.update({
        "host": "127.0.0.1", "port": port,
        "extra_ports": [port2], "extra_port_expect": {str(port2): "Second Port OK"},
        "server_bin": binary_path, "conf_template": tpath,
        "server_cwd": os.path.dirname(binary_path) if binary_path else os.getcwd(),
        "www_root": os.path.join(dest, "www"),
        "index_path": "/", "index_contains": "webserv sample index",
        "static_files": [["/index.html", "text/html"], ["/about.html", "text/html"]],
        "download_files": [["/big.bin", big]],
        "missing_path": "/missing_webserv_test_file_zz",
        "custom_404_contains": "TESTER CUSTOM 404",
        "get_only_path": "/index.html", "post_path": "/post_body",
        "upload_path": "/upload", "upload_dir": os.path.join(dest, "www", "uploads"),
        "upload_fetch_prefix": "/uploads/", "delete_prefix": "/uploads/",
        "max_body_path": "/limited", "max_body_size": 1000,
        "autoindex_on_path": "/listing/", "autoindex_off_path": "/empty/",
        "redirect_path": "/redirect", "redirect_location": "/index.html",
        "cgi_base": "/cgi-bin", "cgi_types": [["hello.py", "Hello from CGI (python)"]],
        "cgi_big_body": 200000,
    })
    jpath = os.path.join(dest, "tester_config.json")
    with open(jpath, "w") as f:
        json.dump(settings, f, indent=2)
    return cpath, tpath, jpath


def wait_started(proc, port, seconds=15):
    end = time.time() + seconds
    while time.time() < end:
        if proc.poll() is not None:
            return False
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=0.25)
            sock.close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def start_owned_server(binary, conf, port, use_valgrind=False, workspace=None):
    """Start only our temporary server; never hijack a listener already running."""
    global AUTO_PROCESS
    try:
        probe = socket.create_connection(("127.0.0.1", port), timeout=0.3)
    except OSError:
        pass
    else:
        probe.close()
        raise RuntimeError("Port %d already has a server. Stop it or use --port N." % port)
    argv = [binary, conf]
    if use_valgrind:
        vg = shutil.which("valgrind")
        if vg is None:
            raise RuntimeError("Valgrind was requested but is not installed.")
        # A unique file per run prevents old logs from affecting new verdicts.
        run_id = uuid.uuid4().hex[:12]
        basename = "valgrind.%s.%%p.log" % run_id
        vgfile = os.path.join(workspace, basename)
        CONFIG["valgrind_log"] = os.path.join(workspace, "valgrind.%s.*.log" % run_id)
        argv = [vg, "--leak-check=full", "--show-leak-kinds=all",
                "--track-origins=yes", "--error-limit=no",
                "--trace-children=no", "--log-file=" + vgfile] + argv
    cwd = os.path.dirname(binary)
    log_path = os.path.join(workspace, "server.log")
    log = open(log_path, "w")
    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT)
    finally:
        log.close()
    AUTO_PROCESS = proc
    if not wait_started(proc, port, seconds=25 if use_valgrind else 10):
        try:
            with open(log_path) as f:
                content = f.read()[-1600:]
        except OSError:
            content = "(no log)"
        raise RuntimeError("Webserv did not listen on port %d. Log:\n%s" % (port, content))
    return proc


def stop_owned_server(proc):
    """Never stop an external server."""
    if proc is None or proc.poll() is not None:
        return
    pid = MON.pid if MON else find_server_pid(CONFIG["port"])
    if pid:
        try:
            os.kill(pid, signal.SIGINT)
        except OSError:
            pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def ask_choice():
    print(c("\nWebserv tester", BOLD))
    print("What do you want to test?")
    print("  1. Mandatory Test")
    print("  2. Bonus Test")
    print("  3. Both (Mandatory + Bonus)")
    print("  4. Extra Tests (optional features and edge cases)")
    print("  Valgrind memory checks run automatically on Linux if installed.")
    while True:
        try:
            a = input("Choose [1-4]: ").strip()
        except EOFError:
            sys.exit(1)
        if a in ("1", "2", "3", "4"):
            return a
        print("Please type a number from 1 to 4.")


def list_sections():
    for g in (MANDATORY, BONUS, EXTRA):
        print(c(g.upper(), BOLD))
        for n, (gg, title) in enumerate([s for s in SECTIONS if s[0] == g], 1):
            cnt = sum(1 for t in TESTS if t[0] == g and t[1] == title)
            print("  %2d. %-62s %3d tests" % (n, title, cnt))


def main():
    global QUICK, VALGRIND_ACTIVE
    ap = argparse.ArgumentParser(description="Webserv tester (mandatory, bonus, extra, memory leaks)")
    ap.add_argument("--auto", action="store_true", help="generate fixtures/config, launch Webserv, test, and stop it")
    ap.add_argument("--external", action="store_true", help="use an already running Webserv without starting/stopping it")
    ap.add_argument("--setup", nargs="?", const="", metavar="DIR", help="generate fixtures, Webserv .conf and tester JSON, then exit")
    ap.add_argument("--valgrind", action="store_true", help="require Valgrind (automatic on Linux when installed)")
    ap.add_argument("--no-valgrind", action="store_true", help="disable automatic Valgrind (faster performance testing)")
    ap.add_argument("--stop-server", action="store_true", help="allow --valgrind-log to stop an EXTERNAL server (destructive)")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--config", help="JSON file overriding CONFIG keys (external server mode)")
    ap.add_argument("--bin", help="path to your webserv binary (section 1)")
    ap.add_argument("--conf-template", help="working config with {{PORT}} placeholder (section 1)")
    ap.add_argument("--choice", choices=["1", "2", "3", "4"], help="skip the menu: 1=Mandatory, 2=Bonus, 3=Both, 4=Extra")
    ap.add_argument("--section", help="only run sections whose title contains this text")
    ap.add_argument("--quick", action="store_true", help="skip slow tests")
    ap.add_argument("--pid", type=int, help="PID of the running server (memory checks)")
    ap.add_argument("--no-memory", action="store_true", help="turn the memory leak checks off")
    ap.add_argument("--mem-scale", type=float, help="multiply the repetitions of the leak hunts")
    ap.add_argument("--valgrind-log", help="glob of the external Valgrind log; requires --stop-server to parse after shutdown")
    ap.add_argument("--stop-signal", help="signal that stops the server (default SIGINT)")
    ap.add_argument("--leaks", action="store_true", help="macOS: run `leaks PID` after every test")
    ap.add_argument("--list", action="store_true", help="list sections and exit")
    ap.add_argument("--gen-samples", metavar="DIR", help="write sample site + example config")
    a = ap.parse_args()
    if a.valgrind and a.no_valgrind:
        ap.error("Choose either --valgrind or --no-valgrind")
    if a.valgrind and a.external:
        ap.error("--valgrind requires --auto; for an external Valgrind server use --valgrind-log --stop-server")
    if a.auto and a.external:
        ap.error("Choose --auto or --external, not both")
    if a.auto and a.config:
        ap.error("--auto creates its own JSON; use --external --config to test an existing setup")

    if a.gen_samples:
        gen_samples(a.gen_samples)
        return 0
    if a.list:
        list_sections()
        return 0
    if a.setup is not None:
        binary = find_webserv_binary(a.bin) if a.bin else None
        cpath, tpath, jpath = make_test_setup(a.setup or None, binary=binary, port=a.port or 8080)
        print("Fixtures and configuration generated:")
        print("  Webserv: %s" % cpath)
        print("  Port template: %s" % tpath)
        print("  Tester JSON: %s" % jpath)
        print("Start your server with: ./webserv %s" % cpath)
        print("Then run: python3 webserv_tester.py --external --config %s" % jpath)
        return 0

    # A bare call behaves like the school tester: own the server and present a menu.
    owned = a.auto or (a.valgrind and not a.external and not a.config) or (not a.external and not a.config and not a.valgrind_log and not a.host and not a.port and not a.pid)
    if a.valgrind and not owned:
        ap.error("--valgrind requires --auto")
    if owned:
        binary = find_webserv_binary(a.bin)
        cpath, tpath, jpath = make_test_setup(binary=binary, port=a.port or 8080)
        with open(jpath) as f:
            CONFIG.update(json.load(f))
        CONFIG["server_bin"] = binary
        CONFIG["server_cwd"] = os.path.dirname(binary)
        print("Fixture folder: %s" % os.path.dirname(cpath))
        print("Webserv config: %s" % cpath)
        print("Port-check template: %s" % tpath)
    if a.config:
        with open(a.config) as f:
            CONFIG.update(json.load(f))
    for opt, key in ((a.host, "host"), (a.port, "port"), (a.bin, "server_bin"),
                     (a.conf_template, "conf_template"), (a.pid, "server_pid"),
                     (a.mem_scale, "mem_hunt_scale"), (a.valgrind_log, "valgrind_log"),
                     (a.stop_signal, "stop_signal")):
        if opt:
            CONFIG[key] = opt
    if a.no_memory:
        CONFIG["memory_check"] = False
    if a.leaks:
        CONFIG["use_leaks_tool"] = True
    QUICK = a.quick
    if CONFIG.get("www_root") and not CONFIG["download_files"]:
        big = os.path.join(CONFIG["www_root"], "big.bin")
        if os.path.exists(big):
            CONFIG["download_files"] = [["/big.bin", big]]

    choice = a.choice or ask_choice()
    groups = {"1": [MANDATORY], "2": [BONUS], "3": [MANDATORY, BONUS],
              "4": [EXTRA]}[choice]

    # One run of each selected test, optionally instrumented throughout by Memcheck.
    # No separate 'Quick Mandatory' or 'Valgrind Mandatory' menu entries.
    if owned and not a.no_valgrind and sys.platform.startswith("linux"):
        vg_path = shutil.which("valgrind")
        if vg_path:
            a.valgrind = True
            print("Valgrind: enabled automatically (one instrumented test pass; may be slower).")
        elif a.valgrind:
            raise RuntimeError("Valgrind requested but not installed. Install valgrind or omit --valgrind.")
        else:
            print("Valgrind: not installed; continuing with per-case FD/zombie/RSS checks.")
    elif a.valgrind:
        raise RuntimeError("Valgrind mode requires an owned Linux Webserv process.")
    elif owned:
        print("Valgrind: disabled or unavailable; per-case FD/zombie/RSS checks remain active.")
    VALGRIND_ACTIVE = bool(owned and a.valgrind)

    own_proc = None
    try:
        if owned:
            own_proc = start_owned_server(binary, cpath, CONFIG["port"],
                                          use_valgrind=a.valgrind, workspace=os.path.dirname(cpath))
            print("Started Webserv (PID %d); server log is in %s" % (
                  own_proc.pid, os.path.join(os.path.dirname(cpath), "server.log")))
        print("\nTesting server at %s:%d ..." % (CONFIG["host"], CONFIG["port"]))
        if not alive():
            print(c("Cannot reach the server. Verify the config/port.", RED))
            return 2
        setup_memory()
        stats, selected = run(groups, a.section)

        vg_result = None
        if CONFIG["valgrind_log"]:
            if not owned and not a.stop_server:
                print("\nValgrind final report not checked: refusing to stop an external server without --stop-server.")
                print("For automatic Valgrind, use the one-command launcher.")
            else:
                vg_result = valgrind_check()
        return 0 if final_report(stats, groups, vg_result) else 1
    finally:
        if owned:
            stop_owned_server(own_proc)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
    except (RuntimeError, OSError, ValueError) as e:
        print("Setup error: %s" % e, file=sys.stderr)
        sys.exit(2)
