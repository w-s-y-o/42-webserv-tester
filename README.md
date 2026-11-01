# Webserv Tester v5.1 — one-command testing for 42 Webserv

An independent black-box HTTP tester for the **42 Webserv** project (subject v24.1). It covers static files, GET/POST/DELETE, configuration, HTTP parsing, CGI, uploads, chunked bodies, connection resilience, resource handling and stress. It is **not** the official school tester or an official grading rubric.

**One menu choice = one run of the selected tests.** The former *Quick Mandatory* and *Quick Mandatory + Valgrind* menu choices were merged into **Mandatory Test**. Valgrind is automatically enabled on Linux when installed; it instruments **the same run**, not a duplicate run. If unavailable, the tester continues with file-descriptor, zombie-process and RSS monitoring. You can opt out of Valgrind for performance testing with `--no-valgrind`.

## Quick start

Requirements: Linux or macOS; Python **3.6+**, standard library only; a Webserv executable built by `make`. For Valgrind instrumentation, install `valgrind` on Linux. No Python packages or hardcoded usernames are required.

Place this folder in your Webserv project:

```text
webserv/
├── Makefile
├── webserv                   # compiled by make
└── webserv_tester/
    ├── run.sh
    ├── webserv_tester.py
    ├── README.md
    ├── config/webserv.conf.in
    └── tester_site/
        ├── www/             # sample static files, CGI programs, uploads
        └── second_www/
```

From the project root:

```sh
make
./webserv_tester/run.sh
```

The menu is:

```text
1. Mandatory Test
2. Bonus Test
3. Both (Mandatory + Bonus)
4. Extra Tests (optional features and edge cases)
```

Or skip the menu:

```sh
./webserv_tester/run.sh --choice 1                  # all mandatory-labelled tests, once
./webserv_tester/run.sh --choice 2                  # bonus
./webserv_tester/run.sh --choice 3                  # mandatory + bonus
./webserv_tester/run.sh --choice 4                  # extra compatibility checks
```

**No duplicate Mandatory passes.** Valgrind runs during the selected cases when available. To test without instrumentation overhead (especially important for timing and stress comparisons):

```sh
./webserv_tester/run.sh --choice 1 --no-valgrind
```

For a shorter *subset of the same Mandatory category*, use `--quick` instead of a separate menu item:

```sh
./webserv_tester/run.sh --choice 1 --quick
```

`--quick` intentionally skips tests flagged as slow, so it is not a complete evaluation of the Mandatory category. See the detailed section counts with `./webserv_tester/run.sh --list`.

## What the launcher does

The launcher automatically:

1. Locates the `webserv` executable next to the tester folder (or accepts its path).
2. Prepares an isolated sample site, CGI scripts, an upload folder and error pages.
3. Renders the Webserv `.conf`, a dynamic-port config template and the matching tester `.json` with local absolute paths.
4. Starts **only its own** Webserv process on `127.0.0.1:8080` and `127.0.0.1:8081` by default.
5. Runs every selected test case once, with per-case process monitoring; when Valgrind is present, checks newly reported runtime memory errors after each case.
6. Stops its owned Webserv process and, when instrumented, evaluates the final Valgrind heap-leak/error summary.

It does **not** kill unrelated Webserv processes. If the port is already in use, choose a free one:

```sh
./webserv_tester/run.sh --choice 1 --port 8090
```

For a tester stored outside the project, specify the executable explicitly:

```sh
./run.sh /absolute/path/to/webserv --choice 1
```

The included Webserv template uses these directives: `server`, `listen`, `root`, `location`, `methods`, `index`, `directory_listing`, `error_page`, `redirect`, `upload`, `upload_store`, `client_max_body_size` and `cgi`. **42 Webserv configuration syntax is not standardized.** If a different student wrote a different parser, they may need to adapt only `config/webserv.conf.in` before running the tester. A config parsing error is not a server feature failure.

## Files and sample routes

Files included in the package are portable. Running `./run.sh` generates machine-specific files inside `tester_site/`:

| File | Purpose |
|---|---|
| `webserv_test.conf` | Real config passed to the user's Webserv |
| `webserv_test.template.conf` | Same config, using `{{PORT}}` and `{{PORT2}}` placeholders for port/startup tests |
| `tester_config.json` | Python-side URLs, expectations and local directories |
| `server.log` | Webserv stdout/stderr |
| `valgrind.<run-id>.<pid>.log` | Valgrind output, when enabled |

Example routes: `/` and `/index.html` (GET); `/listing/` (autoindex on); `/empty/` (autoindex off); `/redirect` (301); `/cgi-bin/*.py` (Python CGI GET/POST); `/post_body` (POST echo); `/upload/<name>` (raw upload); `/upload` (multipart route if supported); `/uploads/<name>` (GET/DELETE); `/limited` (1000-byte limit). A second port serves `Second Port OK`.

To generate the fixtures and config **without** starting Webserv:

```sh
python3 webserv_tester.py --setup --bin ../webserv
```

To test a Webserv process you start yourself, first generate fixtures, then start it with the generated config:

```sh
# Terminal 1
./webserv webserv_tester/tester_site/webserv_test.conf

# Terminal 2
python3 webserv_tester/webserv_tester.py --external \
    --config webserv_tester/tester_site/tester_config.json --choice 1
```

External mode does not start or stop the server and does **not** automatically attach Valgrind to an already-running process. Use the one-command launcher for automatic instrumentation. The original `--gen-samples DIR` command is retained for manual fixture generation, but `--setup` also generates the real `.conf` files.

## Valgrind and memory checking

On Linux, **Valgrind is automatic for the one-command launcher if installed**. It runs Memcheck over the Webserv process while the normal selected test cases run:

```text
valgrind --leak-check=full --show-leak-kinds=all \
         --track-origins=yes --error-limit=no --trace-children=no \
         --log-file=<unique-log-path> ./webserv <generated.conf>
```

For each test, the tester prints:

- `Status`: whether the HTTP behavior matched the expectation.
- `Memory Leak`: per-test **process resource** result (file descriptors, zombie children and persistent RSS growth). This is **not** an exact per-request heap-leak verdict.
- `Valgrind` (when enabled): newly observed Memcheck runtime-error lines such as invalid reads/writes or uninitialized-value use. These are best-effort associations with the current case; asynchronous/duplicate diagnostics may appear later or be merged by Valgrind.

After the **single test run**, the tester requests server shutdown and checks the complete Valgrind leak report (`definitely lost`, `indirectly lost`, `possibly lost`, plus `ERROR SUMMARY`). **Actual heap leaks are assessed after process exit, not independently for each HTTP request.** Reliable, exact per-request heap-leak attribution would require instrumenting the server or restarting it for individual cases, which would slow the suite and repeat work. Server-startup/config tests launch separate helper server processes and are not individually instrumented by Valgrind.

If Valgrind is missing, the tester tells you and still runs all selected tests using resource monitoring; it never claims Valgrind passed when it did not run. On macOS, Valgrind is skipped; `--leaks` optionally enables Apple's `leaks` tool.

**Valgrind slows execution.** Some tests have real-time latency constraints; a timing failure under Memcheck may reflect instrumentation overhead rather than a true production performance problem. To confirm latency, use the same test section with `--no-valgrind`. This is a *targeted rerun for diagnosis*, not an automatic duplicate of the full Mandatory suite.

For a meaningful final heap report, Webserv should handle `SIGINT`, leave the main loop, close its sockets and reap children. If it terminates abnormally, inspect the Valgrind log before interpreting the verdict. Valgrind wasn't installed in the packaging environment; automatic fallback, Python syntax, normal launcher execution and control flow were tested, but a real Memcheck run was not.

To explicitly require Valgrind (error if missing):

```sh
./webserv_tester/run.sh --choice 1 --valgrind
```

To switch it off:

```sh
./webserv_tester/run.sh --choice 1 --no-valgrind
```

External Valgrind logs remain supported via `--external --valgrind-log 'path/glob' --stop-server`, which **explicitly permits stopping the external process** to read its exit report. Without `--stop-server`, the tester will not send a signal to a user-owned process.

## Choose a topic without running everything

```sh
./webserv_tester/run.sh --choice 1 --section "CGI"
./webserv_tester/run.sh --choice 1 --section "Upload"
./webserv_tester/run.sh --choice 1 --section "URL validation"
./webserv_tester/run.sh --choice 1 --section "Memory" --mem-scale 0.2
./webserv_tester/run.sh --list
```

The counts in v5 are **236 mandatory-labelled, 19 bonus, 39 extra** (294 total). `SKIP` is not counted as a failure. Some expectations follow widely used HTTP conventions, not explicit 42 subject text; use the *Expected/Got* diagnostic to decide whether a failure represents a real mandatory issue.

**Mandatory-labelled** focuses on configuration and ports, serving static files, GET/POST/DELETE, limits and chunked decoding, directory/index/redirect, parsing and security, CGI, partial packets, non-blocking I/O, connection resilience, timeouts and process resources.

**Bonus** checks cookies, sessions and multiple CGI types. **Extra** checks optional HTTP features, persistent keep-alive, pipelining, multipart-specific behavior, HTTP/1.0 compatibility and virtual hosts. This separation is designed to avoid lowering the Mandatory score for features not explicitly required by the current subject.

## CLI options

| Option | Effect |
|---|---|
| `--choice 1..4` | Mandatory / Bonus / Both / Extra; skip the menu |
| `--auto` | Generate configs, own the Webserv process and run tests (default for `run.sh`) |
| `--external --config FILE` | Test an already running server; do not stop it |
| `--bin PATH` | Specify the Webserv executable |
| `--port N` | Choose the first listening port; second is N+1 |
| `--setup [DIR]` | Generate sample site and config without running tests |
| `--quick` | Skip slow cases inside the same category |
| `--section TEXT` | Run a specific topic |
| `--valgrind` | Require Valgrind; enabled automatically on Linux when available |
| `--no-valgrind` | Disable auto Valgrind for native-speed checks |
| `--no-memory` | Disable per-case FD/zombie/RSS checks |
| `--mem-scale F` | Reduce or increase repetition of the leak hunts |
| `--pid N` | Override automatic PID detection for resource checks |
| `--list` | List categories, sections and test counts |
| `--leaks` | macOS: enable the `leaks` tool, if installed |
| `--valgrind-log GLOB --stop-server` | Read external Valgrind output and explicitly stop that server |

## Troubleshooting and limitations

- **Cannot find executable:** build with `make`, or provide the path as the first argument to `run.sh`.
- **Port already used:** stop the other server or add `--port 8090`.
- **Invalid Webserv config:** adjust `config/webserv.conf.in` to your parser's syntax.
- **CGI requests fail:** check Python installation and `tester_site/server.log`. The launcher inserts the local Python interpreter into the configuration.
- **Valgrind missing:** install it on Linux for heap diagnostics. The rest of the suite still runs without it.
- **Valgrind final report incomplete:** check `SIGINT` cleanup and inspect the generated Valgrind log.
- **Stress tests fail only under Valgrind:** verify timing at native speed with `--no-valgrind`.
- **Too many `SKIP`s:** check the generated tester JSON and routes. The CLI `--quick` explicitly skips slow cases.
- **Potential false-positive memory growth:** the allocator may keep freed RSS pages; use the final Memcheck report.

Tests may create and delete `tester_*` files in the **isolated test site's upload directory**. The full suite may be resource intensive; avoid running heavy stress tests on shared production servers.

This is a black-box suite. It cannot prove a single `poll()` use, C++98 compliance, the authorized-function list, or other code-review requirements. Use it alongside the school's tester and peer source review. AI assistance was used for review, classification, fixtures, launcher automation and documentation; understand and verify the resulting behavior before submitting it.

## v5.1 reporting corrections

- A safe HTTP 403 for `/x/../index.html` is accepted. A mandatory test no longer requires a particular limit for a 10,000-character URI or header name; these checks remain available under **Extra**.
- Slow-reader worker socket resets are reported as partial downloads, rather than dumping unhandled Python thread tracebacks.
- An unresponsive server is **not** automatically declared crashed. The tester attempts to confirm process exit, continues testing if a process exit is not confirmed, and marks cases genuinely not executed after an exit as `SKIP` instead of inventing failed results.
- Valgrind adds substantial execution overhead; when investigating **timing-related** failures, use `./webserv_tester/run.sh --choice 1 --section "Checking sockets non-blocking" --no-valgrind`. This does not replace the separate whole-run memory check.
- Save a unique log for every run: `./webserv_tester/run.sh --choice 1 --no-valgrind 2>&1 | tee "tester_$(date +%Y%m%d_%H%M%S).log"`.
