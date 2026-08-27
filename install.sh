#!/usr/bin/env bash
# Visual Office for Hermes — installer.
#
# Two halves get installed: the plugin, which must live where Hermes looks for
# plugins, and the server, which must live somewhere stable that survives this
# checkout being deleted. Everything else is a copy and a chmod.
#
# The script is deliberately re-runnable: run it again after `git pull` and it
# replaces both halves without touching your desks.yaml.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PREFIX=""
HOST="127.0.0.1"
PORT="8130"
ADVERTISE=""
INSTALL_SERVICE="auto"
FORCE_DESKS="no"
START_NOW="ask"

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

  --hermes-home DIR   Hermes state directory        (default: $HERMES_HOME or ~/.hermes)
  --prefix DIR        where the server is installed (default: <hermes-home>/visual-office/server)
  --host ADDR         office bind address           (default: 127.0.0.1)
  --port N            office port                   (default: 8130)
  --advertise URL     URL the plugin should post to when it differs from the bind address
  --service           always install the systemd --user unit
  --no-service        never install a service; write a run script instead
  --force-desks       overwrite an existing desks.yaml with the example
  --start             start the office when the install finishes
  --no-start          do not start it
  -h, --help          this text

Examples
  ./install.sh                                  # localhost office on 8130
  ./install.sh --host 0.0.0.0 --port 8130 \
               --advertise http://10.0.0.5:8130 # office on a wall screen
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --hermes-home) HERMES_HOME="$2"; shift 2 ;;
    --prefix)      PREFIX="$2"; shift 2 ;;
    --host)        HOST="$2"; shift 2 ;;
    --port)        PORT="$2"; shift 2 ;;
    --advertise)   ADVERTISE="$2"; shift 2 ;;
    --service)     INSTALL_SERVICE="yes"; shift ;;
    --no-service)  INSTALL_SERVICE="no"; shift ;;
    --force-desks) FORCE_DESKS="yes"; shift ;;
    --start)       START_NOW="yes"; shift ;;
    --no-start)    START_NOW="no"; shift ;;
    -h|--help)     usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

STATE_DIR="$HERMES_HOME/visual-office"
PREFIX="${PREFIX:-$STATE_DIR/server}"
PLUGIN_DIR="$HERMES_HOME/plugins/visual_office"

say()  { printf '  %s\n' "$*"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
warn() { printf '  \033[33m! %s\033[0m\n' "$*"; }
die()  { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight

step "1/6  ตรวจเครื่องก่อน"

PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || die "ไม่พบ python3 — ตัวเซิร์ฟเวอร์ใช้ python3 มาตรฐาน ไม่มี dependency อื่น"
PYVER="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$PYVER" in
  3.9|3.1[0-9]) : ;;
  *) warn "python3 เวอร์ชัน $PYVER — ทดสอบกับ 3.9 ขึ้นไป" ;;
esac
say "python3        $PYTHON ($PYVER)"

HERMES_BIN="$(command -v hermes || true)"
if [ -n "$HERMES_BIN" ]; then
  say "hermes         $HERMES_BIN"
  "$HERMES_BIN" --version 2>/dev/null | head -1 | sed 's/^/                 /' || true
else
  warn "ไม่พบคำสั่ง hermes ใน PATH — จะติดตั้งไฟล์ให้ แต่ต้องสั่ง enable ปลั๊กอินเอง"
fi

[ -d "$HERMES_HOME" ] || warn "ยังไม่มี $HERMES_HOME — จะสร้างให้"
say "hermes home    $HERMES_HOME"
say "server prefix  $PREFIX"

# ---------------------------------------------------------------- plugin

step "2/6  ติดตั้งปลั๊กอิน"

mkdir -p "$HERMES_HOME/plugins"
rm -rf "$PLUGIN_DIR"
cp -R "$REPO_DIR/hermes-plugin/visual_office" "$PLUGIN_DIR"
find "$PLUGIN_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
say "$PLUGIN_DIR"

"$PYTHON" - "$PLUGIN_DIR" <<'PY'
import ast, pathlib, sys
root = pathlib.Path(sys.argv[1])
bad = []
for path in sorted(root.glob("*.py")):
    try:
        ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        bad.append(f"{path.name}: {exc}")
if bad:
    print("  ! ไฟล์ปลั๊กอินมีปัญหา:\n    " + "\n    ".join(bad))
    raise SystemExit(1)
print("  ตรวจไวยากรณ์ python ผ่านทุกไฟล์")
PY

# ---------------------------------------------------------------- server

step "3/6  ติดตั้งเซิร์ฟเวอร์"

mkdir -p "$PREFIX"
rm -rf "$PREFIX/web"
cp "$REPO_DIR/server/visual_office.py" "$REPO_DIR/server/state.py" "$PREFIX/"
cp -R "$REPO_DIR/server/web" "$PREFIX/web"
chmod +x "$PREFIX/visual_office.py"
say "$PREFIX"

# ---------------------------------------------------------------- desks

step "4/6  รายชื่อโต๊ะ"

mkdir -p "$STATE_DIR"
DESKS="$STATE_DIR/desks.yaml"
if [ -f "$DESKS" ] && [ "$FORCE_DESKS" != "yes" ]; then
  say "มีอยู่แล้ว ไม่ทับ — $DESKS"
else
  cp "$REPO_DIR/config/desks.example.yaml" "$DESKS"
  say "คัดลอกตัวอย่างไปที่ $DESKS"
  warn "แก้ไฟล์นี้ให้ตรงกับ alias จริงของคุณก่อนใช้งาน"
fi

# ---------------------------------------------------------------- enable

step "5/6  เปิดใช้ปลั๊กอิน"

if [ -n "$HERMES_BIN" ]; then
  if "$HERMES_BIN" plugins enable visual_office >/dev/null 2>&1; then
    say "hermes plugins enable visual_office — เรียบร้อย"
  else
    warn "สั่ง enable อัตโนมัติไม่สำเร็จ · สั่งเองด้วย: hermes plugins enable visual_office"
  fi
  say "มีผลกับ session ถัดไป ไม่ใช่ session ที่เปิดค้างอยู่"
else
  warn "ข้ามไป — สั่งเองด้วย: hermes plugins enable visual_office"
fi

# ---------------------------------------------------------------- runner

step "6/6  วิธีรันเซิร์ฟเวอร์"

RUN_ARGS="--host $HOST --port $PORT"
[ -n "$ADVERTISE" ] && RUN_ARGS="$RUN_ARGS --advertise $ADVERTISE"

cat > "$STATE_DIR/run-office.sh" <<EOF
#!/usr/bin/env bash
# เขียนโดย install.sh ของ Visual-Office-Hermes — แก้ได้ตามใจ
exec "$PYTHON" "$PREFIX/visual_office.py" $RUN_ARGS "\$@"
EOF
chmod +x "$STATE_DIR/run-office.sh"
say "สคริปต์รัน    $STATE_DIR/run-office.sh"

SERVICE_INSTALLED="no"
if [ "$INSTALL_SERVICE" != "no" ] && command -v systemctl >/dev/null 2>&1; then
  UNIT_DIR="$HOME/.config/systemd/user"
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/visual-office.service" <<EOF
[Unit]
Description=Visual Office for Hermes
After=network.target

[Service]
Type=simple
ExecStart=$PYTHON $PREFIX/visual_office.py $RUN_ARGS --quiet
Environment=HERMES_HOME=$HERMES_HOME
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
  say "systemd unit  $UNIT_DIR/visual-office.service"
  if systemctl --user daemon-reload >/dev/null 2>&1; then
    SERVICE_INSTALLED="yes"
    say "systemctl --user daemon-reload — เรียบร้อย"
  else
    warn "systemctl --user ใช้ไม่ได้บนเครื่องนี้ (ต้อง enable-linger) — ใช้ run-office.sh แทน"
  fi
elif [ "$INSTALL_SERVICE" = "yes" ]; then
  warn "ขอ --service มาแต่ไม่พบ systemctl — ข้าม"
fi

# ---------------------------------------------------------------- finish

printf '\n\033[1mติดตั้งเสร็จ\033[0m\n'
say "รายชื่อโต๊ะ   $DESKS"
say "เปิดดูที่     ${ADVERTISE:-http://${HOST/0.0.0.0/127.0.0.1}:$PORT}"
if [ "$SERVICE_INSTALLED" = "yes" ]; then
  say "เริ่ม/หยุด    systemctl --user start|stop visual-office"
  say "เปิดทุกบูต    systemctl --user enable visual-office"
else
  say "เริ่ม         $STATE_DIR/run-office.sh"
fi

printf '\n'
say "ขั้นต่อไป: ให้ Hermes ชี้ไปยัง gateway ที่เรียก alias ในไฟล์โต๊ะได้ครบ"
say "  hermes model  (หรือแก้ model.base_url ใน $HERMES_HOME/config.yaml)"
say "แล้วเริ่ม session ใหม่และสั่งงานด้วยเครื่องมือ office_delegate"

if [ "$START_NOW" = "yes" ]; then
  step "กำลังเริ่มเซิร์ฟเวอร์"
  if [ "$SERVICE_INSTALLED" = "yes" ]; then
    systemctl --user restart visual-office && say "เริ่มแล้ว"
  else
    nohup "$STATE_DIR/run-office.sh" >"$STATE_DIR/office.log" 2>&1 &
    say "เริ่มแล้ว (log: $STATE_DIR/office.log)"
  fi
fi
