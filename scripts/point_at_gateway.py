#!/usr/bin/env python3
"""ชี้ Hermes ไปยัง gateway เพื่อให้โมเดลของแต่ละโต๊ะเรียกถึงจริง

ลูกน้องรับได้แค่ชื่อโมเดล ปลายทางจริงสืบทอดจากตัวแม่ · สคริปต์นี้จึงตั้งตัวแม่ให้ชี้ไป
gateway ที่ผ่านไปได้ทุก alias ในไฟล์โต๊ะ แล้วเช็คให้ก่อนว่าคีย์ที่ให้มาเรียก alias
เหล่านั้นได้จริง — ตรวจก่อนแก้ ไม่ใช่แก้แล้วค่อยรู้ตอน subagent ตัวแรกล้ม

คีย์ไม่เคยถูกเขียนลง config.yaml · เก็บใน ~/.hermes/.env แล้วอ้างด้วย key_env
ตามกลไกของ Hermes เอง

ตัวอย่าง:
    export LITEGATE_API_KEY=lg_sk_xxx
    python3 scripts/point_at_gateway.py \\
        --gateway http://192.168.139.140:8080/v1 \\
        --model claude-sonnet-4.8

    # ดูว่าจะแก้อะไรบ้างโดยยังไม่แก้จริง
    python3 scripts/point_at_gateway.py --gateway ... --model ... --dry-run

    # คืนค่าเดิม
    python3 scripts/point_at_gateway.py --rollback
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def hermes_home() -> Path:
    home = os.environ.get("HERMES_HOME", "").strip()
    return Path(home) if home else Path.home() / ".hermes"


def reexec_with_yaml() -> None:
    """PyYAML มาพร้อม Hermes เสมอ แต่ python ของระบบอาจไม่มี — วิ่งด้วยตัวที่มี"""
    try:
        import yaml  # noqa: F401
        return
    except ImportError:
        pass
    if os.environ.get("_VO_REEXEC") == "1":
        sys.exit("ไม่พบ PyYAML ทั้งใน python ของระบบและใน venv ของ Hermes")
    for candidate in (
        hermes_home() / "hermes-agent" / "venv" / "bin" / "python3",
        hermes_home() / "hermes-agent" / "venv" / "bin" / "python",
    ):
        if candidate.is_file():
            env = dict(os.environ, _VO_REEXEC="1")
            os.execve(str(candidate), [str(candidate), __file__, *sys.argv[1:]], env)
    sys.exit("ไม่พบ PyYAML — ติดตั้งด้วย: python3 -m pip install pyyaml")


reexec_with_yaml()
import yaml  # noqa: E402


def key_env_for(base_url: str) -> str:
    """ชื่อ env var ที่ Hermes ใช้เก็บคีย์ของ custom endpoint

    ต้องตรงกับ hermes_cli.config.custom_endpoint_key_env — คีย์ผูกกับ host:port
    ไม่ใช่แค่ host เพื่อให้สองเซิร์ฟเวอร์บนเครื่องเดียวกันไม่ทับคีย์กัน
    """
    parsed = urllib.parse.urlparse(base_url)
    identity = parsed.hostname or ""
    if parsed.port:
        identity = f"{identity}_{parsed.port}"
    slug = re.sub(r"[^A-Z0-9]+", "_", identity.upper()).strip("_")
    return f"HERMES_CUSTOM_{slug}_API_KEY" if slug else "HERMES_CUSTOM_API_KEY"


def say(msg: str) -> None:
    print(f"  {msg}")


def warn(msg: str) -> None:
    print(f"  \033[33m! {msg}\033[0m")


def step(msg: str) -> None:
    print(f"\n\033[1m{msg}\033[0m")


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    sys.exit(f"\n\033[31m{msg}\033[0m")


# ---------------------------------------------------------------- desks


def read_desk_models(path: Path) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        warn(f"อ่าน {path} ไม่ได้: {exc}")
        return []
    out = []
    for raw in (data.get("desks") or []):
        if isinstance(raw, dict) and raw.get("id") and raw.get("model"):
            out.append((str(raw["id"]), str(raw["model"])))
    return out


# ---------------------------------------------------------------- gateway


def gateway_models(base_url: str, key: str, timeout: float = 10.0) -> list[str]:
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        die(f"gateway ตอบ {exc.code} ที่ {url}\n  {detail}")
    except Exception as exc:
        die(f"เรียก {url} ไม่ได้: {exc}")

    entries = body.get("data") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        entries = body.get("models") if isinstance(body, dict) else None
    ids = []
    for entry in entries or []:
        if isinstance(entry, dict):
            ident = entry.get("id") or entry.get("name") or entry.get("model")
            if ident:
                ids.append(str(ident))
        elif isinstance(entry, str):
            ids.append(entry)
    return ids


# ---------------------------------------------------------------- .env


ENV_LINE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)=")


def put_env(path: Path, name: str, value: str, dry_run: bool) -> str:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replaced = False
    for index, line in enumerate(lines):
        match = ENV_LINE.match(line)
        if match and match.group("name") == name:
            if line == f"{name}={value}":
                return "ตรงกับที่มีอยู่แล้ว"
            lines[index] = f"{name}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{name}={value}")
    if dry_run:
        return "จะเขียนใหม่" if replaced else "จะเพิ่มบรรทัดใหม่"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return "เขียนทับแล้ว" if replaced else "เพิ่มแล้ว"


# ---------------------------------------------------------------- config


def backup_name(config: Path) -> Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return config.with_name(f"{config.name}.vo-bak.{stamp}")


def latest_backup(config: Path) -> Path | None:
    backups = sorted(config.parent.glob(f"{config.name}.vo-bak.*"))
    return backups[-1] if backups else None


def upsert_provider(config: dict, name: str, base_url: str, key_env: str, model: str) -> str:
    providers = config.get("custom_providers")
    if not isinstance(providers, list):
        providers = []
        config["custom_providers"] = providers
    for entry in providers:
        if isinstance(entry, dict) and str(entry.get("name", "")).lower() == name.lower():
            entry.update({"base_url": base_url, "key_env": key_env, "model": model})
            entry.pop("api_key", None)
            return "อัปเดตรายการเดิม"
    providers.append(
        {"name": name, "base_url": base_url, "key_env": key_env, "model": model}
    )
    return "เพิ่มรายการใหม่"


# ---------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ชี้ Hermes ไปยัง gateway เพื่อให้โมเดลรายโต๊ะเรียกถึงจริง"
    )
    parser.add_argument("--gateway", default="", help="base URL ที่ลงท้ายด้วย /v1")
    parser.add_argument("--model", default="", help="โมเดลตั้งต้นของตัวแม่ (ควรเป็น alias หนึ่งในโต๊ะ)")
    parser.add_argument("--name", default="litegate", help="ชื่อ custom provider ใน config (default: litegate)")
    parser.add_argument(
        "--key-env",
        default="",
        help="ชื่อ env var ที่เก็บคีย์ (ค่าตั้งต้น: ชื่อที่ Hermes สร้างจาก host:port เอง)",
    )
    parser.add_argument("--key", default="", help="คีย์ (ถ้าไม่ใส่ จะอ่านจาก env ตาม --key-env)")
    parser.add_argument("--context-length", type=int, default=0, help="ตั้ง model.context_length ด้วย")
    parser.add_argument("--dry-run", action="store_true", help="แสดงว่าจะแก้อะไร โดยยังไม่แก้")
    parser.add_argument("--rollback", action="store_true", help="คืน config.yaml จาก backup ล่าสุดของสคริปต์นี้")
    parser.add_argument("--skip-verify", action="store_true", help="ข้ามการเรียก /v1/models")
    args = parser.parse_args()

    home = hermes_home()
    config_path = home / "config.yaml"
    if not config_path.exists():
        die(f"ไม่พบ {config_path} — Hermes ติดตั้งที่อื่นหรือเปล่า (ตั้ง HERMES_HOME ได้)")

    if args.rollback:
        step("คืนค่า config.yaml")
        backup = latest_backup(config_path)
        if backup is None:
            die("ไม่พบ backup ที่สคริปต์นี้เคยสร้างไว้")
        shutil.copy2(backup, config_path)
        say(f"คืนจาก {backup.name} แล้ว")
        say("เริ่ม session ใหม่เพื่อให้มีผล")
        return 0

    if not args.gateway or not args.model:
        die("ต้องระบุทั้ง --gateway และ --model (ดู --help)")

    gateway = args.gateway.rstrip("/")
    if not gateway.endswith("/v1"):
        warn(f"{gateway} ไม่ได้ลงท้ายด้วย /v1 — Hermes จะต่อ /chat/completions เอง ตรวจให้ชัวร์")

    derived_env = key_env_for(gateway)
    key_env = args.key_env.strip() or derived_env
    key = (
        args.key.strip()
        or os.environ.get(key_env, "").strip()
        or os.environ.get("LITEGATE_API_KEY", "").strip()
    )
    if not key:
        die(
            f"ไม่มีคีย์ — ตั้ง {key_env} หรือ LITEGATE_API_KEY ในสภาพแวดล้อม หรือส่ง --key มา\n"
            f"  เช่น: export LITEGATE_API_KEY=lg_sk_xxx"
        )
    if key_env != derived_env:
        warn(
            f"--key-env {key_env} ไม่ตรงกับชื่อที่ Hermes สร้างเอง ({derived_env}) — "
            "ตัวแม่จะหาคีย์ไม่เจอถ้าไม่ได้ตั้งชื่อนี้ด้วยเหตุผลเฉพาะ"
        )

    step("1/4  ตรวจว่าคีย์เรียกโมเดลของโต๊ะได้จริง")
    desks = read_desk_models(home / "visual-office" / "desks.yaml")
    if desks:
        say(f"รายชื่อโต๊ะ {len(desks)} ตัว: " + ", ".join(f"{d}→{m}" for d, m in desks))
    else:
        warn("ยังไม่มีรายชื่อโต๊ะ — ข้ามการเทียบ alias")

    if args.skip_verify:
        warn("ข้ามการตรวจตาม --skip-verify")
        available: list[str] = []
    else:
        available = gateway_models(gateway, key)
        say(f"gateway ตอบ {len(available)} โมเดล: " + ", ".join(sorted(available)[:12]))
        missing = [f"{d} ({m})" for d, m in desks if m not in available]
        if missing:
            warn("โต๊ะที่คีย์นี้เรียกไม่ได้ — ลูกจะล้มตอนเรียก ไม่ใช่ตอนสร้าง:")
            for item in missing:
                warn(f"    {item}")
        if args.model not in available:
            die(
                f"โมเดลตั้งต้น {args.model!r} ไม่อยู่ในรายการที่คีย์นี้เรียกได้ — "
                "ตั้งไปก็เปิด session ไม่ขึ้น"
            )
        say(f"โมเดลตั้งต้น {args.model} เรียกได้")

    step("2/4  เก็บคีย์ไว้นอก config.yaml")
    env_path = home / ".env"
    outcome = put_env(env_path, key_env, key, args.dry_run)
    say(f"{env_path} · {key_env} — {outcome}")

    step("3/4  แก้ config.yaml")
    raw = config_path.read_text(encoding="utf-8")
    config = yaml.safe_load(raw) or {}
    before = {
        "default": (config.get("model") or {}).get("default"),
        "base_url": (config.get("model") or {}).get("base_url"),
        "provider": (config.get("model") or {}).get("provider"),
    }

    model_block = config.setdefault("model", {})
    model_block["default"] = args.model
    model_block["provider"] = "custom"
    model_block["base_url"] = gateway
    # Hermes อ่านคีย์ของ custom endpoint จากช่องนี้ โดยอ้าง env var ไม่ใช่ค่าจริง —
    # เป็นเส้นเดียวกับที่ `hermes model` เขียนเอง (model_setup_flows.py)
    model_block["api_key"] = "${" + key_env + "}"
    if args.context_length:
        model_block["context_length"] = args.context_length
    provider_outcome = upsert_provider(config, args.name, gateway, key_env, args.model)

    say(f"model.default   {before['default']}  →  {args.model}")
    say(f"model.base_url  {before['base_url']}  →  {gateway}")
    say(f"model.provider  {before['provider']}  →  custom")
    say("model.api_key   → ${" + key_env + "}  (อ้างชื่อ env ไม่ใช่ค่าคีย์)")
    say(f"custom_providers[{args.name}] — {provider_outcome} (key_env={key_env})")

    if args.dry_run:
        step("จบแบบ --dry-run — ยังไม่มีไฟล์ไหนถูกแก้")
        return 0

    backup = backup_name(config_path)
    shutil.copy2(config_path, backup)
    say(f"backup ไว้ที่ {backup.name}")
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    say("เขียน config.yaml แล้ว")

    step("4/4  ต่อไปทำอะไร")
    say("เริ่ม session ใหม่ — ค่าที่แก้มีผลกับ session ถัดไป ไม่ใช่ตัวที่เปิดค้างอยู่")
    say("ลองสั่ง: hermes -z 'ใช้ office_delegate action=list' --yolo")
    say(f"ถ้าอยากคืนค่าเดิม: python3 {Path(__file__).name} --rollback")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(130)
