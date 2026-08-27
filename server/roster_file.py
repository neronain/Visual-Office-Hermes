"""Validate and write the desk roster.

The office server has no YAML library — it is stdlib-only on purpose — but the
roster is a fixed, shallow shape: a couple of strings and a list of desks whose
fields are all scalars or a list of scalars. Emitting that safely is a dozen
lines; pulling in a parser to do it would cost more than it is worth.

Reading is not done here. The plugin owns reading, because the plugin is what
Hermes actually runs on, and a second reader would be a second interpretation of
the same file waiting to drift.
"""

from __future__ import annotations

import re
from typing import Any

ORIGINS = ("local", "cloud", "unknown")
ROLES = ("leaf", "orchestrator")

DESK_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
TOOLSET = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

MAX_DESKS = 12
LIMITS = {"label": 64, "model": 200, "note": 200, "provider": 64,
          "office_name": 64, "gateway": 200}


class RosterError(ValueError):
    """A roster the office refuses to write, with a reason a person can act on."""


def _text(value: Any, field: str, limit: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise RosterError(f"{field} ต้องเป็นข้อความ")
    value = value.strip()
    if required and not value:
        raise RosterError(f"{field} ห้ามว่าง")
    if len(value) > limit:
        raise RosterError(f"{field} ยาวเกิน {limit} ตัวอักษร")
    if "\n" in value or "\r" in value:
        raise RosterError(f"{field} ขึ้นบรรทัดใหม่ไม่ได้")
    return value


def normalize(payload: Any) -> dict[str, Any]:
    """Return a roster ready to write, or raise RosterError saying what is wrong."""
    if not isinstance(payload, dict):
        raise RosterError("ข้อมูลที่ส่งมาต้องเป็นออบเจกต์")

    office = payload.get("office") if isinstance(payload.get("office"), dict) else {}
    gateway = payload.get("gateway") if isinstance(payload.get("gateway"), dict) else {}
    raw_desks = payload.get("desks")
    if not isinstance(raw_desks, list) or not raw_desks:
        raise RosterError("ต้องมีโต๊ะอย่างน้อยหนึ่งตัว")
    if len(raw_desks) > MAX_DESKS:
        raise RosterError(f"มีโต๊ะได้มากที่สุด {MAX_DESKS} ตัว")

    desks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_desks, start=1):
        if not isinstance(raw, dict):
            raise RosterError(f"โต๊ะลำดับที่ {index} ไม่ใช่ออบเจกต์")

        desk_id = _text(raw.get("id"), f"โต๊ะลำดับที่ {index}: id", 32, required=True).lower()
        if not DESK_ID.match(desk_id):
            raise RosterError(
                f"id {desk_id!r} ใช้ไม่ได้ — ใช้ได้เฉพาะ a-z 0-9 _ - และต้องขึ้นต้นด้วยตัวอักษรหรือตัวเลข"
            )
        if desk_id in seen:
            raise RosterError(f"id {desk_id!r} ซ้ำกับโต๊ะอื่น")
        seen.add(desk_id)

        origin = _text(raw.get("origin"), f"โต๊ะ {desk_id}: origin", 16) or "unknown"
        if origin not in ORIGINS:
            raise RosterError(f"โต๊ะ {desk_id}: origin ต้องเป็น {' / '.join(ORIGINS)}")

        role = _text(raw.get("role"), f"โต๊ะ {desk_id}: role", 16) or "leaf"
        if role not in ROLES:
            raise RosterError(f"โต๊ะ {desk_id}: role ต้องเป็น {' / '.join(ROLES)}")

        raw_toolsets = raw.get("toolsets") or []
        if isinstance(raw_toolsets, str):
            raw_toolsets = [part.strip() for part in raw_toolsets.split(",")]
        if not isinstance(raw_toolsets, list):
            raise RosterError(f"โต๊ะ {desk_id}: toolsets ต้องเป็นรายการ")
        toolsets: list[str] = []
        for item in raw_toolsets:
            name = str(item).strip().lower()
            if not name:
                continue
            if not TOOLSET.match(name):
                raise RosterError(f"โต๊ะ {desk_id}: toolset {name!r} ใช้ไม่ได้")
            if name not in toolsets:
                toolsets.append(name)
        if len(toolsets) > 12:
            raise RosterError(f"โต๊ะ {desk_id}: toolsets มากเกินไป")

        desks.append({
            "id": desk_id,
            "label": _text(raw.get("label"), f"โต๊ะ {desk_id}: label", LIMITS["label"]) or desk_id,
            "model": _text(raw.get("model"), f"โต๊ะ {desk_id}: model", LIMITS["model"], required=True),
            "origin": origin,
            "provider": _text(raw.get("provider"), f"โต๊ะ {desk_id}: provider", LIMITS["provider"]),
            "note": _text(raw.get("note"), f"โต๊ะ {desk_id}: note", LIMITS["note"]),
            "toolsets": toolsets,
            "role": role,
        })

    return {
        "office_name": _text(office.get("name"), "ชื่อห้อง", LIMITS["office_name"]) or "Visual Office",
        "main_model": _text(office.get("main_model"), "โมเดลหลัก", LIMITS["model"]),
        "gateway_base_url": _text(gateway.get("base_url"), "gateway base_url", LIMITS["gateway"]),
        "desks": desks,
    }


def _scalar(value: str) -> str:
    """A double-quoted YAML scalar — safe for Thai, colons, hashes and dashes."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def dump(roster: dict[str, Any], stamp: str) -> str:
    """Render a normalized roster as YAML the plugin can read back."""
    lines = [
        "# รายชื่อโต๊ะ — เขียนโดยหน้าจัดการโต๊ะของ Visual Office",
        f"# {stamp}",
        "#",
        "# แก้ด้วยมือได้ตามปกติ · แต่ถ้ากดบันทึกจากหน้าเว็บอีกครั้ง ไฟล์นี้จะถูกเขียนใหม่ทั้งไฟล์",
        "# และคอมเมนต์ที่เพิ่มเองจะหายไป · ไฟล์ก่อนหน้าถูกสำรองไว้เป็น desks.yaml.bak",
        "",
        "version: 1",
        "",
        "office:",
        f"  name: {_scalar(roster['office_name'])}",
        # Which model the top-level agent runs on. The plugin follows this into
        # ~/.hermes/config.yaml, so both decisions live in one file.
        f"  main_model: {_scalar(roster['main_model'])}",
        "",
        "gateway:",
        f"  base_url: {_scalar(roster['gateway_base_url'])}",
        "",
        "desks:",
    ]
    for desk in roster["desks"]:
        lines.append(f"  - id: {_scalar(desk['id'])}")
        lines.append(f"    label: {_scalar(desk['label'])}")
        lines.append(f"    model: {_scalar(desk['model'])}")
        lines.append(f"    origin: {_scalar(desk['origin'])}")
        if desk["provider"]:
            lines.append(f"    provider: {_scalar(desk['provider'])}")
        if desk["note"]:
            lines.append(f"    note: {_scalar(desk['note'])}")
        if desk["toolsets"]:
            joined = ", ".join(_scalar(t) for t in desk["toolsets"])
            lines.append(f"    toolsets: [{joined}]")
        if desk["role"] != "leaf":
            lines.append(f"    role: {_scalar(desk['role'])}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
