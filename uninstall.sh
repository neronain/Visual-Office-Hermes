#!/usr/bin/env bash
# Remove Visual Office. The desk roster and the event log are left alone unless
# you ask for them to go — losing a roster you hand-tuned to an uninstall would
# be a bad trade.

set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PURGE="no"

while [ $# -gt 0 ]; do
  case "$1" in
    --hermes-home) HERMES_HOME="$2"; shift 2 ;;
    --purge)       PURGE="yes"; shift ;;
    -h|--help)
      echo "Usage: ./uninstall.sh [--hermes-home DIR] [--purge]"
      echo "  --purge  also delete desks.yaml, events.jsonl and the state directory"
      exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

STATE_DIR="$HERMES_HOME/visual-office"
say() { printf '  %s\n' "$*"; }

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user stop visual-office >/dev/null 2>&1 || true
  systemctl --user disable visual-office >/dev/null 2>&1 || true
  rm -f "$HOME/.config/systemd/user/visual-office.service"
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  say "หยุดและถอน systemd unit แล้ว (ถ้ามี)"
fi

if command -v hermes >/dev/null 2>&1; then
  hermes plugins disable visual_office >/dev/null 2>&1 && say "ปิดปลั๊กอินแล้ว" || true
fi

rm -rf "$HERMES_HOME/plugins/visual_office"
say "ลบปลั๊กอินแล้ว"

rm -rf "$STATE_DIR/server" "$STATE_DIR/run-office.sh" "$STATE_DIR/server.json"
say "ลบเซิร์ฟเวอร์แล้ว"

if [ "$PURGE" = "yes" ]; then
  rm -rf "$STATE_DIR"
  say "ลบ $STATE_DIR ทั้งหมด (รวมรายชื่อโต๊ะและ log)"
else
  say "เก็บไว้: $STATE_DIR/desks.yaml และ events.jsonl (ใช้ --purge ถ้าจะลบด้วย)"
fi
