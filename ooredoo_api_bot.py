import asyncio
import os
import re
import json
import time
import uuid
import hashlib
import logging
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

# ثوابت من HAR
BASE_URL = "https://apis.ooredoo.dz"
REALM = "myooredoo"
AUTH_BASE = f"{BASE_URL}/api/auth/realms/{REALM}/protocol/openid-connect"
CLIENT_ID = "myooredoo-app"

# ثوابت ثابتة (لا تتغير)
X_VERSION = "1.5.15"
PLATFORM = "android"
PLATFORM_ORIGIN = "mobile-android"
USER_AGENT = "Dart/3.11 (dart:io)"

# ⚠️ معلومات الجهاز — من HAR
# هذه ثابتة للجهاز المسجل في Ooredoo. إذا غيّرتها، السيرفر يمكن يرفض.
DEVICE_FINGERPRINT = os.getenv("DEVICE_FINGERPRINT", "bcdc9eca56796f5d3c087e2a133fb5c36b07fe6c34f20b2f4ad3fa3a7845308b")
INSTANCE_ID = os.getenv("INSTANCE_ID", "1992a870-b975-11f1-9fa4-c120a3d6d3ec1790404801655")
DEVICE_ID = os.getenv("DEVICE_ID", "1992a870-b975-11f1-9fa4-c120a3d6d3ec1790404801655")

# نشوفو إذا عندنا token محفوظ (من HAR أو من دخول سابق)
SAVED_TOKEN = os.getenv("OOREDOO_TOKEN", "").strip()
SAVED_MSISDN = os.getenv("OOREDOO_MSISDN", "").strip()

STATE_FILE = Path("state.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ooredoo")

WAITING = {"username": None, "password": None}


# ================== Telegram ==================
async def notify(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg,
                               parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        log.error("Telegram error: %s", e)


async def send_document(path: str, filename: str, caption: str = ""):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        with open(path, "rb") as f:
            await bot.send_document(chat_id=TELEGRAM_CHAT_ID, document=f,
                                    filename=filename, caption=caption[:1024])
    except Exception as e:
        log.error("Telegram doc error: %s", e)


# ================== Helpers ==================
def now_ms() -> int:
    return int(time.time() * 1000)


def new_uuid() -> str:
    return str(uuid.uuid4())


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def make_headers(token: str = "") -> dict:
    """نولّدو نفس headers من HAR"""
    ts = str(now_ms())
    nonce = md5(f"{ts}-{new_uuid()}")
    chronos = md5(f"{ts}-{new_uuid()}")
    correlation = new_uuid()

    platform_sig_raw = json.dumps({
        "platform": "android",
        "is-physical-device": True,
        "device-id": DEVICE_ID,
    })
    import base64
    platform_sig = base64.b64encode(platform_sig_raw.encode()).decode()

    headers = {
        "user-agent": USER_AGENT,
        "accept-encoding": "gzip",
        "x-platform-origin": PLATFORM_ORIGIN,
        "x-correlation-id": correlation,
        "platform": PLATFORM,
        "x-version": X_VERSION,
        "x-signature": "f320f896f3da2a5a0284f9af316efb4ab0432b26406413568db116fa9dc60feb",
        "accept": "*/*",
        "x-platform-data-signature": platform_sig,
        "x-timestamp": ts,
        "x-instance-id": INSTANCE_ID,
        "x-nonce-id": nonce,
        "x-chronos-id": chronos,
        "x-device-fingerprint": DEVICE_FINGERPRINT,
    }
    if token:
        headers["authorization"] = f"Bearer {token}"
    return headers


# ================== Login via Keycloak ==================
async def keycloak_login(username: str, password: str) -> Optional[dict]:
    """
    يحاول يجيب token من Keycloak Direct Grant
    يرجع dict فيه: access_token, refresh_token, expires_in, ... أو None
    """
    url = f"{AUTH_BASE}/token"

    # نجربو أكثر من client_id
    candidates = [
        {"client_id": CLIENT_ID, "grant_type": "password"},
        {"client_id": CLIENT_ID, "grant_type": "password", "scope": "openid profile email mobile_number ratePlan"},
    ]

    # نجربو أكثر من صيغة للـ username
    username_variants = list({
        username,
        username.lstrip("0") if username.startswith("0") else f"0{username}",
        f"213{username[1:]}" if username.startswith("0") else username,
        f"+213{username[1:]}" if username.startswith("0") else username,
    })

    async with httpx.AsyncClient(timeout=30) as client:
        for u in username_variants:
            for c in candidates:
                data = {
                    "client_id": c["client_id"],
                    "grant_type": c["grant_type"],
                    "username": u,
                    "password": password,
                }
                if "scope" in c:
                    data["scope"] = c["scope"]

                try:
                    log.info("Trying Keycloak login: user=%s client=%s", u, c["client_id"])
                    r = await client.post(
                        url,
                        data=data,
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded",
                            "User-Agent": USER_AGENT,
                        },
                    )
                    log.info("Response %s: %s", r.status_code, r.text[:300])

                    if r.status_code == 200:
                        j = r.json()
                        if "access_token" in j:
                            log.info("Got access token!")
                            return j
                    elif r.status_code in (400, 401):
                        # نجربو تركيبة أخرى
                        continue
                except Exception as e:
                    log.warning("Keycloak request failed: %s", e)

    return None


# ================== API calls ==================
async def api_get(path: str, token: str, params: dict = None) -> Optional[dict]:
    url = f"{BASE_URL}{path}"
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.get(
                url,
                headers=make_headers(token),
                params=params or {},
            )
            log.info("GET %s → %s", url, r.status_code)
            if r.status_code == 200:
                return r.json()
            else:
                log.warning("API %s returned %s: %s", path, r.status_code, r.text[:300])
        except Exception as e:
            log.error("api_get error: %s", e)
    return None


# ================== Balance ==================
async def get_balance(token: str, msisdn: str) -> Optional[float]:
    """يجيب الرصيد من أكثر من endpoint محتمل"""

    # 1) من userInfo/personal (من HAR شفنا "Current Balance")
    # لكن ما كاينش مباشرة. نجربو endpoints أخرى.

    endpoints = [
        # من HAR
        ("/api/ooredoo-bff/userInfo/personal", {"msisdn": msisdn}),
        ("/api/ooredoo-bff/dashboard", {"msisdn": msisdn}),
        ("/api/ooredoo-bff/balance", {"msisdn": msisdn}),
        ("/api/ooredoo-bff/balance/current", {"msisdn": msisdn}),
        ("/api/ooredoo-bff/account/balance", {"msisdn": msisdn}),
        ("/api/ooredoo-bff/prepaid/balance", {"msisdn": msisdn}),
    ]

    for path, params in endpoints:
        data = await api_get(path, token, params)
        if not data:
            continue

        # نبحثو عن الرصيد في أي مكان في JSON
        balance = find_balance_in_json(data)
        if balance is not None:
            log.info("Balance found in %s: %s", path, balance)
            return balance

    return None


def find_balance_in_json(obj, depth: int = 0) -> Optional[float]:
    """يقلب recursive على أي مفتاح فيه 'balance' أو 'solde'"""
    if depth > 6:
        return None

    if isinstance(obj, dict):
        # أولاً: مفتاح فيه كلمة balance
        for k, v in obj.items():
            kl = k.lower()
            if any(word in kl for word in ["balance", "solde", "credit"]):
                try:
                    if isinstance(v, (int, float)):
                        return float(v)
                    if isinstance(v, str):
                        m = re.search(r"([\d]+[.,][\d]{1,2})", v)
                        if m:
                            return float(m.group(1).replace(",", "."))
                except Exception:
                    pass
        # ثاني: نص "Current Balance"
        for k, v in obj.items():
            if isinstance(v, str):
                m = re.search(r"(?:balance|solde)[^\d]{0,10}([\d]+[.,][\d]{1,2})",
                              v, re.IGNORECASE)
                if m:
                    return float(m.group(1).replace(",", "."))

        # recursive
        for v in obj.values():
            r = find_balance_in_json(v, depth + 1)
            if r is not None:
                return r

    elif isinstance(obj, list):
        for item in obj:
            r = find_balance_in_json(item, depth + 1)
            if r is not None:
                return r

    return None


# ================== Telegram handlers ==================
async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return
    await update.message.reply_text("🚀 بوت Ooredoo (API) جاهز.\nاكتب /check.")


async def check_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return
    await update.message.reply_text("⏳ نجربو الدخول عبر API...")
    asyncio.create_task(run_once())


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()

    if WAITING["username"] and not WAITING["username"].done():
        WAITING["username"].set_result(text)
        try:
            await update.message.delete()
        except Exception:
            pass
        await update.message.reply_text("✅ توصلت بالرقم. أرسل كلمة السر.")
        return

    if WAITING["password"] and not WAITING["password"].done():
        WAITING["password"].set_result(text)
        try:
            await update.message.delete()
        except Exception:
            pass
        await update.message.reply_text("✅ جاري المحاولة...")
        return

    await update.message.reply_text("ℹ️ اكتب /check.")


async def ask_username() -> str:
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    WAITING["username"] = fut
    await notify("📱 أرسل <b>الرقم</b> (مثال: <code>0553372434</code>):")
    try:
        r = await asyncio.wait_for(fut, timeout=300)
    except asyncio.TimeoutError:
        WAITING["username"] = None
        raise Exception("timeout")
    WAITING["username"] = None
    return r


async def ask_password() -> str:
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    WAITING["password"] = fut
    await notify("🔒 أرسل <b>كلمة السر</b>:")
    try:
        r = await asyncio.wait_for(fut, timeout=300)
    except asyncio.TimeoutError:
        WAITING["password"] = None
        raise Exception("timeout")
    WAITING["password"] = None
    return r


# ================== Main flow ==================
async def run_once():
    try:
        token = SAVED_TOKEN
        msisdn = SAVED_MSISDN

        # 1) إذا ما عندناش token، نجيبوه بـ login
        if not token:
            username = await ask_username()
            password = await ask_password()

            # نحولو الرقم لصيغة دولية
            digits = re.sub(r"\D", "", username)
            if digits.startswith("0"):
                msisdn = f"213{digits[1:]}"
            elif digits.startswith("213"):
                msisdn = digits
            else:
                msisdn = digits

            await notify(f"🔐 نحاول الدخول بـ: <code>{msisdn}</code>")

            tokens = await keycloak_login(msisdn, password)
            if not tokens:
                # نجربو بالصيغة الأصلية
                tokens = await keycloak_login(username, password)

            if not tokens:
                await notify("❌ فشل الدخول عبر Keycloak")
                return

            token = tokens.get("access_token")
            if not token:
                await notify("❌ ما رجعش access_token")
                return

            await notify("✅ نجح الدخول! جاري جلب الرصيد...")

        # 2) نجيبو الرصيد
        balance = await get_balance(token, msisdn)

        if balance is not None:
            await notify(f"💰 <b>الرصيد:</b> {balance} دج")
        else:
            await notify("⚠️ ما لقيناش الرصيد. نجربو معلومات الحساب...")

            # نجيبو userInfo
            user_info = await api_get("/api/ooredoo-bff/userInfo/personal",
                                      token, {"msisdn": msisdn})
            if user_info:
                info_str = json.dumps(user_info, ensure_ascii=False, indent=2)
                Path("userinfo.json").write_text(info_str, encoding="utf-8")
                await send_document("userinfo.json", "userinfo.json", "📄 userInfo")
                await notify(f"📝 <pre>{info_str[:1500]}</pre>")
            else:
                await notify("❌ حتى userInfo ما رجعش. ممكن التوكن منتهي أو ما عندوش صلاحيات.")

    except Exception as e:
        log.exception("run_once error")
        await notify(f"❌ خطأ: <code>{e}</code>")


# ================== Main ==================
async def main():
    log.info("API Bot starting...")
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise Exception("TELEGRAM_TOKEN أو TELEGRAM_CHAT_ID ناقصين")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    await notify("🚀 بوت Ooredoo (API) جاهز.\nاكتب /check.")

    stop = asyncio.Event()
    try:
        await stop.wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
