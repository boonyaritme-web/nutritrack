"""
NutriTrack — LINE Health Coach (Phase 2 backend skeleton)
=========================================================
โครง FastAPI สำหรับบอทไลน์: ผู้ใช้กดปุ่ม "สรุปประจำวัน" บน Rich Menu
→ ดึงข้อมูลร่างกาย + อาหารวันนี้จาก DB → เรียก Claude → ตอบสรุป+คำแนะนำในแชต
นอกจากนี้รองรับ: log อาหารด้วยข้อความ, คุยกับโค้ช, ต้อนรับผู้ใช้ใหม่

ติดตั้ง:
    pip install fastapi uvicorn line-bot-sdk
    # เลือกตามผู้ให้บริการที่ใช้:
    pip install google-genai      # ถ้าใช้ Gemini (เริ่มฟรี)
    pip install anthropic         # ถ้าใช้ Claude
รัน:
    uvicorn line_coach_backend:app --reload --port 8000
ตั้ง env:
    LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET
    LLM_PROVIDER = "gemini" หรือ "anthropic"  (default: gemini)
    GEMINI_API_KEY   (ถ้าใช้ gemini — ขอฟรีที่ aistudio.google.com)
    ANTHROPIC_API_KEY (ถ้าใช้ anthropic)
"""

import os, sqlite3, json
from datetime import datetime, date
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookParser
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, PostbackEvent, FollowEvent
)
from fastapi.middleware.cors import CORSMiddleware

# ---------- config ----------
line_bot_api = LineBotApi(os.environ["LINE_CHANNEL_ACCESS_TOKEN"])
parser = WebhookParser(os.environ["LINE_CHANNEL_SECRET"])
DB = "nutritrack.db"

# ---------- LLM provider (สลับ Gemini/Claude ที่ตัวแปรเดียว) ----------
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini")   # "gemini" (เริ่มฟรี) หรือ "anthropic"
GEMINI_MODEL = "gemini-2.5-flash"   # free tier ใช้ได้; สลับเป็น gemini-3-flash ได้ (เช็คชื่อล่าสุดใน AI Studio)
CLAUDE_MODEL = "claude-haiku-4-5"   # ถูกสุดสำหรับงานนี้; หรือ claude-sonnet-4-6 ถ้าต้องการคุณภาพสูงขึ้น

app = FastAPI(title="NutriTrack LINE Coach")

# เปิด CORS ให้หน้า LIFF (คนละโดเมน) ส่งข้อมูลเข้า backend ได้
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # โปรดักชันจริงควรระบุโดเมน GitHub Pages ของคุณ
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================================
# 1) DATABASE  (เหมือน data model ใน spec — ย่อสำหรับ skeleton)
# ==========================================================
def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id TEXT PRIMARY KEY, sex TEXT, age INT, height_cm REAL,
            activity REAL DEFAULT 1.375, goal TEXT DEFAULT 'fat_loss');
        CREATE TABLE IF NOT EXISTS body_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, logged_at TEXT,
            weight REAL, body_fat REAL, visceral_fat INT, muscle_mass REAL);
        CREATE TABLE IF NOT EXISTS food_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, logged_at TEXT,
            name TEXT, kcal REAL, protein REAL, fat REAL, carb REAL);
        """)
init_db()

# มินิฐานข้อมูลอาหารไทย (โปรดักชันใช้ฐาน INMU มหิดล)
FOOD_DB = {
    "ข้าวมันไก่": (596, 25, 25, 68), "ผัดกะเพรา": (620, 28, 32, 55),
    "ส้มตำ": (140, 5, 3, 25), "อกไก่": (330, 62, 7, 0),
    "ไข่ต้ม": (78, 6, 5, 1), "เวย์": (120, 24, 2, 3),
    "ข้าวสวย": (120, 2, 0, 27), "สลัดอกไก่": (280, 35, 8, 18),
    "ก๋วยเตี๋ยว": (430, 20, 14, 55), "นมจืด": (130, 8, 5, 12),
}

# ==========================================================
# 2) ENGINE  (ตรงกับ prototype/spec เป๊ะ)
# ==========================================================
ACT = {"sedentary":1.2,"light":1.375,"moderate":1.55,"active":1.725}

def compute_targets(profile, body):
    w, bf = body["weight"], body.get("body_fat") or 0
    lbm = w * (1 - bf/100) if bf else None
    if bf:
        bmr = 370 + 21.6 * lbm                                   # Katch-McArdle
    else:                                                         # fallback Mifflin
        s = 5 if profile["sex"] == "M" else -161
        bmr = 10*w + 6.25*profile["height_cm"] - 5*profile["age"] + s
    tdee = bmr * profile["activity"]

    goal = profile["goal"]
    target = tdee - 400 if goal=="fat_loss" else tdee + 250 if goal=="muscle_gain" else tdee
    floored = target < bmr
    if floored: target = bmr                                     # GUARDRAIL: ห้ามต่ำกว่า BMR

    ppk = 1.8 if goal=="muscle_gain" else 2.2 if goal=="recomp" else 2.0
    protein = ppk * w
    fat = target*0.25/9
    carb = (target - protein*4 - fat*9)/4
    return dict(lbm=round(lbm,1) if lbm else None, bmr=round(bmr), tdee=round(tdee),
                target=round(target), protein=round(protein), fat=round(fat),
                carb=round(carb), floored=floored)

# ==========================================================
# 3) COACH  (system prompt + context — ยกมาจาก prototype ตรงๆ)
# ==========================================================
COACH_SYSTEM = """คุณคือ "โค้ชนิว" นักโภชนาการและเทรนเนอร์ส่วนตัวในแอปสุขภาพบน LINE พูดไทยเป็นกันเอง ให้กำลังใจแต่ตรงไปตรงมา

สิ่งที่ต้องทำทุกครั้งที่สรุป/ให้คำแนะนำ (สำคัญมาก):
1. บอก "ตัวเลขคงเหลือ" ชัดๆ เสมอ: เหลืออีกกี่ kcal และโปรตีนอีกกี่กรัม (เอาจากข้อมูลที่ให้มา)
2. แนะนำเมนูเจาะจงพร้อมปริมาณ ว่ากินอะไรบ้างถึงจะครบโปรตีน/แคลอรี่ที่เหลือ
   เช่น "เหลือโปรตีนอีก 90g ลองอกไก่ย่าง 250g (~58g) + ไข่ต้ม 3 ฟอง (~18g) + นม 1 กล่อง"
   ให้เมนูที่บวกแล้วใกล้เคียงยอดที่ขาดจริง ไม่ใช่พูดลอยๆ
3. เน้นอาหารไทย/หาง่าย และให้ทางเลือก 2-3 อย่าง

หลักการ:
- ตอบกระชับเหมือนแชต แต่ต้องมีตัวเลขคงเหลือ + เมนูแนะนำเสมอ
- ใช้ตัวเลขจริงของผู้ใช้ ห้ามตอบกว้างๆ แบบ "ยังต้องลุยต่อ" โดยไม่มีตัวเลขหรือเมนู

ความปลอดภัย (สำคัญมาก):
- ห้ามแนะนำกินต่ำกว่า BMR หรืออดอาหารแบบสุดโต่ง
- visceral fat สูงมาก ให้ย้ำเบาๆ ว่าควรปรึกษาแพทย์ ไม่วินิจฉัยโรคเอง
- ไม่ให้คำแนะนำทางการแพทย์เฉพาะโรค แนะนำพบแพทย์/นักกำหนดอาหารแทน
- ส่งเสริมพฤติกรรมยั่งยืน"""

def build_context(user_id):
    """รวมข้อมูลร่างกายล่าสุด + เป้าหมาย + อาหารวันนี้ เป็นข้อความ context"""
    with db() as c:
        prof = c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        body = c.execute("SELECT * FROM body_logs WHERE user_id=? ORDER BY logged_at DESC LIMIT 1",
                         (user_id,)).fetchone()
        today = date.today().isoformat()
        foods = c.execute("SELECT * FROM food_logs WHERE user_id=? AND logged_at LIKE ?",
                          (user_id, f"{today}%")).fetchall()
    if not prof or not body:
        return None, None

    profile = dict(sex=prof["sex"], age=prof["age"], height_cm=prof["height_cm"],
                   activity=prof["activity"], goal=prof["goal"])
    b = dict(weight=body["weight"], body_fat=body["body_fat"])
    t = compute_targets(profile, b)

    eaten = dict(k=sum(f["kcal"] for f in foods), p=sum(f["protein"] for f in foods))
    goal_th = {"fat_loss":"ลดไขมัน","muscle_gain":"เพิ่มกล้าม","recomp":"recomp","maintain":"คงน้ำหนัก"}[prof["goal"]]
    food_list = ", ".join(f["name"] for f in foods) or "ยังไม่ได้กินอะไร"

    ctx = f"""ข้อมูลผู้ใช้ ณ ตอนนี้:
- น้ำหนัก {body['weight']}kg, Body fat {body['body_fat']}%, LBM {t['lbm']}kg, Visceral fat {body['visceral_fat']}
- เป้าหมาย: {goal_th}
- BMR {t['bmr']} / TDEE {t['tdee']} / เป้าหมายแคลอรี่วันนี้ {t['target']} kcal
- โปรตีนเป้าหมาย {t['protein']}g
- วันนี้กินไป {round(eaten['k'])} kcal (โปรตีน {round(eaten['p'])}g)
- คงเหลือ {round(t['target']-eaten['k'])} kcal, ยังขาดโปรตีน {max(0,round(t['protein']-eaten['p']))}g
- รายการวันนี้: {food_list}"""
    return ctx, t

import time

def ask_llm(system, user_msg, retries=3):
    """เรียก LLM ตาม LLM_PROVIDER — มี auto-retry เผื่อเซิร์ฟเวอร์แน่นชั่วคราว (503)
    prompt (COACH_SYSTEM) และ context เหมือนกันทั้งสองเจ้า ไม่ต้องแก้ logic"""
    last_err = None
    for attempt in range(retries):
        try:
            if LLM_PROVIDER == "gemini":
                from google import genai
                from google.genai import types
                client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
                resp = client.models.generate_content(
                    model=GEMINI_MODEL, contents=user_msg,
                    config=types.GenerateContentConfig(system_instruction=system, max_output_tokens=1000))
                return (resp.text or "").strip()
            else:  # anthropic
                from anthropic import Anthropic
                client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
                resp = client.messages.create(model=CLAUDE_MODEL, max_tokens=1000,
                                               system=system, messages=[{"role":"user","content":user_msg}])
                return "".join(b.text for b in resp.content if b.type == "text").strip()
        except Exception as e:
            last_err = e
            msg = str(e)
            # ลองใหม่เฉพาะ error ชั่วคราว (503/overload/rate limit)
            if any(x in msg for x in ("503", "UNAVAILABLE", "overload", "429", "RESOURCE_EXHAUSTED")) and attempt < retries-1:
                time.sleep(1.5 * (attempt + 1))   # รอแล้วลองใหม่ (1.5s, 3s)
                continue
            raise
    raise last_err

# ==========================================================
# 4) HANDLERS
# ==========================================================
def handle_daily_summary(user_id):
    """ปุ่ม 'สรุปประจำวัน' บน Rich Menu → สรุป + คำแนะนำ"""
    ctx, _ = build_context(user_id)
    if not ctx:
        return "ยังไม่มีข้อมูลร่างกายเลยครับ กดปุ่ม 'กรอกข้อมูล' เพื่อเริ่มก่อนนะ 🙂"
    prompt = (ctx + "\n\nช่วยสรุปผลประจำวันแบบกระชับ ต้องมี:\n"
              "1) บรรทัดตัวเลขคงเหลือชัดๆ: เหลืออีกกี่ kcal และโปรตีนอีกกี่กรัม\n"
              "2) สิ่งที่ทำได้ดีวันนี้ 1 ข้อ\n"
              "3) แนะนำเมนูเจาะจง+ปริมาณ ว่ากินอะไรถึงจะครบโปรตีน/แคลอรี่ที่เหลือ (ให้ตัวเลือก 2-3 อย่าง บวกแล้วใกล้เคียงยอดที่ขาด)\n"
              "ใช้อิโมจินำแต่ละบรรทัดเล็กน้อย เน้นอาหารไทยหาง่าย")
    return ask_llm(COACH_SYSTEM, prompt)

def estimate_food_llm(text):
    """ให้ AI แยกและประเมินอาหาร 'ทุกอย่าง' ในข้อความ -> list ของ dict (รองรับหลายเมนูในครั้งเดียว)"""
    sys = ("คุณเป็นผู้เชี่ยวชาญโภชนาการ ผู้ใช้จะพิมพ์อาหารที่กิน อาจมีหลายอย่างในข้อความเดียว "
           "(คั่นด้วย + เครื่องหมาย เว้นวรรค คำว่า 'กับ' ฯลฯ) ให้แยกเป็นรายการแล้วประเมินแต่ละอย่าง "
           "ประเมินเมนูใดก็ได้ทั่วโลก (ไทย ญี่ปุ่น ฝรั่ง ฟาสต์ฟู้ด) "
           "ตอบกลับเป็น JSON array เท่านั้น ห้ามมีข้อความอื่นหรือ markdown "
           'รูปแบบ: [{"name":"ชื่อกระชับ","kcal":ตัวเลข,"protein":ตัวเลข,"fat":ตัวเลข,"carb":ตัวเลข}, ...] '
           "ประเมินตามปริมาณที่ระบุ ถ้าไม่ระบุให้ถือว่า 1 หน่วยเสิร์ฟปกติ "
           "หน่วย: kcal เป็นกิโลแคลอรี protein/fat/carb เป็นกรัม")
    try:
        raw = ask_llm(sys, text).replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
        if isinstance(data, dict):       # เผื่อ AI ตอบมาเป็นก้อนเดียว
            data = [data]
        items = []
        for d in data:
            items.append({"name": str(d.get("name", text))[:60],
                          "kcal": float(d.get("kcal", 0)), "protein": float(d.get("protein", 0)),
                          "fat": float(d.get("fat", 0)), "carb": float(d.get("carb", 0))})
        return [x for x in items if x["kcal"] > 0]
    except Exception as e:
        print("estimate error:", e)
        return None

def remaining_msg(user_id):
    """ข้อความยอดคงเหลือวันนี้ (ถ้าตั้งเป้าหมายไว้แล้ว)"""
    _, t = build_context(user_id)
    if not t:
        return "\n\n(กดปุ่ม 'กรอกข้อมูล' เพื่อตั้งเป้าหมาย จะได้เห็นยอดคงเหลือ)"
    today = date.today().isoformat()
    with db() as conn:
        foods = conn.execute("SELECT kcal,protein FROM food_logs WHERE user_id=? AND logged_at LIKE ?",
                             (user_id, f"{today}%")).fetchall()
    ek = sum(x["kcal"] for x in foods); ep = sum(x["protein"] for x in foods)
    return f"\n\nวันนี้เหลือ {round(t['target']-ek)} kcal · โปรตีนอีก {max(0,round(t['protein']-ep))}g"

def save_food(user_id, name, kcal, p, f, c_):
    with db() as conn:
        conn.execute("INSERT INTO food_logs(user_id,logged_at,name,kcal,protein,fat,carb) VALUES(?,?,?,?,?,?,?)",
                     (user_id, datetime.now().isoformat(), name, kcal, p, f, c_))

def handle_food_text(user_id, text):
    """ข้อความปกติ = log อาหาร — แยกได้หลายเมนูในครั้งเดียว (เช่น 'ลาบ+ข้าวสวย+ไข่ต้ม')"""
    items = []
    # ถ้ามีตัวคั่น (มีหลายเมนู) ให้ AI แยกทั้งหมดเลย กัน FOOD_DB จับได้ไม่ครบ
    multi = any(s in text for s in ["+", ",", "กับ", "และ", "/"])
    if not multi:
        # เมนูเดี่ยว: ลองจับจากฐานข้อมูลไทยก่อน (เร็ว แม่น ไม่เปลือง AI)
        for k, (kcal, p, f, c_) in FOOD_DB.items():
            if k in text:
                items.append({"name": k, "kcal": kcal, "protein": p, "fat": f, "carb": c_, "est": False})
                break
    # ไม่เจอในฐาน หรือมีหลายเมนู -> ให้ AI แยก+ประเมิน (รองรับอาหารทั่วโลก)
    if not items:
        est = estimate_food_llm(text)
        if est:
            items = [{**x, "est": True} for x in est]
    if not items:
        return "ขอโทษครับ ประเมินอาหารนี้ไม่ได้ ลองพิมพ์ให้ชัดขึ้น เช่น 'ลาบหมู 1 จาน + ข้าวสวย 1 ทัพพี'"
    # บันทึกทุกเมนู
    lines = []
    for it in items:
        save_food(user_id, it["name"], it["kcal"], it["protein"], it["fat"], it["carb"])
        tag = " 🤖" if it.get("est") else ""
        lines.append(f"• {it['name']}{tag} — {round(it['kcal'])} kcal, โปรตีน {round(it['protein'])}g")
    head = "บันทึกแล้ว ✅\n" + "\n".join(lines)
    return head + remaining_msg(user_id)

def handle_follow(user_id):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(user_id,sex,age,height_cm) VALUES(?,?,?,?)",
                  (user_id, "M", 30, 170))
    return ("สวัสดีครับ ผมโค้ชนิว 🥗\nกดเมนู 'กรอกข้อมูล' เพื่อใส่น้ำหนัก/เป้าหมาย "
            "แล้วพิมพ์อาหารที่กินได้เลย เช่น 'ข้าวมันไก่ 1 จาน'\n"
            "อยากดูสรุปเมื่อไรก็กดปุ่ม 'สรุปประจำวัน' ได้ตลอด")

# ==========================================================
# 5) WEBHOOK
# ==========================================================
@app.post("/webhook")
async def webhook(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = (await request.body()).decode()
    try:
        events = parser.parse(body, signature)
    except InvalidSignatureError:
        raise HTTPException(400, "Invalid signature")

    for ev in events:
        uid = ev.source.user_id
        print("WEBHOOK uid:", uid, "|", type(ev).__name__)   # ดู userId ฝั่งบอท
        reply = None

        try:
            if isinstance(ev, FollowEvent):
                reply = handle_follow(uid)

            elif isinstance(ev, PostbackEvent):
                # Rich Menu / ปุ่มต่างๆ ส่ง postback data มา
                data = dict(p.split("=") for p in ev.postback.data.split("&"))
                if data.get("action") == "daily_summary":
                    reply = handle_daily_summary(uid)
                # เพิ่ม action อื่น: open_form (เปิด LIFF), set_goal ฯลฯ

            elif isinstance(ev, MessageEvent) and isinstance(ev.message, TextMessage):
                reply = handle_food_text(uid, ev.message.text.strip())
        except Exception as e:
            print("HANDLER error:", repr(e))
            reply = "ขอโทษครับ ระบบขัดข้องชั่วคราว (เซิร์ฟเวอร์ AI แน่น) ลองใหม่อีกครั้งในสักครู่นะครับ 🙏"

        if reply:
            try:
                line_bot_api.reply_message(ev.reply_token, TextSendMessage(text=reply))
            except Exception as e:
                print("REPLY error:", repr(e))

    return "OK"

# ==========================================================
# 6) LIFF API (ฟอร์มกรอกข้อมูลร่างกายเรียกมาที่นี่)
# ==========================================================
@app.post("/api/body-log")
async def api_body_log(req: Request):
    d = await req.json()                       # {user_id, weight, body_fat, visceral_fat, ...}
    uid = d["user_id"]
    print("BODY-LOG uid:", uid)                # ดู userId ฝั่งฟอร์ม LIFF (เทียบกับฝั่งบอท)

    def val(key):  # ค่าที่ส่งมาจริง (ไม่นับค่าว่าง/0/None)
        v = d.get(key)
        return v if v not in (None, "", 0, "0") else None

    with db() as c:
        # สร้าง user row ถ้ายังไม่มี (เผื่อกรอกฟอร์มก่อนเคยทักบอท)
        c.execute("INSERT OR IGNORE INTO users(user_id,sex,age,height_cm) VALUES(?,?,?,?)",
                  (uid, "M", 30, 170))
        # QUICK-ENTRY: ค่าที่ไม่ได้กรอก ให้ดึงค่าล่าสุดจาก DB มาเติมให้
        last = c.execute("""SELECT body_fat,visceral_fat,muscle_mass FROM body_logs
                            WHERE user_id=? ORDER BY logged_at DESC LIMIT 1""", (uid,)).fetchone()
        body_fat     = val("body_fat")     or (last["body_fat"]     if last else None)
        visceral_fat = val("visceral_fat") or (last["visceral_fat"] if last else None)
        muscle_mass  = val("muscle_mass")  or (last["muscle_mass"]  if last else None)

        c.execute("""INSERT INTO body_logs(user_id,logged_at,weight,body_fat,visceral_fat,muscle_mass)
                     VALUES(?,?,?,?,?,?)""",
                  (uid, datetime.now().isoformat(), d["weight"], body_fat, visceral_fat, muscle_mass))
        c.execute("""UPDATE users SET activity=?, goal=? WHERE user_id=?""",
                  (ACT.get(d.get("activity_level"),1.375), d.get("goal","fat_loss"), uid))
    ctx, t = build_context(uid)
    return {"ok": True, "targets": t}

# ดึงข้อมูลล่าสุดของผู้ใช้ — ให้หน้า LIFF โหลดมาแสดงตอนเปิด (dashboard ไม่ว่าง)
@app.get("/api/latest")
async def api_latest(user_id: str):
    with db() as c:
        prof = c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        body = c.execute("SELECT * FROM body_logs WHERE user_id=? ORDER BY logged_at DESC LIMIT 1",
                         (user_id,)).fetchone()
    if not body:
        return {"has_data": False}
    targets = None
    if prof:
        targets = compute_targets(
            dict(sex=prof["sex"], age=prof["age"], height_cm=prof["height_cm"],
                 activity=prof["activity"], goal=prof["goal"]),
            dict(weight=body["weight"], body_fat=body["body_fat"] or 0))
    return {
        "has_data": True,
        "weight": body["weight"], "body_fat": body["body_fat"],
        "visceral_fat": body["visceral_fat"], "muscle_mass": body["muscle_mass"],
        "activity": prof["activity"] if prof else 1.375,
        "goal": prof["goal"] if prof else "fat_loss",
        "targets": targets,
    }

# สรุปประจำวัน — ให้ปุ่มในหน้า LIFF เรียกใช้ได้ (เหมือนปุ่มในแชต LINE)
@app.post("/api/summary")
async def api_summary(req: Request):
    d = await req.json()
    return {"text": handle_daily_summary(d["user_id"])}

# คุยกับโค้ช — ให้หน้า LIFF เรียกใช้ได้ (ใช้ Gemini ผ่าน backend)
@app.post("/api/coach")
async def api_coach(req: Request):
    d = await req.json()
    uid = d["user_id"]
    question = (d.get("message") or "").strip()
    if not question:
        return {"text": "ส่งคำถามมาได้เลยครับ"}
    ctx, _ = build_context(uid)
    prompt = f"{ctx}\n\nคำถาม: {question}" if ctx else question
    return {"text": ask_llm(COACH_SYSTEM, prompt)}

@app.get("/api/latest")
async def api_latest(user_id: str):
    """หน้าเว็บเรียกตอนเปิด เพื่อดึงข้อมูลล่าสุด + เป้าหมาย + ยอดกินวันนี้ มาแสดงบน dashboard"""
    with db() as c:
        prof = c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        body = c.execute("SELECT * FROM body_logs WHERE user_id=? ORDER BY logged_at DESC LIMIT 1",
                         (user_id,)).fetchone()
        today = date.today().isoformat()
        foods = c.execute("SELECT name,kcal,protein,fat,carb FROM food_logs WHERE user_id=? AND logged_at LIKE ?",
                          (user_id, f"{today}%")).fetchall()
    if not body:
        return {"ok": True, "hasData": False}
    _, t = build_context(user_id)
    return {
        "ok": True, "hasData": True,
        "body": {"weight": body["weight"], "body_fat": body["body_fat"],
                 "visceral_fat": body["visceral_fat"], "muscle_mass": body["muscle_mass"]},
        "profile": {"activity": prof["activity"] if prof else 1.375,
                    "goal": prof["goal"] if prof else "fat_loss"},
        "targets": t,
        "eaten": {"kcal": sum(f["kcal"] for f in foods), "protein": sum(f["protein"] for f in foods),
                  "fat": sum(f["fat"] for f in foods), "carb": sum(f["carb"] for f in foods)},
        "foods": [dict(f) for f in foods],
    }

"""
หมายเหตุการตั้งค่า Rich Menu (ทำครั้งเดียวผ่าน LINE API):
ปุ่ม 'สรุปประจำวัน' ตั้ง action เป็น postback แบบ:  data = "action=daily_summary"
ปุ่ม 'กรอกข้อมูล'   ตั้ง action เป็น uri → เปิด LIFF (หน้า prototype ที่ทำไว้)
ปุ่ม 'log อาหาร'    ตั้ง action เป็น message ให้ผู้ใช้พิมพ์ หรือ uri เปิด LIFF
"""
