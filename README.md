<div align="center">

# Visual Office for Hermes

**ห้องทำงานพิกเซลที่รู้ว่าตัวละครแต่ละตัวใช้โมเดลของใคร — และให้คุณสั่งได้ว่าโต๊ะไหนใช้โมเดลอะไร**

ทุก session และทุก subagent ของ [Hermes Agent](https://github.com/NousResearch/hermes-agent)
กลายเป็นตัวละครนั่งโต๊ะ · แต่ละโต๊ะผูกกับโมเดลของตัวเอง
งานที่ส่งไปโต๊ะไหน ก็วิ่งด้วยโมเดลของโต๊ะนั้น

**[สถาปัตยกรรม](docs/ARCHITECTURE.md)** · **[รายชื่อโต๊ะ](docs/DESKS.md)** · **[LiteGate — ประตูเดียวหน้า GPU](https://github.com/neronain/AiGatewayLocal)**

สร้างและดูแลโดย **neronain** — [facebook.com/neronain.minidev](https://www.facebook.com/neronain.minidev)

MIT — fork และต่อยอดเชิงพาณิชย์ได้ แต่ประกาศลิขสิทธิ์ต้องอยู่ · ดู [LICENSE](LICENSE) และ [NOTICE](NOTICE)

</div>

---

## ปัญหาที่ตัวนี้แก้

มีคนทำห้องพิกเซลให้ Hermes อยู่แล้ว — [`teknium1/hermes-pixel-office`](https://github.com/teknium1/hermes-pixel-office)
และ [`aiunlocked1412/hermes-agent-pixel`](https://github.com/aiunlocked1412/hermes-agent-pixel)
ทั้งสองตัวทำงานได้ดีในสิ่งที่ตั้งใจ แต่ทั้งสองตัวมองไม่เห็นสิ่งเดียวกัน:

> **ตัวละครทุกตัวหน้าเหมือนกันหมด** ทั้งที่ตัวหนึ่งอาจกำลังเผาเงินคลาวด์
> อีกตัวใช้ GPU ในตึกที่จ่ายไปแล้ว · และ **คุณเลือกไม่ได้ว่าลูกน้องตัวไหนใช้สมองตัวไหน**
> เพราะ `delegation.model` ใน Hermes เป็นค่าเดียวสำหรับลูกทุกตัว

ตัวนี้เติมสองอย่างนั้น

| | |
|---|---|
| 🪑 **โต๊ะผูกกับโมเดล** | เครื่องมือ `office_delegate` ส่งงานไป *ชื่อโต๊ะ* ไม่ใช่ลูกน้องนิรนาม · ลูกที่เกิดมาถูกปักหมุดไว้กับโมเดลของโต๊ะนั้น ผ่าน subagent lifecycle API ที่ Hermes เปิดไว้ให้ปลั๊กอินอย่างเป็นทางการ |
| 📊 **มิติโมเดลบนตัวละคร** | hook `post_api_request` ให้ทั้ง `model`, `provider`, `base_url` และ `usage` มาทุกครั้งที่เรียก · ตัวละครจึงมีป้ายชื่อโมเดล ปลอกคอบอกว่ามาจากเครื่องเราหรือคลาวด์ และตัวนับโทเคนรายโต๊ะ |
| 🧩 **ไม่แตะ Hermes** | เป็นปลั๊กอินล้วน ๆ ไม่มีการแก้โค้ดต้นทาง · hook ทุกตัวคืน `None` การส่งอีเวนต์เป็นแบบยิงแล้วลืม · เซิร์ฟเวอร์ล่มแล้ว Hermes ไม่รู้สึกอะไร |
| 🪶 **ไม่มี dependency** | ฝั่งเซิร์ฟเวอร์ใช้ python มาตรฐานอย่างเดียว ฝั่งหน้าเว็บเป็น canvas + JS ล้วน · ไม่มี npm ไม่มี build step ก๊อปไปเครื่องไหนก็รันได้ |

---

## ภาพรวม

```
Hermes Agent                              Visual Office server
  ปลั๊กอิน visual_office                     :8130
  ├── hooks ──── POST /api/events ────▶     ├── fold เป็นสถานะห้อง
  │   (model, provider, usage)              ├── GET /api/state
  │                                         ├── GET /api/stream (SSE)
  └── tool: office_delegate(desk) ──┐       └── หน้า canvas
          │                          │
          ▼                          └────▶ desk_assign {subagent_id, desk, model}
      ctx.subagent_lifecycle.launch(
          SubagentLaunchRequest(model="<โมเดลของโต๊ะ>"))
          │
          ▼
      ลูกที่ปักหมุดโมเดลไว้แล้ว ── เรียกผ่าน base_url ที่สืบทอดจากตัวแม่
                                          │
                                          ▼
                                 LiteGate :8080  ──▶ Claude · Gemini · MiniMax · GPU ของเรา
```

**เรื่องเดียวที่ต้องเข้าใจก่อนตั้งค่า:** ลูกน้องรับได้แค่ *ชื่อโมเดล* · ปลายทางจริง
(base_url, คีย์) สืบทอดจากตัวแม่เสมอ · ดังนั้น **ตัวแม่ต้องชี้ไปยังที่ที่เรียกได้ครบ
ทุกโมเดลในไฟล์โต๊ะ** ซึ่งก็คือหน้าที่ของ gateway · ชี้ Hermes ไป LiteGate แล้วใส่
alias ของ LiteGate ลงช่อง `model` ของแต่ละโต๊ะ — โต๊ะหนึ่งจะนั่งบนคลาวด์หรือบน
GPU ของเราก็ได้ โดย Hermes ไม่ต้องรู้ความต่าง

---

## ติดตั้ง

```bash
git clone https://github.com/neronain/Visual-Office-Hermes.git
cd Visual-Office-Hermes
./install.sh --start
```

ตัวติดตั้งจะ:

1. ตรวจว่ามี `python3` และ `hermes` แล้วบอกเวอร์ชัน
2. ก๊อปปลั๊กอินไป `~/.hermes/plugins/visual_office/` และตรวจไวยากรณ์
3. ก๊อปเซิร์ฟเวอร์ไป `~/.hermes/visual-office/server/`
4. วางตัวอย่างรายชื่อโต๊ะที่ `~/.hermes/visual-office/desks.yaml` (ถ้ามีอยู่แล้วจะไม่ทับ)
5. สั่ง `hermes plugins enable visual_office`
6. เขียน `run-office.sh` และ systemd `--user` unit ให้ (ถ้าเครื่องรองรับ)

รันซ้ำได้เสมอ — หลัง `git pull` สั่งใหม่ได้เลย ไฟล์ `desks.yaml` ของคุณไม่ถูกทับ

### เอาไปใช้กับเครื่องอื่น

```bash
# ห้องอยู่บนเครื่องเดียวกับ Hermes (ค่าตั้งต้น)
./install.sh

# ห้องขึ้นจอทีวี — เปิดให้เครื่องอื่นในวงเห็น
./install.sh --host 0.0.0.0 --port 8130 --advertise http://10.0.0.5:8130

# Hermes อยู่คนละเครื่องกับห้อง — ตั้ง env ฝั่ง Hermes แทนการค้นหาอัตโนมัติ
export VISUAL_OFFICE_URL=http://10.0.0.5:8130
export VISUAL_OFFICE_TOKEN=<token ที่เซิร์ฟเวอร์พิมพ์ตอนเริ่ม>
```

ค่าตั้งต้นผูกกับ `127.0.0.1` โดยตั้งใจ — เปิดเป็น `0.0.0.0` เมื่อคุณตัดสินใจแล้วว่า
ทุกคนที่เข้าถึงพอร์ตนี้ได้ อ่านชื่องานบนจอได้

ถอนออก: `./uninstall.sh` (เติม `--purge` ถ้าจะลบรายชื่อโต๊ะและ log ด้วย)

---

## ตั้งโต๊ะ

`~/.hermes/visual-office/desks.yaml` — ดูคำอธิบายเต็มที่ [docs/DESKS.md](docs/DESKS.md)

```yaml
version: 1
office:
  name: ห้องทำงาน EduLLM
gateway:
  base_url: http://192.168.139.140:8080/v1

desks:
  - id: coder
    label: ช่างโค้ด
    model: coder-next            # alias ของ LiteGate
    origin: local                # local = GPU เรา · cloud = จ่ายตามใช้
    note: โมเดลโค้ด 80B MoE
    toolsets: [file, terminal, web, todo]

  - id: vision
    label: คนดูรูป
    model: gemini-2.5-flash
    origin: cloud
    toolsets: [file, vision, web]
```

แก้แล้วต้องเริ่ม session ใหม่ — รายชื่อโต๊ะถูกอ่านตอนโหลดปลั๊กอิน

---

## ใช้งาน

เปิดห้อง แล้วสั่งงาน Hermes ตามปกติ ตัวละครจะเดินเข้ามาเอง · ถ้าจะเลือกโมเดลให้ลูกน้อง
ก็บอกโมเดลไปตรง ๆ:

```
ส่งงานตรวจโค้ดไปที่โต๊ะ reviewer แล้วส่งงานเขียนเทสต์ไปที่โต๊ะ coder
```

โมเดลจะเรียก `office_delegate` เอง หรือจะเรียกตรง ๆ ก็ได้:

| action | ทำอะไร |
|---|---|
| `spawn` (ค่าตั้งต้น) | `{"goal": "...", "desk": "coder"}` — ส่งงานไปโต๊ะ · ใส่ `"wait": false` ถ้าไม่อยากรอ |
| `list` | ดูโต๊ะทั้งหมดและลูกน้องที่ยังทำงานอยู่ |
| `status` | `{"action": "status", "subagent_id": "..."}` |
| `cancel` | หยุดลูกน้องกลางคัน |

---

## ทดสอบแล้วกับอะไรบ้าง

| | |
|---|---|
| Hermes Agent | v0.20.5 (2026.8.19) · ติดตั้งแบบ git |
| Python | 3.13 (เซิร์ฟเวอร์ใช้ stdlib ล้วน ควรได้ตั้งแต่ 3.9) |
| เครื่อง | Ubuntu questing บน OrbStack (arm64) |
| พิสูจน์แล้ว | ลูกสองตัวจากสองโต๊ะ ถือชื่อโมเดลคนละตัวจริง · โทเคนแยกรายโต๊ะถูกต้อง · การจับคู่ subagent↔โต๊ะทำงานได้ทั้งสองลำดับของอีเวนต์ |

> **ข้อควรระวัง** — การพิสูจน์ข้างบนยืนยันว่า *ชื่อโมเดลของลูกแต่ละตัวถูกแยกจริง*
> ส่วนการที่ชื่อนั้นจะพาไปถึง backend คนละตัว เป็นหน้าที่ของ gateway ที่ตัวแม่ชี้อยู่
> ถ้าตัวแม่ชี้ตรงไป llama.cpp ตัวเดียว ชื่อโมเดลจะถูกเมิน (llama.cpp ตอบด้วยตัวที่โหลดไว้เสมอ)
> — ต้องมี LiteGate หรือ gateway อื่นคั่นกลาง มิติโมเดลถึงจะเป็นการ *เลือกเครื่อง* จริง

---

## เครดิต

- [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) — ตัวเอเจนต์ และ subagent lifecycle API ที่ทำให้เรื่องนี้เป็นไปได้โดยไม่ต้อง fork
- [teknium1/hermes-pixel-office](https://github.com/teknium1/hermes-pixel-office) — ต้นแบบแนวคิด hook → log → หน้าเว็บ
- [aiunlocked1412/hermes-agent-pixel](https://github.com/aiunlocked1412/hermes-agent-pixel) — ต้นแบบการค้นหาเซิร์ฟเวอร์และคิวส่งอีเวนต์แบบยิงแล้วลืม
- [neronain/AiGatewayLocal](https://github.com/neronain/AiGatewayLocal) — LiteGate ประตูเดียวที่ทำให้ alias ของโต๊ะไปถึงหลายผู้ให้บริการได้
