import asyncio
import os
import re
import logging
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"

SIGNIN_URL = "https://my.ooredoo.dz/sign-in"
DASHBOARD_URL = "https://my.ooredoo.dz/dashboard/my-ooredoo"
STATE_FILE = Path("state.json")

MAX_LOGIN_ATTEMPTS = 3  # عدد المحاولات عند الخطأ

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ooredoo")

WAITING = {"username": None, "password": None}


# ================== Telegram helpers ==================
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


# ================== Telegram handlers ==================
async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return
    await update.message.reply_text("🚀 بوت Ooredoo جاهز.\nاكتب /check.")


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
        await update.message.reply_text("✅ توصلت بكلمة السر. جاري الدخول...")
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


# ================== Error detection ==================
async def find_error_message(page) -> str | None:
    """يبحث عن رسائل الخطأ الشائعة"""
    try:
        # نبحث في النصوص عن كلمات مفتاحية
        body = await page.inner_text("body")
        patterns = [
            r"(identifiants?\s+incorrect[s]?[^\n]{0,100})",
            r"(mot de passe\s+incorrect[^\n]{0,100})",
            r"(nom d'utilisateur\s+incorrect[^\n]{0,100})",
            r"(utilisateur\s+non\s+trouvé[^\n]{0,100})",
            r"(erreur[^\n]{0,150})",
            r"(échec[^\n]{0,100})",
            r"(veuillez\s+réessayer[^\n]{0,100})",
            r"(réessayez[^\n]{0,100})",
            r"(خطأ[^\n]{0,100})",
            r"(كلمة السر[^\n]{0,80}غير صحيحة[^\n]{0,80})",
            r"(المستخدم[^\n]{0,80}غير موجود[^\n]{0,80})",
        ]
        for p in patterns:
            m = re.search(p, body, re.IGNORECASE)
            if m:
                return m.group(1).strip()

        # رسائل Vue-Toastification
        toast = page.locator(".Vue-Toastification__toast-body, .v-snackbar__content").first
        if await toast.count() > 0:
            t = await toast.inner_text()
            if t and t.strip():
                return t.strip()

        # رسائل v-messages
        err = page.locator(".v-messages__message, .v-input__details").first
        if await err.count() > 0:
            t = await err.inner_text()
            if t and t.strip() and len(t.strip()) > 3:
                return t.strip()
    except Exception as e:
        log.warning("find_error_message failed: %s", e)
    return None


async def wait_for_login_result(page, timeout_ms: int = 90000) -> dict:
    """
    يستنى حتى تكتمل العملية:
    - يختفي spinner
    - يتغير URL
    - أو تظهر رسالة خطأ
    يرجع dict فيه:
      - status: 'success' | 'error' | 'timeout'
      - error: نص الخطأ إن وجد
      - url: URL النهائي
    """
    result = {"status": "timeout", "error": None, "url": page.url}
    start = asyncio.get_event_loop().time()
    timeout_s = timeout_ms / 1000.0

    while (asyncio.get_event_loop().time() - start) < timeout_s:
        # 1) نجحنا؟
        if await is_logged_in(page):
            result["status"] = "success"
            result["url"] = page.url
            return result

        # 2) كاين خطأ؟
        err = await find_error_message(page)
        if err:
            result["status"] = "error"
            result["error"] = err
            result["url"] = page.url
            return result

        # 3) كاين spinner نشط؟
        try:
            spinner = page.locator(".v-overlay--active, .v-progress-circular--visible").first
            spinner_active = await spinner.count() > 0
        except Exception:
            spinner_active = False

        if not spinner_active:
            # نتأكدو مرة أخرى
            await page.wait_for_timeout(1500)
            if await is_logged_in(page):
                result["status"] = "success"
                result["url"] = page.url
                return result
            err = await find_error_message(page)
            if err:
                result["status"] = "error"
                result["error"] = err
                result["url"] = page.url
                return result

        await page.wait_for_timeout(1000)

    result["url"] = page.url
    return result


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

    for sel in ["[data-testid*='balance' i]", ".balance", ".balance-value", ".solde"]:
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
    """دخلنا إذا ماشين في /sign-in و/أو حقل كلمة السر ما بقاش ظاهر"""
    try:
        url = page.url
        if "/sign-in" in url or "/login" in url:
            # لكن نتأكدو بلي الحقل ماشي ظاهر
            if await page.locator("input[type='password']").count() > 0:
                return False
            # ممكن الصفحة في transition
            return False
        # إذا ماشي في sign-in، على الأغلب دخلنا
        return True
    except Exception:
        return False


# ================== Hide overlays ==================
async def hide_overlays(page, keep_spinner: bool = False):
    try:
        js = """
            (keepSpinner) => {
                const selectors = [
                    '.swiper', '.swiper-wrapper', '.swiper-slide',
                    '.cookie-banner', '.cookie-consent', '.cc-window',
                    '#onetrust-banner-sdk',
                    '.grecaptcha-badge',
                ];
                if (!keepSpinner) {
                    selectors.push('.v-overlay__scrim');
                }
                for (const sel of selectors) {
                    document.querySelectorAll(sel).forEach(el => {
                        el.style.display = 'none';
                        el.style.pointerEvents = 'none';
                    });
                }
            }
        """
        await page.evaluate(js, keep_spinner)
        log.info("Overlays hidden (keepSpinner=%s)", keep_spinner)
    except Exception as e:
        log.warning("hide_overlays failed: %s", e)


# ================== Fill helpers ==================
async def fill_username(page, username: str) -> bool:
    try:
        found = await page.evaluate("""
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
                    if (inputs.length > 0) target = inputs[0];
                }
                if (!target) return false;
                target.focus();
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                setter.call(target, val);
                target.dispatchEvent(new Event('input',  {bubbles: true}));
                target.dispatchEvent(new Event('change', {bubbles: true}));
                target.dispatchEvent(new Event('blur',   {bubbles: true}));
                target.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
                return true;
            }
        """, username)
        if found:
            log.info("Username filled via JS")
            return True
    except Exception as e:
        log.warning("JS username failed: %s", e)

    try:
        xp = "xpath=//label[contains(., \"Nom d'utilisateur\")]/following::input[1]"
        inp = page.locator(xp).first
        if await inp.count() > 0:
            await inp.scroll_into_view_if_needed()
            await inp.click(force=True, timeout=5000)
            await inp.fill("", force=True)
            await inp.press_sequentially(username, delay=50)
            await inp.press("Tab")
            log.info("Username filled via label (force)")
            return True
    except Exception as e:
        log.warning("label force failed: %s", e)
    return False


async def fill_password(page, password: str) -> bool:
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
                pwd.dispatchEvent(new Event('input',  {bubbles: true}));
                pwd.dispatchEvent(new Event('change', {bubbles: true}));
                pwd.dispatchEvent(new Event('blur',   {bubbles: true}));
                return true;
            }
        """, password)
        if ok:
            log.info("Password filled via JS")
            return True
    except Exception as e:
        log.warning("pwd JS failed: %s", e)

    try:
        pwd = page.locator("input[type='password']").first
        if await pwd.count() > 0:
            await pwd.scroll_into_view_if_needed()
            await pwd.click(force=True, timeout=5000)
            await pwd.fill("", force=True)
            await pwd.press_sequentially(password, delay=50)
            await pwd.press("Tab")
            log.info("Password filled (force)")
            return True
    except Exception as e:
        log.warning("pwd force failed: %s", e)
    return False


async def click_connexion(page) -> bool:
    try:
        ok = await page.evaluate("""
            () => {
                const btns = [...document.querySelectorAll('button')];
                const target = btns.find(b =>
                    /connexion|se connecter/i.test(b.textContent || '')
                );
                if (!target) return false;
                target.removeAttribute('disabled');
                target.click();
                return true;
            }
        """)
        if ok:
            log.info("Connexion clicked via JS")
            return True
    except Exception as e:
        log.warning("JS connexion failed: %s", e)

    try:
        btn = page.locator(
            "button:has-text('Connexion'), "
            "button:has-text('Se connecter'), "
            "button[type='submit']"
        ).first
        if await btn.count() > 0:
            await btn.scroll_into_view_if_needed()
            try:
                await btn.click(timeout=5000)
            except Exception:
                await btn.click(force=True)
            log.info("Connexion clicked (force)")
            return True
    except Exception as e:
        log.error("click_connexion failed: %s", e)
    return False


# ================== Main flow ==================
async def attempt_login(page, phone: str, password: str, attempt_num: int) -> dict:
    """محاولة واحدة دخول. يرجع {'status', 'error', 'url'}"""
    log.info("=== Attempt %d ===", attempt_num)
    await notify(f"🔁 محاولة {attempt_num}/{MAX_LOGIN_ATTEMPTS}")

    # نروحو لصفحة sign-in من جديد
    await page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(5000)
    await hide_overlays(page)

    # نعبّيو الحقول
    if not await fill_username(page, phone):
        return {"status": "error", "error": "ما قدرناش نعبّيو اسم المستخدم", "url": page.url}

    if not await fill_password(page, password):
        return {"status": "error", "error": "ما قدرناش نعبّيو كلمة السر", "url": page.url}

    await page.wait_for_timeout(1200)
    await page.screenshot(path=f"before_login_attempt{attempt_num}.png", full_page=True)

    # Connexion
    if not await click_connexion(page):
        return {"status": "error", "error": "ما لقيناش زر Connexion", "url": page.url}

    log.info("Connexion clicked, waiting for result...")
    await notify("⏳ جاري الانتظار حتى يكمل الدخول...")

    # ننتظرو النتيجة (سلسلة كاملة)
    result = await wait_for_login_result(page, timeout_ms=90000)
    log.info("Result: %s", result)

    # نصوّرو
    await page.screenshot(path=f"after_attempt{attempt_num}.png", full_page=True)

    return result


async def login_and_get_balance(browser) -> str | None:
    # 1) جلسة محفوظة
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

    # 2) جلسة جديدة
    ctx = await browser.new_context(
        locale="fr-FR",
        viewport={"width": 412, "height": 915},
        user_agent=("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"),
    )
    page = await ctx.new_page()

    try:
        # نطلب البيانات
        raw_phone = await ask_username()
        password = await ask_password()

        phone = (raw_phone or "").strip()
        if not phone:
            await notify("❌ الرقم فارغ")
            await ctx.close()
            return None

        # محاولات متعددة
        last_error = None
        for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
            try:
                result = await attempt_login(page, phone, password, attempt)
            except Exception as e:
                log.exception("attempt %d crashed", attempt)
                await notify(f"❌ خطأ في المحاولة {attempt}: <code>{e}</code>")
                last_error = str(e)
                continue

            status = result.get("status")
            error = result.get("error")

            if status == "success":
                await notify("✅ دخلنا بنجاح!")
                break

            if status == "error":
                last_error = error or "خطأ غير معروف"
                await notify(f"⚠️ <b>خطأ:</b> <code>{last_error}</code>")
                await page.screenshot(path=f"error_attempt{attempt}.png", full_page=True)
                await send_photo(f"error_attempt{attempt}.png", f"❌ محاولة {attempt}: {last_error}")

                if attempt < MAX_LOGIN_ATTEMPTS:
                    await notify("🔄 نعاود المحاولة...")
                    await page.wait_for_timeout(3000)
                    continue
                else:
                    await notify(f"❌ فشلت {MAX_LOGIN_ATTEMPTS} محاولات. آخر خطأ: {last_error}")
                    await ctx.close()
                    return None

            if status == "timeout":
                await notify(f"⏰ انتهت مدة المحاولة {attempt} بلا نتيجة.")
                if attempt < MAX_LOGIN_ATTEMPTS:
                    await notify("🔄 نعاود...")
                    await page.wait_for_timeout(3000)
                    continue
                else:
                    await notify("❌ فشلت كل المحاولات (timeout)")
                    await ctx.close()
                    return None

        # نحفظو الجلسة
        await ctx.storage_state(path=str(STATE_FILE))
        log.info("Session saved")

        # نروحو للرصيد
        await notify("✅ دخلنا. نجيب الرصيد...")
        await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(9000)
        await hide_overlays(page)

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
