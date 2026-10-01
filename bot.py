import asyncio
import json
import time
import os
from utils.logger import get_logger
from utils.helpers import human_delay, human_move
from utils.screenshot import take_screenshot

log = get_logger("CloudConsole")

class CloudConsole:
    """كلاس مسؤول عن تسجيل الدخول إلى Google Cloud Console."""

    def __init__(self, context):
        self.context = context
        self.username = None
        self.password = None
        self.user_id = None
        self.sender = None

    # ==================== تسجيل الدخول ====================

    async def login(self, username: str, password: str, user_id: int = None, sender=None):
        self.username = username
        self.password = password
        self.user_id = user_id
        self.sender = sender

        page = await self.context.new_page()
        log.info("🔐 بدء تسجيل الدخول إلى Cloud Console...")
        await page.goto("https://console.cloud.google.com", wait_until="domcontentloaded")
        await human_delay(3, 5)
        await self._shot(page, "1️⃣ فتح Cloud Console")

        try:
            for attempt in range(8):
                log.info(f"--- المحاولة {attempt + 1} ---")
                await human_delay(2, 3)
                log.info(f"URL الحالي: {page.url[:150]}")

                if await self._has_wrong_password_error(page):
                    await self._shot(page, "❌ كلمة سر غلط")
                    raise RuntimeError("❌ كلمة السر غير صحيحة.")

                if await self._has_verify_required(page):
                    await self._shot(page, "⚠️ Verify مطلوب")
                    raise RuntimeError("⚠️ Google تطلب التحقق (verify)")

                # ✅ 0. TOS / speedbump — الأولوية القصوى
                if await self._is_welcome_page(page):
                    log.info("📋 صفحة Welcome / TOS")
                    await self._shot(page, "3️⃣ صفحة Welcome")

                    clicked = await self._click_blue_button(page)
                    if clicked:
                        log.info(f"✅ تم تنفيذ الضغط على: {clicked}")

                        gone = False
                        for i in range(20):
                            await human_delay(2, 2)
                            if await self._is_tos_gone(page):
                                log.info(f"✅ TOS اختفت بعد ~{(i+1)*2}s")
                                gone = True
                                break
                            log.info(f"⏳ مازال TOS... ({(i+1)*2}s)")

                        if not gone:
                            log.warning("⚠️ TOS ما اختفتش — نكملو بالقوة")
                            await self._shot(page, "⚠️ TOS مازال")

                        await human_delay(5, 8)
                    else:
                        await self._shot(page, "❌ لم نجد الزر")
                        await self._send_report(page, "❌ لم نجد زر Accept في صفحة Welcome")
                        await human_delay(5, 8)
                    continue

                # ✅ 1. صفحة تسجيل الدخول
                if await self._is_signin_page(page):
                    log.info("🔑 صفحة Sign in")
                    await self._shot(page, "2️⃣ صفحة Sign in")
                    try:
                        await self._do_signin(page, self.username, self.password)
                        await human_delay(5, 8)
                    except Exception as e:
                        log.warning(f"⚠️ فشل تسجيل الدخول: {e}")
                        await self._shot(page, "❌ فشل Sign in")
                        await self._send_report(page, f"فشل Sign in: {str(e)[:200]}")
                        raise
                    continue

                # ✅ 2. الكونسول جاهز
                if await self._is_console_ready(page):
                    log.info("✅ وصلنا إلى Console!")
                    await self._shot(page, "4️⃣ تم الدخول إلى Google Cloud")
                    break

                log.warning("❓ صفحة غير معروفة — نستناو")
                await human_delay(3, 5)

            await self._wait_for_console(page, timeout=30000)
            log.info(f"✅ URL النهائي: {page.url}")
            return page

        except Exception as e:
            log.error(f"فشل تسجيل الدخول: {e}")
            await self._shot(page, "❌ فشل")
            raise

    # ==================== أدوات المساعدة ====================

    async def _shot(self, page, caption: str = ""):
        try:
            from telegram import InputFile
            shot = await take_screenshot(page, caption[:30] if caption else "shot")
            if not shot or not os.path.exists(shot):
                return
            if self.sender:
                try:
                    with open(shot, "rb") as f:
                        await self.sender.reply_photo(
                            photo=InputFile(f),
                            caption=caption[:1000]
                        )
                except Exception as e:
                    log.warning(f"تعذّر إرسال الصورة: {e}")
        except Exception as e:
            log.warning(f"_shot: {e}")

    async def _send_report(self, page, reason: str):
        try:
            info = await page.evaluate("""
                () => {
                    const url = window.location.href.substring(0, 300);
                    const title = document.title || '';
                    const text = (document.body.innerText || '').substring(0, 1000).replace(/\\n+/g, ' | ');

                    const buttons = [];
                    for (const el of document.querySelectorAll('button, a, [role="button"], input[type="submit"]')) {
                        if (el.offsetParent === null) continue;
                        const rect = el.getBoundingClientRect();
                        const bg = window.getComputedStyle(el).backgroundColor;
                        const t = (el.innerText || el.value || '').trim();
                        if (t && t.length < 80) {
                            buttons.push({
                                text: t.substring(0, 80),
                                tag: el.tagName,
                                id: el.id || '',
                                cls: (el.className || '').toString().substring(0, 60),
                                bg: bg,
                                x: Math.round(rect.x + rect.width / 2),
                                y: Math.round(rect.y + rect.height / 2),
                                disabled: el.disabled || false,
                            });
                        }
                    }

                    const inputs = [];
                    for (const el of document.querySelectorAll('input, textarea, select')) {
                        if (el.offsetParent === null) continue;
                        const rect = el.getBoundingClientRect();
                        inputs.push({
                            tag: el.tagName,
                            type: el.type || '',
                            name: el.name || '',
                            id: el.id || '',
                            aria: el.getAttribute('aria-label') || '',
                            placeholder: el.placeholder || '',
                            value: (el.value || '').substring(0, 30),
                            formcontrolname: el.getAttribute('formcontrolname') || '',
                            x: Math.round(rect.x + rect.width / 2),
                            y: Math.round(rect.y + rect.height / 2),
                        });
                    }

                    const iframes = document.querySelectorAll('iframe').length;

                    return { url, title, text, buttons, inputs, iframes };
                }
            """)

            try:
                os.makedirs("/app/data/records", exist_ok=True)
                path = f"/app/data/records/error_{int(time.time())}.json"
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"reason": reason, **info}, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

            msg = "🔴 *تقرير المشكل*\n\n"
            msg += f"📋 *السبب:* {reason}\n\n"
            msg += f"🔗 *URL:*\n`{info.get('url', '')[:200]}`\n\n"
            msg += f"📄 *العنوان:* `{info.get('title', '')[:80]}`\n\n"
            msg += f"📝 *النص:*\n```{info.get('text', '')[:300]}```\n\n"

            msg += f"🔘 *الأزرار ({len(info.get('buttons', []))}):*\n"
            for b in info.get("buttons", [])[:15]:
                msg += f"  • `{b['text']}` — bg:`{b['bg']}` @({b['x']},{b['y']})\n"

            msg += f"\n📝 *الحقول ({len(info.get('inputs', []))}):*\n"
            for i in info.get("inputs", [])[:15]:
                msg += f"  • type=`{i['type']}` name=`{i['name']}` aria=`{i['aria'][:30]}`\n"

            msg += f"\n🖼️ *iframes:* {info.get('iframes', 0)}"

            if self.sender:
                try:
                    for i in range(0, len(msg), 3500):
                        try:
                            await self.sender.reply_text(msg[i:i+3500], parse_mode="Markdown")
                        except Exception:
                            await self.sender.reply_text(msg[i:i+3500])
                except Exception:
                    pass

        except Exception as e:
            log.warning(f"_send_report: {e}")

    # ==================== دوال الفحص ====================

    async def _is_signin_page(self, page) -> bool:
        """هل نحن في صفحة تسجيل الدخول؟ (ماشي TOS)"""
        try:
            url = page.url.lower()

            # ❌ TOS / speedbump → ماشي signin
            if "workspacetermsofservice" in url or "speedbump" in url:
                return False

            # ✅ accounts.google.com (بدون TOS) → signin
            if "accounts.google.com" in url:
                return True

            if "signin" in url:
                return True

            has_input = await page.evaluate("""
                () => {
                    for (const inp of document.querySelectorAll('input')) {
                        if (inp.offsetParent === null) continue;
                        if (inp.type === 'email' || inp.name === 'identifier' || inp.type === 'password') {
                            return true;
                        }
                    }
                    return false;
                }
            """)
            return bool(has_input)
        except Exception:
            return False

    async def _is_welcome_page(self, page) -> bool:
        """هل نحن في صفحة الترحيب / شروط الخدمة؟"""
        try:
            url = page.url.lower()

            # ✅ 1. TOS من URL — الأولوية القصوى
            if "workspacetermsofservice" in url or "speedbump" in url:
                return True

            # ✅ 2. accounts.google.com غير TOS → ماشي welcome
            if "accounts.google.com" in url:
                return False

            # ✅ 3. ما فيهش حقل إيميل؟
            has_email = await page.evaluate("""
                () => {
                    for (const inp of document.querySelectorAll('input')) {
                        if (inp.offsetParent === null) continue;
                        if (inp.type === 'email' || inp.name === 'identifier') return true;
                    }
                    return false;
                }
            """)
            if has_email:
                return False

            # ✅ 4. نشوفو النص والأزرار
            info = await page.evaluate("""
                () => {
                    const text = (document.body.innerText || '').toLowerCase();
                    const has_welcome = text.includes('welcome to your new account') ||
                                        (text.includes('terms of service') && text.includes('i understand'));
                    let has_understand = false;
                    for (const el of document.querySelectorAll('button, [role="button"]')) {
                        if (el.offsetParent === null) continue;
                        const t = (el.innerText || '').trim().toLowerCase();
                        if (t.includes('i understand') || t.includes('agree and continue')) {
                            has_understand = true;
                            break;
                        }
                    }
                    return { has_welcome, has_understand };
                }
            """)
            return bool(info.get("has_welcome") or info.get("has_understand"))
        except Exception:
            return False

    async def _has_wrong_password_error(self, page) -> bool:
        try:
            content = (await page.content()).lower()
            return any(txt in content for txt in ('incorrect password', 'wrong password'))
        except Exception:
            return False

    async def _has_verify_required(self, page) -> bool:
        try:
            content = (await page.content()).lower()
            return any(txt in content for txt in
                       ('verify it', 'verify your', 'enter the code', '2-step'))
        except Exception:
            return False

    async def _is_console_ready(self, page) -> bool:
        url = page.url
        if "console.cloud.google.com" not in url:
            return False
        if "signin" in url.lower() or "accounts.google.com" in url:
            return False
        if "/welcome" in url.lower() or "/new" in url.lower():
            return False

        try:
            text = (await page.inner_text("body")).lower()
            if "welcome to your new account" in text:
                return False
        except Exception:
            pass

        return "project=" in url or "/home/" in url

    # ==================== الضغط على الزر الأزرق ====================

    async def _click_blue_button(self, page) -> str:
        """
        يضغط على الزر الأزرق مرة واحدة فقط بضغطة mouse حقيقية.
        يرجع نص الزر إذا نجح، None إذا لم يجد الزر.
        """
        log.info("🔍 نبحث عن الزر الأزرق...")

        target = await page.evaluate("""
            () => {
                const kws = ['i understand', 'agree and continue', 'accept'];
                for (const el of document.querySelectorAll('button, a, [role="button"], input[type="submit"]')) {
                    if (el.offsetParent === null || el.disabled) continue;
                    const t = (el.innerText || el.value || '').trim().toLowerCase();
                    for (const kw of kws) {
                        if (t.includes(kw)) {
                            const rect = el.getBoundingClientRect();
                            const bg = window.getComputedStyle(el).backgroundColor;
                            return {
                                text: (el.innerText || el.value || '').trim(),
                                x: Math.round(rect.x + rect.width / 2),
                                y: Math.round(rect.y + rect.height / 2),
                                w: Math.round(rect.width),
                                h: Math.round(rect.height),
                                bg: bg,
                            };
                        }
                    }
                }
                return null;
            }
        """)

        if not target:
            log.warning("⚠️ ما لقيناش زر بالـ JS")
            return None

        log.info(f"🎯 الزر: '{target['text']}' @({target['x']},{target['y']}) size={target['w']}x{target['h']} bg={target['bg']}")

        # ============================================
        # 🖱️ ضغطة mouse حقيقية واحدة
        # ============================================
        try:
            await page.evaluate(f"""
                () => {{
                    const el = document.elementFromPoint({target['x']}, {target['y']});
                    if (el) el.scrollIntoView({{block: 'center', behavior: 'instant'}});
                }}
            """)
            await human_delay(0.6, 1.0)

            # إعادة حساب الإحداثيات بعد scroll
            target2 = await page.evaluate("""
                () => {
                    const kws = ['i understand', 'agree and continue', 'accept'];
                    for (const el of document.querySelectorAll('button, a, [role="button"]')) {
                        if (el.offsetParent === null || el.disabled) continue;
                        const t = (el.innerText || el.value || '').trim().toLowerCase();
                        for (const kw of kws) {
                            if (t.includes(kw)) {
                                const rect = el.getBoundingClientRect();
                                return {
                                    x: Math.round(rect.x + rect.width / 2),
                                    y: Math.round(rect.y + rect.height / 2),
                                };
                            }
                        }
                    }
                    return null;
                }
            """)
            if target2:
                target['x'] = target2['x']
                target['y'] = target2['y']
                log.info(f"📍 الإحداثيات بعد scroll: ({target['x']},{target['y']})")

            # حركة إنسانية
            await page.mouse.move(target['x'] - 150, target['y'] - 80, steps=12)
            await human_delay(0.2, 0.4)
            await page.mouse.move(target['x'] - 50, target['y'] - 20, steps=10)
            await human_delay(0.2, 0.4)
            await page.mouse.move(target['x'], target['y'], steps=8)
            await human_delay(0.4, 0.7)
            await page.mouse.move(target['x'], target['y'] + 1, steps=3)
            await human_delay(0.2, 0.4)

            # الضغطة
            await page.mouse.down()
            await human_delay(0.08, 0.15)
            await page.mouse.up()

            log.info("✅ mouse click تنفذ — نرجعو النجاح مباشرة")
            return target['text']

        except Exception as e:
            log.warning(f"❌ mouse click فشل: {e}")

        # ============================================
        # fallback: Playwright click
        # ============================================
        log.info("🖱️ fallback: Playwright click")
        for sel in [
            'button:has-text("I understand")',
            'button:has-text("Agree and continue")',
            'button:has-text("Accept")',
        ]:
            try:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.scroll_into_view_if_needed()
                    await human_delay(0.5, 1)
                    await el.click(force=True, timeout=5000)
                    log.info(f"✅ fallback نجح: {sel}")
                    return target['text']
            except Exception as e:
                log.warning(f"fallback {sel}: {e}")

        # ============================================
        # fallback أخير: JS click
        # ============================================
        log.info("🖱️ fallback: JS click")
        try:
            clicked = await page.evaluate("""
                () => {
                    const kws = ['i understand', 'agree and continue', 'accept'];
                    for (const el of document.querySelectorAll('button, a, [role="button"]')) {
                        if (el.offsetParent === null || el.disabled) continue;
                        const t = (el.innerText || '').trim().toLowerCase();
                        for (const kw of kws) {
                            if (t.includes(kw)) {
                                el.click();
                                return t;
                            }
                        }
                    }
                    return null;
                }
            """)
            if clicked:
                log.info(f"✅ JS click: {clicked}")
                return target['text']
        except Exception as e:
            log.warning(f"JS click: {e}")

        return None

    # ==================== Check TOS gone ====================

    async def _is_tos_gone(self, page) -> bool:
        """يتحقق واش خرجنا من TOS."""
        try:
            # ✅ 1. إذا زر TOS مازال موجود → مازال فـ TOS
            still_has_button = await page.evaluate("""
                () => {
                    const kws = ['i understand', 'agree and continue'];
                    for (const el of document.querySelectorAll('button, a, [role="button"]')) {
                        if (el.offsetParent === null || el.disabled) continue;
                        const t = (el.innerText || '').trim().toLowerCase();
                        for (const kw of kws) {
                            if (t.includes(kw)) return true;
                        }
                    }
                    return false;
                }
            """)
            if still_has_button:
                return False

            # ✅ 2. URL فيه speedbump/workspacetermsofservice → مازال
            url = page.url.lower()
            if "speedbump" in url or "workspacetermsofservice" in url:
                return False

            # ✅ 3. URL رجع لـ console → خلاص
            if "console.cloud.google.com" in url:
                return True

            # ✅ 4. النص ما بقاش فيه TOS
            try:
                text = (await page.inner_text("body")).lower()
                if "welcome to your new account" not in text and "i understand" not in text:
                    return True
            except Exception:
                pass

            return False
        except Exception as e:
            log.warning(f"_is_tos_gone: {e}")
            return False

    # ==================== خطوات تسجيل الدخول ====================

    async def _do_signin(self, page, username: str, password: str):
        # ---------- الإيميل ----------
        email_filled = False
        for sel in (
            'input[type="email"]',
            'input[type="text"][name="identifier"]',
            'input[name="identifier"]',
            'input[type="text"]',
        ):
            try:
                el = page.locator(sel).first
                if await el.count() == 0 or not await el.is_visible():
                    continue
                log.info(f"✅ حقل الإيميل: {sel}")
                await el.click()
                await human_delay(0.5, 1.0)
                await el.fill("")
                await human_delay(0.2, 0.5)
                await el.fill(username)
                await human_delay(0.5, 1.0)
                if (await el.input_value()).strip():
                    email_filled = True
                    break
                await el.click()
                await page.keyboard.type(username, delay=60)
                await human_delay(0.5, 1.0)
                if (await el.input_value()).strip():
                    email_filled = True
                    break
            except Exception:
                continue

        if not email_filled:
            raise RuntimeError("تعذّر كتابة البريد الإلكتروني")

        await self._click_next(page, "email")
        await human_delay(4, 6)

        # ---------- CAPTCHA ----------
        try:
            from automation.captcha_solver import has_captcha, detect_and_solve_captcha
            for _ in range(5):
                await human_delay(3, 5)
                if await has_captcha(page):
                    log.info("🚨 CAPTCHA ظهر")
                    await self._shot(page, "🚨 CAPTCHA")
                    solved = await detect_and_solve_captcha(page)
                    if solved:
                        log.info(f"✅ CAPTCHA: {solved}")
                        await human_delay(4, 6)
                        break
                else:
                    break
        except Exception as e:
            log.warning(f"CAPTCHA: {e}")

        # ---------- كلمة السر ----------
        await human_delay(3, 5)
        pwd_filled = False
        for sel in ('input[type="password"]', 'input[name="password"]'):
            try:
                el = page.locator(sel).first
                if await el.count() == 0 or not await el.is_visible():
                    continue
                log.info(f"✅ حقل كلمة السر: {sel}")
                await el.click()
                await human_delay(0.5, 1.0)
                await el.fill("")
                await human_delay(0.2, 0.5)
                await el.fill(password)
                await human_delay(0.5, 1.0)
                if (await el.input_value()).strip():
                    pwd_filled = True
                    break
                await el.click()
                await page.keyboard.type(password, delay=60)
                await human_delay(0.5, 1.0)
                if (await el.input_value()).strip():
                    pwd_filled = True
                    break
            except Exception:
                continue

        if not pwd_filled:
            raise RuntimeError("تعذّر كتابة كلمة السر")

        await self._click_next(page, "password")
        await human_delay(8, 12)
        log.info("✅ تم إدخال الإيميل وكلمة السر")

    async def _click_next(self, page, step: str):
        for sel in (
            '#identifierNext', '#passwordNext', '#captchaNext',
            'button:has-text("Next")', 'button[type="submit"]',
        ):
            try:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    return
            except Exception:
                continue

    async def _wait_for_console(self, page, timeout: int = 30000):
        log.info("⏳ انتظار تحميل Console...")
        try:
            await page.wait_for_function(
                """() => {
                    const url = window.location.href;
                    return url.includes('console.cloud.google.com') &&
                           !url.includes('signin') &&
                           !url.includes('accounts.google.com');
                }""",
                timeout=timeout,
            )
        except Exception:
            log.warning("⏱️ انتهت مدة انتظار Console")
        await human_delay(3, 5)
