"""The desk roster — the file that says which model sits at which desk.

A desk is the unit the whole project is built around: a name a human uses
("ช่างโค้ด"), bound to one model string. When a task is handed to a desk, the
child agent is pinned to that desk's model.

A desk reaches its model one of two ways.

Through the gateway (``base_url`` empty) it becomes a real Hermes subagent:
tools, its own session, the whole agent loop. Hermes' public subagent API takes
a model and nothing else — the child inherits the parent's provider and base
URL — so every gateway desk necessarily shares one endpoint, and a gateway
alias is what makes them land on different machines anyway.

With its own ``base_url`` it skips Hermes entirely and speaks OpenAI-compatible
HTTP straight to that endpoint. It answers questions and nothing more: no
tools, no session, no agent loop. What it buys is independence — that desk
keeps working when the gateway is down, which a gateway desk cannot do.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

ORIGINS = ("local", "cloud", "unknown")


@dataclasses.dataclass(frozen=True)
class Desk:
    id: str
    label: str
    model: str
    origin: str = "unknown"
    provider: str = ""
    note: str = ""
    toolsets: tuple[str, ...] = ()
    role: str = "leaf"
    # ว่าง = ผ่าน gateway (เป็น subagent เต็มรูป) · มีค่า = ยิงตรงไปที่ endpoint นี้เอง
    base_url: str = ""
    # ชื่อ env ที่เก็บคีย์ของ endpoint นั้น — ไม่เก็บตัวคีย์ลงไฟล์ roster
    api_key_env: str = ""

    @property
    def direct(self) -> bool:
        """โต๊ะนี้คุยกับ endpoint ของตัวเองโดยไม่ผ่าน gateway หรือไม่"""
        return bool(self.base_url)

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["toolsets"] = list(self.toolsets)
        data["direct"] = self.direct
        return data


@dataclasses.dataclass(frozen=True)
class Roster:
    office_name: str
    gateway_base_url: str
    desks: tuple[Desk, ...]
    source: str
    problems: tuple[str, ...] = ()
    # Which model the top-level agent runs on. Kept here so the office can show
    # and change it beside the desks — it is the same kind of decision.
    main_model: str = ""

    def find(self, desk_id: str) -> Optional[Desk]:
        wanted = (desk_id or "").strip().lower()
        for desk in self.desks:
            if desk.id.lower() == wanted:
                return desk
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "office_name": self.office_name,
            "gateway_base_url": self.gateway_base_url,
            "main_model": self.main_model,
            "desks": [d.to_dict() for d in self.desks],
            "source": self.source,
            "problems": list(self.problems),
        }


def desks_path() -> Path:
    """Where the roster lives — env override first, then the Hermes home."""
    override = os.environ.get("VISUAL_OFFICE_DESKS", "").strip()
    if override:
        return Path(override).expanduser()
    home = os.environ.get("HERMES_HOME", "").strip()
    base = Path(home) if home else Path.home() / ".hermes"
    return base / "visual-office" / "desks.yaml"


def _coerce_desk(raw: Any, index: int, problems: list[str]) -> Optional[Desk]:
    if not isinstance(raw, dict):
        problems.append(f"desks[{index}] is not a mapping — skipped")
        return None

    desk_id = str(raw.get("id") or "").strip()
    model = str(raw.get("model") or "").strip()
    if not desk_id:
        problems.append(f"desks[{index}] has no id — skipped")
        return None
    if not model:
        problems.append(f"desk {desk_id!r} has no model — skipped")
        return None

    origin = str(raw.get("origin") or "unknown").strip().lower()
    if origin not in ORIGINS:
        problems.append(
            f"desk {desk_id!r} has origin {origin!r}; expected one of {', '.join(ORIGINS)} — treated as unknown"
        )
        origin = "unknown"

    role = str(raw.get("role") or "leaf").strip().lower()
    if role not in ("leaf", "orchestrator"):
        problems.append(f"desk {desk_id!r} has role {role!r}; expected leaf or orchestrator — treated as leaf")
        role = "leaf"

    raw_toolsets = raw.get("toolsets") or ()
    if isinstance(raw_toolsets, str):
        raw_toolsets = [raw_toolsets]
    toolsets = tuple(str(t).strip() for t in raw_toolsets if str(t).strip())

    base_url = str(raw.get("base_url") or "").strip().rstrip("/")
    if base_url and not base_url.startswith(("http://", "https://")):
        problems.append(
            f"desk {desk_id!r} has base_url {base_url!r} without http:// or https:// — "
            "ignored, so this desk keeps going through the gateway"
        )
        base_url = ""

    # toolsets ไม่มีผลกับโต๊ะที่ยิงตรง — พูดออกมาดีกว่าปล่อยให้คิดว่าตั้งแล้วได้ผล
    if base_url and toolsets:
        problems.append(
            f"desk {desk_id!r} is a direct-endpoint desk; its toolsets are ignored "
            "(only gateway desks run as Hermes subagents)"
        )

    return Desk(
        id=desk_id,
        label=str(raw.get("label") or desk_id).strip(),
        model=model,
        origin=origin,
        provider=str(raw.get("provider") or "").strip(),
        note=str(raw.get("note") or "").strip(),
        toolsets=toolsets,
        role=role,
        base_url=base_url,
        api_key_env=str(raw.get("api_key_env") or "").strip(),
    )


def load_roster() -> Roster:
    """Read the desk roster. A missing or broken file yields an empty office."""
    path = desks_path()
    problems: list[str] = []

    if not path.exists():
        return Roster("", "", (), str(path), (f"no desk roster at {path}",))

    try:
        import yaml  # Hermes ships PyYAML; the office server never needs it.
    except ImportError:
        return Roster("", "", (), str(path), ("PyYAML is unavailable — desks cannot be read",))

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return Roster("", "", (), str(path), (f"could not parse {path}: {exc}",))

    if not isinstance(data, dict):
        return Roster("", "", (), str(path), (f"{path} is not a mapping",))

    office = data.get("office") if isinstance(data.get("office"), dict) else {}
    gateway = data.get("gateway") if isinstance(data.get("gateway"), dict) else {}

    desks: list[Desk] = []
    seen: set[str] = set()
    for index, raw in enumerate(data.get("desks") or ()):
        desk = _coerce_desk(raw, index, problems)
        if desk is None:
            continue
        if desk.id.lower() in seen:
            problems.append(f"duplicate desk id {desk.id!r} — later one skipped")
            continue
        seen.add(desk.id.lower())
        desks.append(desk)

    if not desks:
        problems.append("roster defines no usable desks")

    return Roster(
        office_name=str(office.get("name") or "Visual Office").strip(),
        gateway_base_url=str(gateway.get("base_url") or "").strip(),
        main_model=str(office.get("main_model") or "").strip(),
        desks=tuple(desks),
        source=str(path),
        problems=tuple(problems),
    )
