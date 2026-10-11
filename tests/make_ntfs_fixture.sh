#!/bin/sh
set -eu
OUT="$1"
MNT=$(mktemp -d)
rm -f "$OUT"
truncate -s 32M "$OUT"
mkntfs -F -Q -q -s 512 -c 4096 -L CLAUDETEST "$OUT"
ntfs-3g -o loop "$OUT" "$MNT"

LOG="$MNT/Users/tester/AppData/Local/Claude/logs"
CFG="$MNT/Users/tester/AppData/Roaming/Claude"
CLI="$MNT/Users/tester/.claude"
mkdir -p "$LOG" "$CFG" "$CLI"

cat > "$LOG/main.log" <<'EOF'
2026-09-23 13:03:58 [info] Starting Claude
2026-09-23 13:04:10 [info] Login-state transition: loggedOut: true -> false uuid: <none> -> 11111111-2222-4333-8444-555555555555
2026-09-23 13:20:00 [info] some unrelated line
2026-09-23 13:24:51 [info] beforeQuit
EOF
touch -d "2026-09-23 04:24:51 UTC" "$LOG/main.log"

cat > "$CFG/config.json" <<'EOF'
{"locale": "ko-KR", "userThemeMode": "system"}
EOF
touch -d "2026-09-23 04:24:50 UTC" "$CFG/config.json"

echo '{"theme":"dark"}' > "$CLI/settings.json"

sync
umount "$MNT"
rmdir "$MNT"
