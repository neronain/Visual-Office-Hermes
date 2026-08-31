# สถาปัตยกรรม

เอกสารนี้อธิบายว่าของแต่ละชิ้นวางอยู่ตรงไหน ทำไมถึงวางอย่างนั้น และอะไรคือ
ข้อจำกัดที่กำหนดรูปร่างของทั้งระบบ

---

## 1. ข้อจำกัดที่กำหนดทุกอย่าง

Hermes เปิด API สาธารณะให้ปลั๊กอินสร้าง subagent เองที่
`agent/subagent_lifecycle.py` — เรียกผ่าน `PluginContext.subagent_lifecycle`
คำขอมีหน้าตาแบบนี้ (ตัดมาจากต้นทาง v0.20.5):

```python
@dataclasses.dataclass(frozen=True)
class SubagentLaunchRequest:
    goal: str
    context: Optional[str] = None
    role: str = "leaf"
    model: Optional[str] = None          # ← ช่องที่ทั้งโปรเจกต์นี้ยืนอยู่บนมัน
    allowed_toolsets: Optional[tuple[str, ...]] = None
    ...
```

มี `model` แต่ **ไม่มี** `provider` `base_url` หรือ `api_key` — ลูกได้ปลายทางจาก
ตัวแม่เสมอ (`_build_child_preserving_parent_tools` รับ `model=request.model` แล้ว
ที่เหลือสืบทอด)

ผลที่ตามมามีสองข้อ และทั้งสองข้อคือเหตุผลที่โปรเจกต์นี้หน้าตาเป็นแบบนี้:

1. **โต๊ะเก็บได้แค่ชื่อโมเดล** — ไฟล์ `desks.yaml` จึงไม่มีช่อง endpoint ให้กรอก
   ไม่ใช่เพราะขี้เกียจ แต่เพราะกรอกไปก็ไม่มีใครอ่าน
2. **ตัวแม่ต้องเป็นประตูที่ผ่านไปได้ทุกโมเดล** — ซึ่งคือนิยามของ gateway
   ชี้ `model.base_url` ของ Hermes ไป LiteGate แล้วชื่อโมเดลในโต๊ะก็คือ alias

ทางเลือกอื่นที่พิจารณาแล้วไม่เอา:

| ทางเลือก | ทำไมไม่เอา |
|---|---|
| แก้ `delegate_task` ในโค้ดต้นทาง | ต้อง fork Hermes ที่ออกเวอร์ชันถี่มาก · อัปเดตทีก็ merge ทีนึง |
| ใช้ `tools.override` ทับ `delegate_task` | ต้องเรียกฟังก์ชันภายในอย่าง `_build_child_preserving_parent_tools` เอง · ผูกกับโครงสร้างภายในที่ไม่มีสัญญาว่าจะไม่เปลี่ยน |
| ตั้ง `delegation.model` แล้วสลับไปมา | เป็นค่าเดียวทั้ง process · ลูกที่รันพร้อมกันจะแย่งกัน |
| ให้ปลั๊กอินเป็น model provider | แก้ปัญหาผิดข้อ — มันเปลี่ยนวิธีคุยกับโมเดล ไม่ได้เปลี่ยนว่าลูกตัวไหนได้โมเดลอะไร |

API สาธารณะประกาศ `PUBLIC_CONTRACT_VERSION = 1` และเอกสารในไฟล์บอกเองว่า
"the supported boundary for plugins that need to supervise fresh child sessions"
— เป็นทางที่ตั้งใจเปิดไว้ให้ ไม่ใช่ช่องโหว่ที่เราไปงัด

---

## 2. สองครึ่งที่แยกกันชัด

### ครึ่งที่ดู — hook → อีเวนต์ → สถานะ → หน้าจอ

```
hook ของ Hermes            คิวในปลั๊กอิน            เซิร์ฟเวอร์
─────────────────          ──────────────          ───────────────────────
on_session_start   ┐                               POST /api/events
pre_llm_call       │                                 │
pre_tool_call      ├──▶ Sink.emit() ──▶ เธรดเดียว ──▶ Office.apply()
post_tool_call     │    (ไม่บล็อก)      ยิง FIFO       │  พับเป็นสถานะห้อง
post_api_request   │                                 ├──▶ events.jsonl
subagent_start     │                                 └──▶ /api/state · /api/stream
subagent_stop      ┘
```

`post_api_request` คือ hook ที่ทำให้โปรเจกต์นี้ต่างจากตัวอื่น · ที่จุดเรียกจริงใน
`agent/conversation_loop.py` มันส่งมาให้ครบ:

```python
_invoke_hook(
    "post_api_request",
    session_id=..., platform=..., task_id=...,
    model=agent.model,          # ← ชื่อโมเดลที่ใช้จริงในรอบนั้น
    provider=agent.provider,
    base_url=agent.base_url,
    api_mode=agent.api_mode,
    api_duration=..., finish_reason=...,
    usage=agent._usage_summary_for_api_request_hook(response),
    ...
)
```

ห้องจึงบอกได้ว่าตัวละครตัวไหนใช้โมเดลอะไร ผ่านทางไหน และกินโทเคนไปเท่าไร
โดยไม่ต้องเดาและไม่ต้องถามใครเพิ่ม

### ครึ่งที่สั่ง — เครื่องมือ `office_delegate`

```python
request = SubagentLaunchRequest(
    goal=goal,
    context=context,
    role=desk.role,
    model=desk.model,                       # ← โมเดลของโต๊ะ
    allowed_toolsets=tuple(desk.toolsets) or None,
    metadata={"desk": desk.id, "origin": desk.origin},
)
handle = ctx.subagent_lifecycle.launch(request)
```

`handle` มี `subagent_id`, `model`, `provider` กลับมา · ปลั๊กอินยิงอีเวนต์
`desk_assign` ที่ผูก `subagent_id` เข้ากับโต๊ะ

เครื่องมือนี้ลงทะเบียนใน toolset **`delegation`** ไม่ใช่ toolset ใหม่ — เพราะมันคือ
การ delegate จริง ๆ และ toolset นั้นเปิดอยู่แล้วทุกที่ที่ `delegate_task` เปิด
ติดตั้งปลั๊กอินจึงไม่ต้องไปแก้ `platform_toolsets` ตามอีกที่

---

## 3. การจับคู่ subagent เข้ากับโต๊ะ

อีเวนต์สองตัวมาถึงโดย **ไม่รับประกันลำดับ**: `subagent_start` ยิงจากข้างใน
`launch()` (ตอนสร้างลูก) ส่วน `desk_assign` ยิงหลัง `launch()` คืนค่า · ในทางปฏิบัติ
`subagent_start` มักมาก่อน

```
desk_assign   { subagent_id, desk, model, origin }
subagent_start{ child_subagent_id, child_session_id, goal }
                       │                    │
                       └── join ────────────┘
                                            ▼
api_request   { session_id = child_session_id, model, usage }   → บวกโทเคนเข้าโต๊ะ
subagent_stop { child_session_id, status, duration_ms }         → ตัวละครเดินออก
```

`state.py` จึงเก็บสองแมป: `subagent_sessions` (subagent_id → session_id) และ
`pending_desks` (การมอบหมายที่มาถึงก่อนตัวละครเกิด) · ตัวไหนมาก่อนก็ได้ ผลลัพธ์เท่ากัน

หมายเหตุ: `subagent_stop` ไม่มี `child_subagent_id` มาด้วย มีแต่ `child_session_id`
— นี่คือเหตุผลที่ต้องจำ `subagent_sessions` ไว้ ไม่ใช่ทิ้งหลังจับคู่เสร็จ

---

## 4. ทำไมเซิร์ฟเวอร์ถึงไม่มี dependency

ปลั๊กอินต้องรันใน virtualenv ของ Hermes (แตะไม่ได้) ส่วนเซิร์ฟเวอร์ต้องรันบนเครื่อง
ที่มีจอ (อาจเป็นคนละเครื่อง) · ถ้าฝั่งใดฝั่งหนึ่งต้อง `npm install` หรือ `pip install`
การติดตั้งก็จะยากกว่าตัวงานเอง

- ปลั๊กอิน: `json`, `queue`, `threading`, `urllib` — และ `yaml` ซึ่ง Hermes มีอยู่แล้ว
- เซิร์ฟเวอร์: `http.server`, `json`, `secrets` — ไม่มีอะไรนอกนั้น
- หน้าเว็บ: canvas + JS ล้วน ไม่มี framework ไม่มี build step

รายชื่อโต๊ะถูกอ่านโดย **ปลั๊กอิน** แล้วประกาศให้เซิร์ฟเวอร์ผ่านอีเวนต์ `roster`
เซิร์ฟเวอร์จึงไม่ต้องอ่าน YAML เอง — และหน้าจอแสดง "รายชื่อโต๊ะที่ปลั๊กอินใช้จริง"
ไม่ใช่ "ไฟล์ที่เผอิญวางอยู่ข้าง ๆ"

---

## 5. อะไรที่ตัวนี้ทำไม่ได้ (ตั้งใจ)

- **บล็อกอะไรไม่ได้** — hook แบบ directive (`pre_tool_call`, `pre_llm_call`) คืน `None`
  เสมอ · ห้องนี้ดูอย่างเดียว
- **ไม่เก็บเนื้อหา** — อีเวนต์มีชื่อเครื่องมือ ชื่อโมเดล จำนวนโทเคน และเป้าหมายของ
  subagent · ไม่มี prompt ไม่มีคำตอบ ไม่มีผลลัพธ์เครื่องมือ
- **ไม่ข้ามเครื่องเอง** — ห้องหนึ่งเห็นทุก process ของ Hermes บนเครื่องเดียวกัน
  จะรวมหลายเครื่องต้องตั้ง `VISUAL_OFFICE_URL` ให้ทุกเครื่องชี้มาที่ห้องเดียวกัน
- **ไม่มีสิทธิ์อ่านแยกคน** — อ่านได้ทุกคนที่เข้าถึงพอร์ต · เขียนต้องมี token
  ค่าตั้งต้นผูก `127.0.0.1` ไว้ก่อนด้วยเหตุผลนี้

---

## 6. ไฟล์ไหนทำอะไร

```
hermes-plugin/visual_office/
  plugin.yaml     manifest + รายการ hook
  __init__.py     register() · hook ทุกตัว · เครื่องมือ office_delegate
  desks.py        อ่านและตรวจ desks.yaml เป็น Roster/Desk
  sink.py         คิวยิงอีเวนต์แบบไม่บล็อก + ค้นหาเซิร์ฟเวอร์

server/
  visual_office.py  HTTP server · ingest · SSE · เสิร์ฟไฟล์ static · เขียน server.json
  state.py          Office — พับอีเวนต์เป็นสถานะ · รวมยอดรายโต๊ะ/รายโมเดล/รายต้นทาง
  web/              index.html · office.css · office.js (canvas)

config/desks.example.yaml   ตัวอย่างที่ตัวติดตั้งคัดลอกไป
install.sh / uninstall.sh   ติดตั้งแบบรันซ้ำได้
```

---

## เก็บข้อเท็จจริง คำนวณสิ่งที่เห็น

`state.py` เก็บลงหน่วยความจำเฉพาะสิ่งที่ **เป็นจริงและยังจริงอยู่** — เข้ามาเมื่อไร
กำลังคิดอยู่ไหม เปิดเครื่องมือตัวไหนค้างไว้ มีอะไรรออนุมัติไหม จบไปเมื่อไร

**ไม่มีฟิลด์ `activity` เก็บไว้เลย** สิ่งที่ตัวละครกำลังทำถูกคำนวณตอนอ่านโดย `_display()`
ตามลำดับเดียวที่เขียนไว้ในฟังก์ชันนั้น:

| ถ้า | แสดงเป็น | ทำไมมาก่อนข้อถัดไป |
|---|---|---|
| จบแล้ว | `leaving` | กำลังจะออกจากห้อง อย่างอื่นไม่สำคัญแล้ว |
| มีอะไรรออนุมัติ | `waiting` | สถานะเดียวที่ไม่มีอะไรเดินต่อจนกว่าคนจะลงมือ |
| มีเครื่องมือเปิดค้าง | ตามเครื่องมือ | เจาะจงที่สุดเท่าที่เรารู้ |
| กำลังคิด | `thinking` | ทำงานอยู่ แค่ไม่เห็นว่าทำอะไร |
| เพิ่งเข้ามาและยังไม่มีอะไรเกิด | `arriving` | ยังเดินเข้ามาไม่ถึงที่ |
| เงียบเกิน 90 วินาที | `idle` | นานพอจะเชื่อว่าไม่ได้ทำอะไรแล้ว |
| นอกนั้น | `working` | เพิ่งเห็นความเคลื่อนไหว แต่ไม่รู้รายละเอียด |

### ทำไมต้องทำแบบนี้

รุ่นแรกแก้ `activity` ตรง ๆ ทุกครั้งที่อีเวนต์เข้ามา ผลคือสิ่งที่เห็นขึ้นอยู่กับ**ลำดับ**
ที่อีเวนต์มาถึงและ**การไม่พลาดอีเวนต์ไหนเลย** ซึ่งเป็นเงื่อนไขที่รักษาไม่ได้จริง
บั๊กสามตัวในสัปดาห์เดียวมาจากตรงนี้ทั้งหมด:

- ตัวละครเดินออกจากห้องทุกครั้งที่ตอบเสร็จ (เพราะ `session_end` ถูกตีความเป็นการจากไป
  ทั้งที่ Hermes ยิงมันตอนจบ *ทุก turn*)
- session ที่กลับมาด้วย id เดิมหลัง restart ค้างสถานะ "ออกไปแล้ว"
- โต๊ะที่ถูกลบจากรายชื่อยังมีคนนั่งอยู่

**ทั้งสามอย่างไปไม่ถึงโค้ดชุดใหม่ได้เลย** เพราะไม่มีฟิลด์ให้ค้าง — มันไม่มีอยู่จนกว่า
จะมีคนถาม · ตัวละครกลายเป็น `idle` เพราะ*เวลาผ่านไป* ไม่ใช่เพราะมีอีเวนต์มาบอก
(วัดจากของจริง: เงียบไป 141 วินาที แล้วสถานะเปลี่ยนเองโดยไม่มีอีเวนต์ใหม่เข้ามาเลย)

แนวคิดนี้ยืมมาจาก [agent-orchestrator](https://github.com/Untrivial-ai/agent-orchestrator)
ที่แยก "durable facts" ออกจาก "display status computed at read time" ชัดเจน
— คนละภาษา คนละโดเมน แต่ปัญหาเดียวกัน
