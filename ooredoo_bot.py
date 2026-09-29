import asyncio
import os
import re
import logging
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"

SIGNIN_URL = "https://my.ooredoo.dz/sign-in"
DASHBOARD_URL = "https://my.ooredoo.dz/dashboard/my-ooredoo"
STATE_FILE = Path("state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ooredoo")

# ---------- حالة انتظار الإدخال ----------
WAITING = {
    "username": None,     # asyncio.Future
    "password": None,
}


# ---------------- إشعارات ----------------
async def notify(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID, text=msg,
            parse_mode="HTML", disable_web_page_preview=True,
        )
    except Exception as e:
        log.error("Telegram error: %s", e)


async def send_photo(path: str, caption: str = ""):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        with open(path, "rb") as f:
            await bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID, photo=f, caption=caption[:1024],
            )
    except Exception as e:
        log.error("Telegram photo error: %s", e)


async def send_document(path: str, filename: str, caption: str = ""):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        with open(path, "rb") as f:
            await bot.send_document(
                chat_id=TELEGRAM_CHAT_ID, document=f,
                filename=filename, caption=caption[:1024],
            )
    except Exception as e:
        log.error("Telegram document error: %s", e)


# ---------------- Telegram listener ----------------
async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return
    await update.message.reply_text("🚀 بوت Ooredoo جاهز.\nاكتب /check لجلب الرصيد.")


async def check_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return
    await update.message.reply_text("⏳ غادي نطلب منك اسم المستخدم وكلمة السر...")
    asyncio.create_task(run_once())


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """يستقبل الرسائل منك ويعبّي الـ future المناسب"""
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    # إذا كاين انتظار لاسم المستخدم
    if WAITING["username"] and not WAITING["username"].done():
        WAITING["username"].set_result(text)
        # نحذف الرسالة من الشات للأمان (اختياري)
        try:
            await update.message.delete()
        except Exception:
            pass
        await update.message.reply_text("✅ توصلت باسم المستخدم. أرسل الآن كلمة السر.")
        return

    # إذا كاين انتظار لكلمة السر
    if WAITING["password"] and not WAITING["password"].done():
        WAITING["password"].set_result(text)
        try:
            await update.message.delete()
        except Exception:
            pass
        await update.message.reply_text("✅ توصلت بكلمة السر. جاري تسجيل الدخول...")
        return

    # رسائل أخرى
    await update.message.reply_text("ℹ️ اكتب /check لجلب الرصيد.")


async def ask_username() -> str:
    """يبعتلك طلب اسم المستخدم ويستنى ردّك"""
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    WAITING["username"] = fut
    await notify("📱 أرسل <b>اسم المستخدم</b> (Nom d'utilisateur):")
    try:
        result = await asyncio.wait_for(fut, timeout=300)  # 5 دقائق
    except asyncio.TimeoutError:
        WAITING["username"] = None
        raise Exception("انتهت المدة (5 دقائق) بلا رد")
    WAITING["username"] = None
    return result


async def ask_password() -> str:
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    WAITING["password"] = fut
    await notify("🔒 أرسل <b>كلمة السر</b> (Mot de passe):")
    try:
        result = await asyncio.wait_for(fut, timeout=300)
    except asyncio.TimeoutError:
        WAITING["password"] = None
        raise Exception("انتهت المدة (5 دقائق) بلا رد")
    WAITING["password"] = None
    return result


# ---------------- Captcha ----------------
async def detect_captcha(page) -> str | None:
    checks = {
        "recaptcha_v2": "iframe[src*='google.com/recaptcha/api2/anchor']",
        "hcaptcha": "iframe[src*='hcaptcha.com']",
        "turnstile": "iframe[src*='challenges.cloudflare.com']",
    }
    for name, sel in checks.items():
        try:
            if await page.locator(sel).count() > 0:
                # نتأكدو بلي ماشي invisible
                loc = page.locator(sel).first
                box = await loc.bounding_box()
                if box and box["width"] > 0 and box["height"] > 0:
                    return name
        except Exception:
            continue
    return None


# ---------------- Extraction ----------------
async def extract_balance(page) -> str | None:
    try:
        body = await page.inner_text("body")
        patterns = [
            r"(?:solde|balance|رصيد|الرصيد)\D{0,30}([\d]+[.,][\d]{1,2})",
            r"([\d]+[.,][\d]{1,2})\s*(?:DA|دج|DZD)",
        ]
        for p in patterns:
            m = re.search(p, body, re.IGNORECASE)
            if m:
                return m.group(1).replace(",", ".")
    except Exception:
        pass

    selectors = [
        "[data-testid*='balance' i]",
        ".balance", ".balance-value", ".solde",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                txt = await loc.inner_text()
                m = re.search(r"([\d]+[.,][\d]{1,2})", txt)
                if m:
                    return m.group(1).replace(",", ".")
        except Exception:
            continue
    return None


async def is_logged_in(page) -> bool:
    """نتحققو فعلياً: واش مازال في /sign-in؟"""
    try:
        if "/sign-in" in page.url or "/login" in page.url:
            # نتحققو إذا حقل كلمة السر ما زال ظاهر
            if await page.locator("input[type='password']").count() > 0:
                return False
    except Exception:
        pass
    return True


# ---------------- Main flow ----------------
async def login_and_get_balance(browser) -> str | None:
    # 1) الجلسة المحفوظة؟
    if STATE_FILE.exists():
        log.info("Trying saved session")
        context = await browser.new_context(
            storage_state=str(STATE_FILE),
            locale="fr-FR",
            viewport={"width": 412, "height": 915},
        )
        page = await context.new_page()
        try:
            await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(4000)
            if await is_logged_in(page):
                log.info("Saved session valid")
                balance = await extract_balance(page)
                if balance:
                    await notify(f"💰 <b>الرصيد:</b> {balance} دج")
                    await context.close()
                    return balance
                # إذا الجلسة صالحة لكن ما لقيناش الرصيد، نكملو في الصفحة
                await context.close()
                # نكملو عادي
            else:
                log.info("Saved session expired")
                await context.close()
        except Exception as e:
            log.warning("Saved session attempt failed: %s", e)
            try:
                await context.close()
            except Exception:
                pass

    # 2) جلسة جديدة
    context = await browser.new_context(
        locale="fr-FR",
        viewport={"width": 412, "height": 915},
        user_agent=("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"),
    )
    page = await context.new_page()

    try:
        # 3) نروحو لـ /sign-in
        log.info("Opening /sign-in")
        await page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(4000)

        # كابتشا؟
        captcha = await detect_captcha(page)
        if captcha:
            await page.screenshot(path="captcha.png", full_page=True)
            await send_photo("captcha.png", f"⚠️ كاين كابتشا: {captcha}")
            await notify("⛔ ما نقدرش نكمل. نحتاجو أداة حل كابتشا.")
            await context.close()
            return None

        # 4) نطلب من المستخدم البيانات
        username = await ask_username()
        password = await ask_password()

        # 5) نعبّيو الحقول — نلقاوهم واحد واحد
        await page.wait_for_timeout(500)

        # --- اسم المستخدم ---
        user_selectors = [
            "input[placeholder*='utilisateur' i]",
            "input[placeholder*='Nom' i]",
            "input[name*='user' i]",
            "input[id*='user' i]",
            "input[type='text']",
            "input[type='email']",
        ]
        user_filled = False
        for sel in user_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    await loc.click()
                    await loc.fill("")  # نمسحو أي شي
                    await loc.type(username, delay=30)
                    log.info("Username filled with: %s", sel)
                    user_filled = True
                    break
            except Exception as e:
                log.warning("username selector %s failed: %s", sel, e)
                continue

        if not user_filled:
            await page.screenshot(path="no_user_field.png", full_page=True)
            await send_photo("no_user_field.png", "❌ ما لقيتش حقل اسم المستخدم")
            raise Exception("ما لقيتش حقل اسم المستخدم")

        # --- كلمة السر ---
        pwd_sel = "input[type='password']"
        await page.wait_for_selector(pwd_sel, timeout=10000)
        pwd_loc = page.locator(pwd_sel).first
        await pwd_loc.click()
        await pwd_loc.fill("")
        await pwd_loc.type(password, delay=30)
        log.info("Password filled")

        # 6) نأكدو بلي الحقول معبّيين
        await page.wait_for_timeout(1000)
        await page.screenshot(path="filled.png", full_page=True)
        await send_photo("filled.png", "📸 الصفحة بعد التعبئة — نتحققو")

        # 7) نضغطو Connexion — نستعملو JS باش نضغطو حتى لو disabled
        clicked = False
        try:
            btn = page.locator(
                "button:has-text('Connexion'), "
                "button:has-text('Se connecter'), "
                "button[type='submit']"
            ).first
            if await btn.count() > 0:
                # نستنى يكون enabled
                for _ in range(20):
                    is_disabled = await btn.get_attribute("disabled")
                    if is_disabled is None:
                        break
                    await page.wait_for_timeout(300)
                await btn.click()
                clicked = True
                log.info("Clicked Connexion button")
        except Exception as e:
            log.warning("Btn click failed: %s", e)

        if not clicked:
            # نحاول JS
            try:
                await page.evaluate("""
                    () => {
                        const btns = document.querySelectorAll('button');
                        for (const b of btns) {
                            if (/connexion|connecter/i.test(b.textContent||'')) {
                                b.removeAttribute('disabled');
                                b.click();
                                return true;
                            }
                        }
                        return false;
                    }
                """)
                log.info("Clicked Connexion via JS")
            except Exception as e:
                log.error("JS click failed: %s", e)

        # 8) ننتظرو
        await page.wait_for_timeout(6000)
        try:
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass

        # 9) واش دخلنا؟
        if not await is_logged_in(page):
            await page.screenshot(path="login_failed.png", full_page=True)
            await send_photo("login_failed.png", "❌ ما دخلناش")
            await notify(f"❌ URL الحالي: <code>{page.url}</code>")
            await context.close()
            return None

        # 10) نحفظو الجلسة
        await context.storage_state(path=str(STATE_FILE))
        log.info("Session saved")

        # 11) نروحو لصفحة الرصيد
        await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(6000)

        # 12) نستخرجو الرصيد
        balance = await extract_balance(page)

        if balance:
            await notify(f"💰 <b>الرصيد:</b> {balance} دج")
            await context.close()
            return balance
        else:
            await page.screenshot(path="no_balance.png", full_page=True)
            await send_photo("no_balance.png", "📸 dashboard — ما لقيتش الرصيد")

            try:
                body = await page.inner_text("body")
                await notify(f"📝 <b>نص الصفحة:</b>\n<pre>{body[:3000]}</pre>")
            except Exception as e:
                log.error("text dump: %s", e)

            try:
                html = await page.content()
                Path("page_dump.html").write_text(html, encoding="utf-8")
                await send_document("page_dump.html", "page_dump.html", "📄 HTML")
            except Exception as e:
                log.error("html dump: %s", e)

            await notify(f"🔗 URL: <code>{page.url}</code>")
            await context.close()
            return None

    except Exception as e:
        log.exception("login flow error")
        await notify(f"❌ خطأ: <code>{e}</code>")
        await context.close()
        return None


# ---------------- run_once ----------------
async def run_once():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-gpu", "--single-process"],
        )
        try:
            await login_and_get_balance(browser)
        finally:
            await browser.close()


# ---------------- main ----------------
async def main():
    log.info("Bot starting...")
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise Exception("TELEGRAM_TOKEN أو TELEGRAM_CHAT_ID ناقصين")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    await notify(
        "🚀 بوت Ooredoo جاهز.\n"
        "اكتب /check باش نطلب منك البيانات ونجيب الرصيد."
    )

    # نحافظو على التشغيل
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
