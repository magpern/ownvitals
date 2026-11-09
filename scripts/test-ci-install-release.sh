#!/usr/bin/env bash
# Tests scripts/ci-install-release.sh against a loopback HTTP server (no real network): a cold
# cache, a warm cache, a download that answers 504 first, a wrong checksum, and a tampered cached
# tarball, a connection dropped mid-answer, and a download that never succeeds. Run by the `checks` job.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
install="$here/ci-install-release.sh"
work="$(mktemp -d)"
srv=""
cleanup() { [ -z "$srv" ] || kill "$srv" 2>/dev/null || true; rm -rf "$work"; }
trap cleanup EXIT

failures=0
report() { echo "$1 $2"; [ "$1" = ok ] || failures=$((failures + 1)); }

# The "release": a tarball with one file named tool.
mkdir -p "$work/src" "$work/www"
printf '#!/bin/sh\necho real\n' > "$work/src/tool"
chmod +x "$work/src/tool"
tar -czf "$work/www/tool.tgz" -C "$work/src" tool
good="$(sha256sum "$work/www/tool.tgz" | cut -d' ' -f1)"
bad="$(printf x | sha256sum | cut -d' ' -f1)"

# Server: answers 504 to the first FAILS requests of the file, then serves it; counts requests.
cat > "$work/server.py" <<'PY'
import http.server, sys
fails = int(sys.argv[2])
drop = len(sys.argv) > 5 and sys.argv[5] == "drop"
hits = 0
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global hits
        hits += 1
        open(sys.argv[3], "w").write(str(hits))
        if hits <= fails and drop:
            self.connection.close(); return  # no response at all: curl error 52, retried only with --retry-all-errors
        if hits <= fails:
            self.send_response(504); self.end_headers(); return
        data = open(sys.argv[1] + "/tool.tgz", "rb").read()
        self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)
    def log_message(self, *a): pass
s = http.server.HTTPServer(("127.0.0.1", 0), H)
open(sys.argv[4], "w").write(str(s.server_port))
s.serve_forever()
PY

start_server() { # start_server <failures before the first 200> [drop: close the connection instead of 504]
  rm -f "$work/port" "$work/hits"
  python3 -I "$work/server.py" "$work/www" "$1" "$work/hits" "$work/port" "${2:-}" &
  srv=$!
  for _ in $(seq 50); do [ -s "$work/port" ] && break; sleep 0.1; done
  [ -s "$work/port" ] || { echo "server did not start"; exit 1; }
  url="http://127.0.0.1:$(cat "$work/port")/tool.tgz"
}
stop_server() { kill "$srv" 2>/dev/null || true; wait "$srv" 2>/dev/null || true; srv=""; }
hits() { cat "$work/hits" 2>/dev/null || echo 0; }

# run <case> <expected exit, or "fail" for any nonzero> <sha> <cache> <dest>
run() {
  local name="$1" want="$2" sha="$3" cache="$4" dest="$5" got=0
  CURL_RETRY_DELAY=1 "$install" tool "$sha" "$url" "$cache" "$dest" > "$work/out" 2>&1 || got=$?
  if { [ "$want" = fail ] && [ "$got" -ne 0 ]; } || [ "$got" = "$want" ]; then report ok "$name"; else report FAIL "$name: exit $got, wanted $want"; sed 's/^/    /' "$work/out"; fi
}

# 1. Cold cache, the first two attempts answer 504: still installed and verified.
start_server 2
run "504 twice, then installed" 0 "$good" "$work/c1" "$work/d1"
if [ -x "$work/d1/tool" ] && [ "$(hits)" -eq 3 ]; then report ok "retried 3 times, binary in place"; else report FAIL "binary missing or hits=$(hits)"; fi
if [ -f "$work/c1/tool.tgz" ]; then report ok "tarball cached"; else report FAIL "tarball not cached"; fi
# 2. Warm cache: no request is made.
before="$(hits)"
run "cache hit" 0 "$good" "$work/c1" "$work/d2"
if [ "$(hits)" -eq "$before" ] && [ -x "$work/d2/tool" ]; then report ok "cache hit skips the download"; else report FAIL "cache hit downloaded"; fi
# 3. Tampered cached tarball: fails, nothing unpacked, and no request is made (no silent re-download).
before="$(hits)"
printf 'tampered' >> "$work/c1/tool.tgz"
run "tampered cache fails" 1 "$good" "$work/c1" "$work/d3"
if grep -q 'does not match' "$work/out" && [ ! -e "$work/d3/tool" ] && [ "$(hits)" -eq "$before" ]; then report ok "tampered cache: error shown, nothing unpacked, no re-download"; else report FAIL "tampered cache handling"; fi
# 4. Wrong pinned checksum on a fresh download: fails.
run "checksum mismatch on download fails" 1 "$bad" "$work/c4" "$work/d4"
if [ ! -e "$work/d4/tool" ] && [ ! -e "$work/c4/tool.tgz" ]; then report ok "bad download not kept or unpacked"; else report FAIL "bad download kept"; fi
stop_server

# 5. A server that never recovers: the download ends in an error, no tarball left behind.
start_server 1000
run "persistent 504 fails" fail "$good" "$work/c5" "$work/d5"
if [ ! -e "$work/c5/tool.tgz" ]; then report ok "no partial tarball promoted"; else report FAIL "partial tarball promoted"; fi
if [ ! -e "$work/c5/tool.tgz.part" ] || [ ! -s "$work/c5/tool.tgz.part" ]; then report ok "no downloaded bytes left as a final or partial file"; else report FAIL "partial bytes left behind"; fi
if [ ! -e "$work/d5/tool" ]; then report ok "nothing unpacked after a failed download"; else report FAIL "unpacked after a failed download"; fi
stop_server

# 6. The connection is closed without any answer twice (curl error 52, which plain --retry does
# not retry, only --retry-all-errors): still installed and verified.
start_server 2 drop
run "dropped connection twice, then installed" 0 "$good" "$work/c6" "$work/d6"
if [ -x "$work/d6/tool" ] && [ "$(hits)" -eq 3 ]; then report ok "dropped connections retried, binary in place"; else report FAIL "drop case: binary missing or hits=$(hits)"; fi
stop_server

[ "$failures" -eq 0 ] || { echo "$failures failure(s)"; exit 1; }
echo "all ok"
