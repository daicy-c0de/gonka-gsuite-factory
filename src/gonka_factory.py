#!/usr/bin/env python3
"""GonkaRouter account factory worker.

Usage: gonka_factory.py email password [proxy]
Proxy optional: user:pass@host:port or host:port or none.
Output: last stdout line = JSON {email, token, key, key_id, error}
Steps emitted to progress/<email>.json for live telegram status.
"""
import time, re, os, sys, json
from playwright.sync_api import sync_playwright

PROGRESS_DIR = os.environ.get("PROGRESS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "progress"))


def parse_proxy(proxy):
    """user:pass@host:port | host:port:user:pass | host:port | http://host:port -> playwright proxy cfg."""
    if not proxy:
        return None
    if "@" in proxy:
        idx = proxy.index("@")
        creds, server = proxy[:idx], proxy[idx + 1:]
        user, pwd = creds.split(":", 1)
        return {"server": "http://" + server, "username": user, "password": pwd}
    parts = proxy.replace("http://", "").replace("https://", "").split(":")
    # host:port:user:pass (format cm-proxy / proxysystem) → pindah creds ke depan
    if len(parts) == 4:
        host, port, user, pwd = parts
        return {"server": f"http://{host}:{port}", "username": user, "password": pwd}
    if proxy.startswith("http"):
        return {"server": proxy}
    return {"server": "http://" + proxy}


def make_account(email: str, password: str, proxy: str | None = None) -> dict:
    result = {"email": email, "token": None, "key": None, "error": None}
    os.makedirs(PROGRESS_DIR, exist_ok=True)
    prog_path = os.path.join(PROGRESS_DIR, re.sub(r"[^a-zA-Z0-9]", "_", email) + ".json")

    def step(msg):
        try:
            with open(prog_path, "w") as f:
                json.dump({"email": email, "step": msg, "ts": time.time()}, f)
        except Exception:
            pass

    launch_kw = {"headless": False,
                 "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled",
                          "--ignore-certificate-errors", "--disable-http2"]}

    # Proxy at LAUNCH level: affects DNS + all connections from this Chromium process.
    # Context-level proxy only handles HTTP, not WebSocket/iframes — use launch proxy.
    if proxy and proxy.lower() not in ("none", ""):
        pc = parse_proxy(proxy)
        if pc:
            launch_kw["proxy"] = pc

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kw)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}, locale="en-US")
        page = ctx.new_page()

        # ── GSI popup pre-warm: Chromium + cm-proxy: nav POPUP pertama ke
        # accounts.google.com sering ERR_TIMED_OUT (direct goto sama context OK).
        # Warm-up page + JANGAN di-close, biar popup berikutnya bisa load.
        warm_page = None
        if proxy:
            try:
                warm_page = ctx.new_page()
                warm_page.goto("https://accounts.google.com", timeout=45000,
                               wait_until="domcontentloaded")
            except Exception:
                pass  # warmup gagal pun lanjut — biar flow normal yang decide

        # ── 429 signup_limit_reached detection on /auth/google-login.
        # GonkaRouter caps signups per-IP; when hit, auth_token never arrives.
        # Raise immediately (non-retryable, IP-level) instead of burning 95s.
        auth_limited = {"hit": False, "body": ""}
        def _on_resp(resp):
            try:
                if "auth/google-login" in resp.url and resp.status == 429:
                    auth_limited["hit"] = True
                    try:
                        auth_limited["body"] = resp.text()[:200]
                    except Exception:
                        pass
            except Exception:
                pass
        ctx.on("response", _on_resp)

        try:
            step("open_site")
            page.goto("https://gonkarouter.io/dashboard", wait_until="load", timeout=90000)
            time.sleep(5)

            step("open_modal")
            page.evaluate("""() => {
                const b = [...document.querySelectorAll('button')].filter(e =>
                    e.offsetParent !== null && e.textContent.trim() === 'Sign In');
                if (b.length) { b[b.length - 1].click(); return true; }
                return false;
            }""")
            time.sleep(3)
            if not page.evaluate("() => !!document.querySelector('input[placeholder=\"you@example.com\"]')"):
                raise RuntimeError("no sign-in modal")

            step("click_google")
            # expect_popup TIDAK dipakai: lewat proxy, nav popup pertama sering
            # chrome-error (ERR_TIMED_OUT) dan expect_popup balikin page error itu,
            # selector #identifierId ga pernah muncul. Klik + poll ctx.pages.
            # Catatan: page barusan di-warm-up ke accounts.google.com — GSI bisa
            # NAVIGATE page warm itu (redirect ke signin/identifier), bukan selalu
            # bikin popup baru. Cek warm_page juga.
            page.evaluate(
                "[...document.querySelectorAll('button')]"
                ".filter(e=>e.offsetParent!==null && e.textContent.includes('Google'))"
                ".slice(-1)[0].click()")
            popup = None
            for _poll in range(12):  # max 60s
                time.sleep(5)
                for _pg in ctx.pages:
                    if _pg is page:
                        continue
                    try:
                        _u = _pg.url or ""
                    except Exception:
                        continue
                    if "accounts.google.com" not in _u and "accounts.google.de" not in _u:
                        continue
                    if "signin/identifier" not in _u:
                        continue
                    try:
                        _pg.wait_for_selector("#identifierId", state="visible", timeout=5000)
                        popup = _pg
                        break
                    except Exception:
                        continue
                if popup is not None:
                    break
            if popup is None:
                raise RuntimeError("google popup tidak muncul (GSI nav timeout via proxy)")

            step("google_email")
            popup.locator("#identifierId").click()
            popup.keyboard.type(email, delay=45)
            time.sleep(1)
            popup.evaluate(
                "() => [...document.querySelectorAll('button,div[role=button]')]"
                ".filter(e => e.offsetParent !== null && e.textContent.trim().toLowerCase()==='next')"
                ".slice(-1)[0]?.click()")
            time.sleep(4)

            # Detect early aborts right after email step (before password field).
            # "Couldn't find this account" = akun ga ada/dihapus; "Account deleted" = barusan dihapus.
            try:
                _post = popup.evaluate("() => document.body.innerText.slice(0,500)") or ""
            except Exception:
                _post = ""
            _plow = _post.lower()
            if "couldn't find" in _plow or "couldn’t find" in _plow:
                raise RuntimeError(f"google: account not found | {email}")
            if "account deleted" in _plow:
                raise RuntimeError(f"google: account deleted | {email}")

            step("google_password")
            popup.wait_for_selector("input[type=password]:visible", timeout=25000)
            popup.locator("input[type=password]:visible").first.click()
            popup.keyboard.type(password, delay=40)
            time.sleep(1)
            popup.evaluate(
                "() => [...document.querySelectorAll('button,div[role=button]')]"
                ".filter(e => e.offsetParent !== null && e.textContent.trim().toLowerCase()==='next')"
                ".slice(-1)[0]?.click()")
            time.sleep(4)

            # Detect Google password errors — but ONLY on a live popup.
            # Popup closed / mid-navigation (auth success or transient) -> skip, let wait_callback grab cookie.
            try:
                _body = popup.evaluate("() => document.body ? document.body.innerText : ''") or ""
            except Exception:
                _body = ""
            if "Wrong password" in _body or ("Enter a password" in _body and "wrong" not in _body.lower()):
                raise RuntimeError("wrong password")
            # CAPTCHA / unusual-traffic / error pages — abort dengan jelas, jangan masuk ToS loop
            low = _body.lower()
            if any(x in low for x in ("captcha", "recaptcha", "verify you're not a robot",
                                       "unusual traffic", "couldn't sign you in",
                                       "this account has been disabled",
                                       "there was a problem with your sign-in",
                                       "sign-in error")):
                raise RuntimeError(f"popup_error: {_body[:80].replace(chr(10),' ')}")

            # ToS / welcome / 500-error loop; popup closing = auth done
            for k in range(14):
                try:
                    u = popup.url
                except Exception:
                    break
                if "accounts.google.com" not in u and "accounts.google.de" not in u:
                    break
                try:
                    body = popup.evaluate("() => document.body.innerText.slice(0,150)")
                except Exception:
                    break
                if "500" in body:
                    step(f"tos_500_wait{k}")
                    time.sleep(12)
                    popup.reload()
                    time.sleep(6)
                    continue
                step("accept_tos")
                popup.evaluate("""() => {
                    [...document.querySelectorAll('button,div[role=button],a')]
                        .filter(e => e.offsetParent !== null &&
                            /i understand|i agree|accept|continue|next|allow/i.test(e.textContent.trim()))
                        .slice(-1)[0]?.click();
                }""")
                time.sleep(4)

            page_alive = True
            popup_alive = True
            try:
                pu = popup.url
            except Exception:
                pu = ""
                popup_alive = False
            try:
                _ = page.url
            except Exception:
                page_alive = False

            # ── Diag snapshot: capture popup + cookie state for debugging
            SNAP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dbg")
            os.makedirs(SNAP_DIR, exist_ok=True)

            def diag_snapshot(tag=""):
                snap = {"tag": tag, "ts": time.time(), "popup_alive": popup_alive, "page_alive": page_alive}
                if popup_alive:
                    try:
                        snap["popup_url"] = popup.url[:120]
                    except Exception:
                        snap["popup_url"] = "<dead>"
                    try:
                        snap["popup_body"] = popup.evaluate(
                            "() => document.body ? document.body.innerText.slice(0,300) : ''"
                        )[:300]
                    except Exception:
                        snap["popup_error"] = "<evaluate_failed>"
                if page_alive:
                    try:
                        snap["page_url"] = page.url[:120]
                    except Exception:
                        snap["page_url"] = "<dead>"
                    try:
                        snap["page_body"] = page.evaluate(
                            "() => document.body ? document.body.innerText.slice(0,200) : ''"
                        )[:200]
                    except Exception:
                        pass
                # Screenshot popup + parent page
                for name, p, alive in [("popup", popup, popup_alive), ("parent", page, page_alive)]:
                    if alive:
                        try:
                            p.screenshot(path=os.path.join(SNAP_DIR, f"{email.replace('@','_').replace('.','_')}_{tag}_{name}.png"))
                        except Exception:
                            pass
                try:
                    cookies_full = ctx.cookies("https://gonkarouter.io")
                    snap["cookie_names"] = [c["name"] for c in cookies_full]
                    snap["has_auth_token"] = bool(next((c["value"] for c in cookies_full if c["name"] == "auth_token"), None))
                except Exception:
                    pass
                return snap

            step("wait_callback")
            # Popup closed = OAuth form_post/postMessage succeeded.
            # Cookie should be set on parent context. Poll for auth_token directly.
            # URL-based check removed: OAuth doesn't redirect the parent page.
            # First: quick check right after ToS loop in case cookie already landed.
            for _ in range(5):
                cookies = ctx.cookies("https://gonkarouter.io")
                tok = next((c["value"] for c in cookies if c["name"] == "auth_token"), None)
                if tok:
                    result["token"] = tok
                    step("logged_in")
                    break
                time.sleep(1)
            # Main polling loop — longer window, finer granularity.
            fail_snap = None
            gsi_stuck_start = None
            for _ in range(90):
                cookies = ctx.cookies("https://gonkarouter.io")
                tok = next((c["value"] for c in cookies if c["name"] == "auth_token"), None)
                if tok:
                    result["token"] = tok
                    step("logged_in")
                    break
                # also check if popup was closed (auth success but cookie not yet propagated)
                popup_alive = True
                try:
                    _ = popup.url  # still alive?
                except Exception:
                    popup_alive = False
                if not popup_alive:
                    # popup closed = auth done, wait longer for cookie to propagate
                    for _ in range(10):
                        time.sleep(1)
                        cookies = ctx.cookies("https://gonkarouter.io")
                        tok = next((c["value"] for c in cookies if c["name"] == "auth_token"), None)
                        if tok:
                            result["token"] = tok
                            step("logged_in")
                            break
                    if result.get("token"):
                        break
                    # popup dead AND no cookie — capture snapshot
                    if fail_snap is None:
                        try:
                            fail_snap = diag_snapshot("popup_dead_no_cookie")
                        except Exception:
                            fail_snap = {"tag": "popup_dead_no_cookie", "snap_err": True}
                    time.sleep(1)
                    # popup still alive — if it left Google entirely, cookie imminent
                    try:
                        pu = popup.url
                    except Exception:
                        pu = ""
                    if pu and "accounts.google.com" not in pu and "google" not in pu.lower():
                        if fail_snap is None and _ % 5 == 4:
                            try:
                                fail_snap = diag_snapshot("left_google_no_cookie")
                            except Exception:
                                pass
                    elif pu:
                        # ── GSI transform rate-limit gate: 60s di gsi/transform tanpa cookie = stuck.
                        # Jangan buang 95s x 5 retry. Last-chance: tunggu 60s total, lalu abort.
                        if "gsi/transform" in pu:
                            if gsi_stuck_start is None:
                                gsi_stuck_start = time.time()
                            elif time.time() - gsi_stuck_start > 60:
                                raise RuntimeError("google: gsi/transform stuck (rate-limited)")
                        else:
                            gsi_stuck_start = None
                    # ── also check body for other stuck states
                    try:
                        body = popup.evaluate("() => document.body.innerText.slice(0,300)") or ""
                    except Exception:
                        body = ""
                    low = body.lower()
                    if "couldn\xadt find" in low or "couldn't find" in low:
                        _s = diag_snapshot("account_not_found")
                        raise RuntimeError(f"google: account not found | snap={_s.get('popup_url','')[:60]}")
                    if "unusual traffic" in low or "verify you're not a robot" in low or "recaptcha" in low:
                        _s = diag_snapshot("google_block")
                        raise RuntimeError(f"google: {body[:60].strip()}")
                    if body and fail_snap is None and _ % 15 == 14:
                        fail_snap = diag_snapshot("stuck_on_google")
                time.sleep(1)
            if not result.get("token"):
                # Build informative error from snapshot
                if auth_limited["hit"]:
                    raise RuntimeError(
                        f"signup_limit_reached: IP kena limit signup GonkaRouter "
                        f"(butuh proxy/residential). {auth_limited['body'][:80]}")
                if fail_snap:
                    err_parts = []
                    if fail_snap.get("popup_error"):
                        err_parts.append(f"popup_dead:{fail_snap['popup_error']}")
                    else:
                        pu = fail_snap.get("popup_url","")
                        pb = fail_snap.get("popup_body","")
                        if pu: err_parts.append(f"popup_url={pu[:80]}")
                        if pb: err_parts.append(f"popup_body={pb[:100]}")
                    err_msg = f"wait_callback_fail[{fail_snap['tag']}]: {'; '.join(err_parts)}"
                else:
                    err_msg = f"no auth_token cookie after 95s, url={page.url[:100]}"
                raise RuntimeError(err_msg)

            # API key via in-browser fetch (carries CF cookies; urllib gets 403)
            step("fetch_key")
            data = None
            for _try in range(3):
                data = page.evaluate("""async (tok) => {
                    const r = await fetch('https://api.gonkarouter.io/api/keys',
                        {headers: {'Authorization': 'Bearer ' + tok}});
                    return await r.json();
                }""", tok)
                if (data or {}).get("items"):
                    break
                time.sleep(2)
            items = (data or {}).get("items") or []
            if not items:
                dbg = page.evaluate("""async (tok) => {
                    const r = await fetch('https://api.gonkarouter.io/api/keys',
                        {headers: {'Authorization': 'Bearer ' + tok}});
                    return {status: r.status, body: (await r.text()).slice(0, 200)};
                }""", tok)
                raise RuntimeError(f"no api key on account: {dbg}")
            kid = items[0]["id"]
            kd = page.evaluate("""async ([kid, tok]) => {
                const r = await fetch(`https://api.gonkarouter.io/api/keys/${kid}/reveal`,
                    {method: 'POST', headers: {'Authorization': 'Bearer ' + tok}});
                return await r.json();
            }""", [kid, tok])
            result["key"] = kd.get("key")
            result["key_id"] = kid
            step("key_captured")
        except Exception as e:
            result["error"] = str(e)[:300]
        finally:
            try:
                browser.close()
            except Exception:
                pass
    return result


if __name__ == "__main__":
    email, password = sys.argv[1], sys.argv[2]
    proxy = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3].lower() not in ("", "none") else None
    r = make_account(email, password, proxy)
    print(json.dumps(r))
