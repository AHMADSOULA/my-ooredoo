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


# ================== Telegram helpers ==================
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


# ================== Telegram handlers ==================
async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return
    await update.message.reply_text("🚀 بوت Ooredoo جاهز.\nاكتب /check لجلب الرصيد.")


async def check_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return
    await update.message.reply_text("⏳ راح نطلب منك الرقم ثم كلمة السر...")
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
        await update.message.reply_text("✅ توصلت بالرقم. أرسل الآن كلمة السر.")
        return

    if WAITING["password"] and not WAITING["password"].done():
        WAITING["password"].set_result(text)
        try:
            await update.message.delete()
        except Exception:
            pass
        await update.message.reply_text("✅ توصلت بكلمة السر. جاري المحاولة...")
        return

    await update.message.reply_text("ℹ️ اكتب /check لجلب الرصيد.")


async def ask_username() -> str:
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    WAITING["username"] = fut
    await notify("📱 أرسل <b>رقم الهاتف</b> (مثال: <code>0553372434</code>):")
    try:
        result = await asyncio.wait_for(fut, timeout=300)
    except asyncio.TimeoutError:
        WAITING["username"] = None
        raise Exception("انتهت المدة (5 دقائق) بلا رد")
    WAITING["username"] = None
    return result


async def ask_password() -> str:
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    WAITING["password"] = fut
    await notify("🔒 أرسل <b>كلمة السر</b>:")
    try:
        result = await asyncio.wait_for(fut, timeout=300)
    except asyncio.TimeoutError:
        WAITING["password"] = None
        raise Exception("انتهت المدة (5 دقائق) بلا رد")
    WAITING["password"] = None
    return result


# ================== Captcha ==================
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


# ================== Extraction ==================
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


# ================== Phone format variants ==================
def phone_variants(raw: str) -> list[str]:
    """نولّدو كل الصيغ الممكنة للرقم"""
    digits = re.sub(r"\D", "", raw or "")
    variants = []

    # 1) كما هو
    if raw:
        variants.append(raw.strip())

    # 2) +213 + local بدون 0
    if digits.startswith("0") and len(digits) >= 10:
        local = digits[1:]
        variants.append(f"+213{local}")
        variants.append(f"213{local}")

    # 3) إذا بدا بـ 213
    if digits.startswith("213"):
        rest = digits[3:]
        variants.append(f"+{digits}")
        variants.append(digits)
        variants.append(f"0{rest}")

    # 4) إذا بدا بـ 05...
    if digits.startswith("05"):
        rest = digits[1:]
        variants.append(f"+213{rest}")
        variants.append(f"213{rest}")

    # تنظيف + إزالة التكرار مع الحفاظ على الترتيب
    seen = set()
    out = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


# ================== Field filling (Vue friendly) ==================
async def _fill_field(loc, value: str):
    await loc.scroll_into_view_if_needed()
    await loc.click()
    await loc.fill("")
    await loc.press_sequentially(value, delay=60)
    await loc.press("Tab")


async def fill_username(page, username: str) -> bool:
    """نلقاو حقل اسم المستخدم ونعبّيوه"""
    # 1) عن طريق label
    try:
        xp = ("xpath=//label[contains(., \"Nom d'utilisateur\")]"
              "/following::input[1]")
        inp = page.locator(xp).first
        if await inp.count() > 0:
            await _fill_field(inp, username)
            log.info("Username filled via label")
            return True
    except Exception as e:
        log.warning("label strategy failed: %s", e)

    # 2) iteration على inputs
    try:
        all_inputs = page.locator("input:visible")
        count = await all_inputs.count()
        for i in range(count):
            inp = all_inputs.nth(i)
            t = await inp.get_attribute("type") or "text"
            ph = (await inp.get_attribute("placeholder") or "").lower()
            if t == "password" or t in ("hidden", "submit", "button", "checkbox", "radio"):
                continue
            if any(x in ph for x in ("search", "recherche")):
                continue
            await _fill_field(inp, username)
            log.info("Username filled via input[%d]", i)
            return True
    except Exception as e:
        log.warning("iteration failed: %s", e)

    return False


async def fill_password(page, password: str) -> bool:
    try:
        sel = "input[type='password']"
        await page.wait_for_selector(sel, timeout=10000)
        pwd = page.locator(sel).first
        await _fill_field(pwd, password)
        log.info("Password filled")
        return True
    except Exception as e:
        log.warning("password fill failed: %s", e)
        return False


async def clear_field(loc):
    await loc.click()
    await loc.fill("")
    await loc.press("Tab")


async def click_connexion(page) -> bool:
    """يضغط زر Connexion، يستنى يكون enabled، وإلا force"""
    try:
        btn = page.locator(
            "button:has-text('Connexion'), "
            "button:has-text('Se connecter'), "
            "button[type='submit']"
        ).first
        if await btn.count() == 0:
            return False

        # نستنى 10 ثواني يكون enabled
        for _ in range(33):
            d = await btn.get_attribute("disabled")
            if d is None:
                break
            await page.wait_for_timeout(300)

        try:
            await btn.click(timeout=5000)
            log.info("Connexion clicked")
            return True
        except Exception:
            log.warning("normal click failed, forcing")
            await btn.click(force=True)
            log.info("Connexion clicked (force)")
            return True
    except Exception as e:
        log.error("click_connexion failed: %s", e)
        return False


# ================== Main login flow ==================
async def try_login_with_variant(
    page, phone: str, password: str, variant_index: int, total: int,
) -> bool:
    """يحاول يدخل بصيغة معينة. يرجع True إذا نجح."""
    log.info("Trying variant %d/%d: %s", variant_index, total, phone)
    await notify(f"🔁 محاولة {variant_index}/{total}: <code>{phone}</code>")

    # نمسحو الحقول
    try:
        user_field = page.locator(
            "xpath=//label[contains(., \"Nom d'utilisateur\")]/following::input[1]"
        ).first
        if await user_field.count() > 0:
            await clear_field(user_field)
    except Exception:
        pass

    try:
        pwd_field = page.locator("input[type='password']").first
        if await pwd_field.count() > 0:
            await clear_field(pwd_field)
    except Exception:
        pass

    # نعبّيو اسم المستخدم
    if not await fill_username(page, phone):
        log.error("Could not fill username")
        return False

    # نعبّيو كلمة السر
    if not await fill_password(page, password):
        log.error("Could not fill password")
        return False

    await page.wait_for_timeout(1200)

    # نصوّرو باش نشوفو الحالة
    shot_name = f"filled_v{variant_index}.png"
    await page.screenshot(path=shot_name, full_page=True)

    # نديرو Connexion
    if not await click_connexion(page):
        await send_photo(shot_name, f"❌ محاولة {variant_index}: الزر ما تلقاش")
        return False

    # نستناو النتيجة
    await page.wait_for_timeout(6000)
    try:
        await page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass

    # واش دخلنا؟
    if await is_logged_in(page):
        await send_photo(shot_name, f"✅ نجحت الصيغة: {phone}")
        return True
    else:
        # نصوّرو الخطأ
        err_name = f"failed_v{variant_index}.png"
        await page.screenshot(path=err_name, full_page=True)
        await send_photo(err_name, f"❌ فشلت الصيغة: {phone}")
        # نرجعو للصفحة
        try:
            await page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
        except Exception:
            pass
        return False


async def login_and_get_balance(browser) -> str | None:
    # 1) الجلسة المحفوظة
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
                log.info("Saved session valid")
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

    # 2) جلسة جديدة
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

        try:
            await page.wait_for_selector("input", timeout=15000)
        except Exception:
            pass

        # كابتشا؟
        captcha = await detect_captcha(page)
        if captcha:
            await page.screenshot(path="captcha.png", full_page=True)
            await send_photo("captcha.png", f"⚠️ كاين كابتشا: {captcha}")
            await notify("⛔ نحتاجو أداة حل الكابتشا.")
            await ctx.close()
            return None

        # نطلبو البيانات
        raw_phone = await ask_username()
        password = await ask_password()

        variants = phone_variants(raw_phone)
        if not variants:
            await notify("❌ الرقم فارغ")
            await ctx.close()
            return None

        log.info("Variants to try: %s", variants)

        success = False
        for idx, phone in enumerate(variants, 1):
            try:
                ok = await try_login_with_variant(
                    page, phone, password, idx, len(variants),
                )
                if ok:
                    success = True
                    break
            except Exception as e:
                log.exception("variant %d failed: %s", idx, e)
                await notify(f"❌ خطأ في المحاولة {idx}: <code>{e}</code>")

        if not success:
            await notify("❌ فشلت كل الصيغ. تحقق من الرقم وكلمة السر.")
            await ctx.close()
            return None

        # نحفظو الجلسة
        await ctx.storage_state(path=str(STATE_FILE))
        log.info("Session saved")

        # نروحو للرصيد
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
