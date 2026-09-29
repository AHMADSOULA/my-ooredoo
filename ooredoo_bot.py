import asyncio
import os
import re
import logging
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright
from telegram import Bot

# ---------------- إعدادات ----------------
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OOREDOO_USER = os.getenv("OOREDOO_USER")
OOREDOO_PASS = os.getenv("OOREDOO_PASS")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "3600"))  # كل ساعة
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"

LOGIN_URL = "https://my.ooredoo.dz"
STATE_FILE = Path("state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ooredoo")


# ---------------- Telegram ----------------
async def notify(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured, message skipped: %s", msg)
        return
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=msg,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.error("Telegram error: %s", e)


# ---------------- Captcha detection ----------------
async def detect_captcha(page) -> str | None:
    checks = {
        "recaptcha_v2": "iframe[src*='google.com/recaptcha/api2/anchor']",
        "recaptcha_v3": "script[src*='recaptcha/api.js?render']",
        "hcaptcha": "iframe[src*='hcaptcha.com']",
        "turnstile": "iframe[src*='challenges.cloudflare.com']",
        "image_captcha": "img[src*='captcha' i]",
        "input_captcha": "input[name*='captcha' i]",
    }
    for name, sel in checks.items():
        try:
            if await page.locator(sel).count() > 0:
                return name
        except Exception:
            continue
    return None


# ---------------- Balance extraction ----------------
async def extract_balance(page) -> str | None:
    # 1) البحث في نص الصفحة
    try:
        body = await page.inner_text("body")
        patterns = [
            r"(?:solde|balance|رصيد|الرصيد)\D{0,25}([\d]+[.,][\d]{1,2})",
            r"([\d]+[.,][\d]{1,2})\s*(?:DA|دج|DZD)",
        ]
        for p in patterns:
            m = re.search(p, body, re.IGNORECASE)
            if m:
                return m.group(1).replace(",", ".")
    except Exception:
        pass

    # 2) selectors محتملة
    selectors = [
        "[data-testid*='balance' i]",
        ".balance", ".balance-value", ".solde",
        "text=/solde|balance|رصيد/i",
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


# ---------------- Login flow ----------------
async def login_and_get_balance(browser) -> str | None:
    # جلسة محفوظة إن كانت موجودة
    if STATE_FILE.exists():
        context = await browser.new_context(
            storage_state=str(STATE_FILE),
            locale="ar-DZ",
            viewport={"width": 412, "height": 915},
        )
    else:
        context = await browser.new_context(
            locale="ar-DZ",
            viewport={"width": 412, "height": 915},
            user_agent=(
                "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"
            ),
        )

    page = await context.new_page()

    try:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)

        logged_in = (
            "login" not in page.url.lower()
            and "auth" not in page.url.lower()
            and "signin" not in page.url.lower()
        )

        if not logged_in:
            # 1) كابتشا؟
            captcha = await detect_captcha(page)
            if captcha:
                await page.screenshot(path="captcha.png", full_page=True)
                await notify(
                    f"⚠️ <b>كاين كابتشا</b> في صفحة الدخول: <code>{captcha}</code>\n"
                    f"البوت موقوف حتى نضيف الحل التلقائي."
                )
                await context.close()
                return None

            # 2) تعبئة الحقول
            try:
                await page.fill(
                    "input[name='username'], input[name='msisdn'], "
                    "input[type='tel'], input[name='phone'], input[name='email']",
                    OOREDOO_USER,
                    timeout=15000,
                )
                await page.click(
                    "button:has-text('Suivant'), button:has-text('التالي'), "
                    "button[type='submit']"
                )
                await page.wait_for_timeout(2000)

                if await page.locator("input[type='password']").count() > 0:
                    await page.fill("input[type='password']", OOREDOO_PASS)
                    await page.click(
                        "button[type='submit'], "
                        "button:has-text('Se connecter'), "
                        "button:has-text('دخول'), button:has-text('تسجيل الدخول')"
                    )

                await page.wait_for_load_state("networkidle", timeout=45000)
            except Exception as e:
                await page.screenshot(path="login_error.png", full_page=True)
                await notify(f"❌ فشل تسجيل الدخول: <code>{e}</code>")
                await context.close()
                return None

            # حفظ الجلسة
            await context.storage_state(path=str(STATE_FILE))

        # 3) الرصيد
        await page.wait_for_timeout(3000)
        balance = await extract_balance(page)

        if balance:
            await notify(f"💰 <b>الرصيد:</b> {balance} دج")
            await context.close()
            return balance
        else:
            await page.screenshot(path="no_balance.png", full_page=True)
            await notify(
                "⚠️ دخلنا بنجاح لكن ما لقيتش الرصيد تلقائياً.\n"
                "شوف <code>no_balance.png</code> وأرسللي الـ selector."
            )
            await context.close()
            return None

    except Exception as e:
        await notify(f"❌ خطأ عام: <code>{e}</code>")
        await context.close()
        return None


# ---------------- Main loop ----------------
async def run_once():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",
            ],
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
