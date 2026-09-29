import asyncio
import os
import re
import logging
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright
from telegram import Bot

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OOREDOO_USER = os.getenv("OOREDOO_USER")
OOREDOO_PASS = os.getenv("OOREDOO_PASS")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "3600"))
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"

SIGNIN_URL = "https://my.ooredoo.dz/sign-in"
DASHBOARD_URL = "https://my.ooredoo.dz/dashboard/my-ooredoo"
STATE_FILE = Path("state.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ooredoo")


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


async def detect_captcha(page) -> str | None:
    checks = {
        "recaptcha_v2": "iframe[src*='google.com/recaptcha/api2/anchor']",
        "hcaptcha": "iframe[src*='hcaptcha.com']",
        "turnstile": "iframe[src*='challenges.cloudflare.com']",
    }
    for name, sel in checks.items():
        try:
            if await page.locator(sel).count() > 0:
                return name
        except Exception:
            continue
    return None


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
    """نتحققو فعلياً واش دخلنا — ماشي بالـ URL فقط"""
    # إذا لقينا زر تسجيل الخروج أو قائمة الحساب، دخلنا
    try:
        if "/dashboard" in page.url:
            return True
    except Exception:
        pass
    # نتحققو من وجود صفحة تسجيل الدخول
    try:
        if await page.locator("input[type='password']").count() > 0:
            return False
        if await page.locator("text=Connectez-vous").count() > 0 and \
           await page.locator("input[type='number']").count() > 0:
            return False
    except Exception:
        pass
    return True


async def login_and_get_balance(browser) -> str | None:
    # نجربو نستعملو الجلسة المحفوظة
    if STATE_FILE.exists():
        log.info("Loading saved session")
        context = await browser.new_context(
            storage_state=str(STATE_FILE),
            locale="fr-FR",
            viewport={"width": 412, "height": 915},
        )
    else:
        context = await browser.new_context(
            locale="fr-FR",
            viewport={"width": 412, "height": 915},
            user_agent=("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"),
        )

    page = await context.new_page()

    try:
        # 1) نروحو مباشرة لصفحة /sign-in
        log.info("Opening %s", SIGNIN_URL)
        await page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(4000)

        # إذا الجلسة صالحة، راح يوجهنا لـ dashboard
        if "/dashboard" in page.url:
            log.info("Session still valid")
            await notify("ℹ️ استعملنا جلسة محفوظة")
        else:
            # 2) كابتشا؟
            captcha = await detect_captcha(page)
            if captcha:
                await page.screenshot(path="captcha.png", full_page=True)
                await send_photo("captcha.png", f"⚠️ كاين كابتشا: {captcha}")
                await notify("البوت موقوف حتى نضيف حل الكابتشا.")
                await context.close()
                return None

            # 3) نعبئو اسم المستخدم / كلمة السر
            # من HTML، /sign-in يستعمل حقول مشابهة
            await page.screenshot(path="before_login.png", full_page=True)
            await send_photo("before_login.png", "📸 صفحة /sign-in قبل التعبئة")

            try:
                # اسم المستخدم
                user_sel = ("input[name='username'], input[name='userName'], "
                            "input[id*='username' i], input[type='text'], "
                            "input[type='number'], input[type='tel']")
                await page.wait_for_selector(user_sel, timeout=15000)
                await page.fill(user_sel, OOREDOO_USER)
                log.info("Filled username")

                # كلمة السر
                pwd_sel = "input[type='password']"
                if await page.locator(pwd_sel).count() == 0:
                    # يمكن في صفحة تانية، نضغطو على زر المتابعة
                    try:
                        await page.click("button[type='submit'], "
                                         "button:has-text('Continuer'), "
                                         "button:has-text('Suivant'), "
                                         "button:has-text('تسجيل')", timeout=8000)
                        await page.wait_for_timeout(2500)
                    except Exception:
                        pass

                if await page.locator(pwd_sel).count() > 0:
                    await page.fill(pwd_sel, OOREDOO_PASS)
                    log.info("Filled password")

                # زر الدخول
                try:
                    await page.click("button[type='submit'], "
                                     "button:has-text('Se connecter'), "
                                     "button:has-text('Connexion'), "
                                     "button:has-text('دخول'), "
                                     "button:has-text('تسجيل الدخول')",
                                     timeout=10000)
                    log.info("Clicked login")
                except Exception as e:
                    log.warning("Login button click failed: %s", e)

                # ننتظرو
                await page.wait_for_timeout(5000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=30000)
                except Exception:
                    pass

            except Exception as e:
                await page.screenshot(path="login_error.png", full_page=True)
                await send_photo("login_error.png", "❌ فشل تعبئة الدخول")
                await notify(f"❌ فشل تسجيل الدخول: <code>{e}</code>")
                await context.close()
                return None

            # 4) نحفظو الجلسة إذا دخلنا
            if await is_logged_in(page):
                log.info("Login successful, saving state")
                await context.storage_state(path=str(STATE_FILE))
            else:
                await page.screenshot(path="login_failed.png", full_page=True)
                await send_photo("login_failed.png", "❌ الصفحة بعد محاولة الدخول")
                await notify(f"❌ ما دخلناش. URL: <code>{page.url}</code>")
                await context.close()
                return None

        # 5) نروحو لصفحة الرصيد
        log.info("Going to dashboard")
        await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(6000)

        # 6) نستخرجو الرصيد
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
        await notify(f"❌ خطأ عام: <code>{e}</code>")
        await context.close()
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
    log.info("Bot started. Interval=%ss", CHECK_INTERVAL)
    await notify("🚀 بوت Ooredoo تشغّل.")

    while True:
        try:
            await run_once()
        except Exception as e:
            log.exception("run_once failed")
            await notify(f"❌ خطأ في الدورة: <code>{e}</code>")

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
