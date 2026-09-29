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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ooredoo")

WAITING = {"username": None, "password": None}


async def notify(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg,
                               parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        log.error("Telegram error: %s", e)


async def send_photo(path: str, caption: str = ""):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        with open(path, "rb") as f:
            await bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=f, caption=caption[:1024])
    except Exception as e:
        log.error("Telegram photo error: %s", e)


async def send_document(path: str, filename: str, caption: str = ""):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        with open(path, "rb") as f:
            await bot.send_document(chat_id=TELEGRAM_CHAT_ID, document=f,
                                    filename=filename, caption=caption[:1024])
    except Exception as e:
        log.error("Telegram document error: %s", e)


# ---------- Telegram handlers ----------
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
        await update.message.reply_text("✅ توصلت باسم المستخدم. أرسل الآن كلمة السر.")
        return

    if WAITING["password"] and not WAITING["password"].done():
        WAITING["password"].set_result(text)
        try:
            await update.message.delete()
        except Exception:
            pass
        await update.message.reply_text("✅ توصلت بكلمة السر. جاري تسجيل الدخول...")
        return

    await update.message.reply_text("ℹ️ اكتب /check لجلب الرصيد.")


async def ask_username() -> str:
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    WAITING["username"] = fut
    await notify("📱 أرسل <b>اسم المستخدم</b> (Nom d'utilisateur):")
    try:
        result = await asyncio.wait_for(fut, timeout=300)
    except asyncio.TimeoutError:
        WAITING["username"] = None
        raise Exception("انتهت المدة بلا رد")
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
        raise Exception("انتهت المدة بلا رد")
    WAITING["password"] = None
    return result


# ---------- Captcha ----------
async def detect_captcha(page) -> str | None:
    checks = {
        "recaptcha_v2": "iframe[src*='google.com/recaptcha/api2/anchor']",
        "hcaptcha": "iframe[src*='hcaptcha.com']",
        "turnstile": "iframe[src*='challenges.cloudflare.com']",
    }
    for name, sel in checks.items():
        try:
            if await page.locator(sel).count() > 0:
                loc = page.locator(sel).first
                box = await loc.bounding_box()
                if box and box["width"] > 0 and box["height"] > 0:
                    return name
        except Exception:
            continue
    return None


# ---------- Balance ----------
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
    try:
        if "/sign-in" in page.url or "/login" in page.url:
            if await page.locator("input[type='password']").count() > 0:
                return False
    except Exception:
        pass
    return True


# ---------- username filling ----------
async def fill_username(page, username: str) -> bool:
    """3 محاولات: label → iteration على inputs → JS"""

    # 1) عن طريق label "Nom d'utilisateur"
    try:
        xp = ("xpath=//label[contains(., \"Nom d'utilisateur\")]"
              "/following::input[1]")
        inp = page.locator(xp).first
        if await inp.count() > 0:
            await inp.click()
            await inp.fill("")
            await inp.type(username, delay=40)
            log.info("Username filled via label XPath")
            return True
    except Exception as e:
        log.warning("label strategy failed: %s", e)

    # 2) iteration على كل الـ inputs المرئية
    try:
        all_inputs = page.locator("input:visible")
        count = await all_inputs.count()
        log.info("Visible inputs: %d", count)

        for i in range(count):
            inp = all_inputs.nth(i)
            t = await inp.get_attribute("type") or "text"
            ph = (await inp.get_attribute("placeholder") or "").lower()
            name = (await inp.get_attribute("name") or "").lower()

            log.info("input[%d] type=%s ph=%s name=%s", i, t, ph, name)

            if t == "password" or t in ("hidden", "submit", "button", "checkbox", "radio"):
                continue
            if any(x in ph for x in ("search", "recherche", "téléphone", "phone")):
                continue

            await inp.click()
            await inp.fill("")
            await inp.type(username, delay=40)
            log.info("Username filled via input[%d]", i)
            return True
    except Exception as e:
        log.warning("input iteration failed: %s", e)

    # 3) JS احتياطي
    try:
        ok = await page.evaluate(
            """
            (val) => {
                const inputs = [...document.querySelectorAll('input')].filter(i => {
                    const t = i.type || 'text';
                    const st = getComputedStyle(i);
                    return t !== 'password' && t !== 'hidden' &&
                           st.display !== 'none' && st.visibility !== 'hidden' &&
                           i.offsetParent !== null;
                });
                if (!inputs.length) return false;
                const inp = inputs[0];
                inp.focus();
                inp.value = val;
                inp.dispatchEvent(new Event('input', {bubbles: true}));
                inp.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            }
            """,
            username,
        )
        if ok:
            log.info("Username filled via JS")
            return True
    except Exception as e:
        log.warning("JS fill failed: %s", e)

    return False


# ---------- main flow ----------
async def login_and_get_balance(browser) -> str | None:
    # session saved?
    if STATE_FILE.exists():
        log.info("Trying saved session")
        ctx = await browser.new_context(
            storage_state=str(STATE_FILE),
            locale="fr-FR",
            viewport={"width": 412, "height": 915},
        )
        page = await ctx.new_page()
        try:
            await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(4000)
            if await is_logged_in(page):
                log.info("Session valid")
                balance = await extract_balance(page)
                if balance:
                    await notify(f"💰 <b>الرصيد:</b> {balance} دج")
                    await ctx.close()
                    return balance
            await ctx.close()
        except Exception as e:
            log.warning("saved session failed: %s", e)
            try:
                await ctx.close()
            except Exception:
                pass

    # جلسة جديدة
    ctx = await browser.new_context(
        locale="fr-FR",
        viewport={"width": 412, "height": 915},
        user_agent=("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"),
    )
    page = await ctx.new_page()

    try:
        log.info("Opening %s", SIGNIN_URL)
        await page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)

        # ننتظرو input يظهر
        try:
            await page.wait_for_selector("input", timeout=15000)
        except Exception:
            pass

        captcha = await detect_captcha(page)
        if captcha:
            await page.screenshot(path="captcha.png", full_page=True)
            await send_photo("captcha.png", f"⚠️ كاين كابتشا: {captcha}")
            await notify("⛔ نحتاجو حل كابتشا باش نكملو.")
            await ctx.close()
            return None

        # نأخذو البيانات من المستخدم
        username = await ask_username()
        password = await ask_password()

        # ---- تعبئة اسم المستخدم ----
        await page.wait_for_timeout(500)
        ok_user = await fill_username(page, username)
        if not ok_user:
            await page.screenshot(path="no_user_field.png", full_page=True)
            await send_photo("no_user_field.png", "❌ ما لقيتش حقل اسم المستخدم")
            await ctx.close()
            return None

        # ---- تعبئة كلمة السر ----
        try:
            pwd_sel = "input[type='password']"
            await page.wait_for_selector(pwd_sel, timeout=10000)
            pwd = page.locator(pwd_sel).first
            await pwd.click()
            await pwd.fill("")
            await pwd.type(password, delay=40)
            log.info("Password filled")
        except Exception as e:
            await page.screenshot(path="no_pwd_field.png", full_page=True)
            await send_photo("no_pwd_field.png", "❌ ما لقيتش حقل كلمة السر")
            await notify(f"❌ خطأ: <code>{e}</code>")
            await ctx.close()
            return None

        await page.wait_for_timeout(1200)
        await page.screenshot(path="filled.png", full_page=True)
        await send_photo("filled.png", "📸 الصفحة بعد التعبئة")

        # ---- نضغطو Connexion ----
        try:
            btn = page.locator(
                "button:has-text('Connexion'), "
                "button:has-text('Se connecter'), "
                "button[type='submit']"
            ).first
            if await btn.count() > 0:
                # نستنى يكون enabled
                for _ in range(30):
                    d = await btn.get_attribute("disabled")
                    if d is None:
                        break
                    await page.wait_for_timeout(300)
                await btn.click()
                log.info("Clicked Connexion")
        except Exception as e:
            log.warning("Click failed: %s", e)
            try:
                await page.evaluate("""
                    () => {
                        const b = [...document.querySelectorAll('button')]
                            .find(x => /connexion|connecter/i.test(x.textContent||''));
                        if (b) { b.removeAttribute('disabled'); b.click(); }
                    }
                """)
                log.info("Clicked via JS")
            except Exception as e2:
                log.error("JS click failed: %s", e2)

        # ---- ننتظرو ----
        await page.wait_for_timeout(7000)
        try:
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass

        # ---- دخلنا؟ ----
        if not await is_logged_in(page):
            await page.screenshot(path="login_failed.png", full_page=True)
            await send_photo("login_failed.png", "❌ ما دخلناش")
            await notify(f"🔗 URL: <code>{page.url}</code>")
            await ctx.close()
            return None

        # ---- نحفظو الجلسة ----
        await ctx.storage_state(path=str(STATE_FILE))
        log.info("Session saved")

        # ---- صفحة الرصيد ----
        await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(7000)

        balance = await extract_balance(page)
        if balance:
            await notify(f"💰 <b>الرصيد:</b> {balance} دج")
            await ctx.close()
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
            await ctx.close()
            return None

    except Exception as e:
        log.exception("login flow error")
        await notify(f"❌ خطأ: <code>{e}</code>")
        await ctx.close()
        return None


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

    await notify("🚀 بوت Ooredoo جاهز.\nاكتب /check.")

    stop = asyncio.Event()
    try:
        await stop.wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
