#!/usr/bin/env python3
"""GonkaRouter EMAIL SIGNUP factory — proxy resi + temp mail (mail.tm) + camoufox.
Flow (verified): modal → type email → engagement → Continue → OTP dialog →
poll mail.tm → type OTP → verify → auth_token cookie.
"""
import json
import random
import re
import sys
import time

import requests
from camoufox.sync_api import Camoufox

RESI_POOL_FILE = os.environ.get("RESI_POOL_FILE", "residential_alive.txt")
RESULT_FILE = os.environ.get("RESULT_FILE", "gonka_email_accounts.txt")


def load_resi_pool():
    try:
        lines = open(RESI_POOL_FILE).read().splitlines()
        return [l.split("|")[0].strip() for l in lines if "://" in l]
    except Exception:
        return []


def proxy_alive(proxy):
    try:
        r = requests.get("https://www.cloudflare.com/cdn-cgi/trace",
                         proxies={"http": proxy, "https": proxy}, timeout=10)
        return "ip=" in r.text
    except Exception:
        return False


def new_temp_email():
    r = requests.get("https://api.mail.tm/domains", timeout=15).json()
    domains = r if isinstance(r, list) else r.get("hydra:member", [])
    domain = [d["domain"] for d in domains if d.get("isActive")][0]
    import string
    user = "gonka_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    addr = f"{user}@{domain}"
    pw = "TempMail" + "".join(random.choices(string.ascii_letters, k=6)) + "!"
    requests.post("https://api.mail.tm/accounts", json={"address": addr, "password": pw}, timeout=15)
    tok = requests.post("https://api.mail.tm/token", json={"address": addr, "password": pw}, timeout=15).json()["token"]
    return addr, tok


def poll_otp(mail_token, timeout_s=180):
    """Poll inbox, cari email dari gonka/cloudflare, return kode 6 digit."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            r = requests.get("https://api.mail.tm/messages",
                             headers={"Authorization": f"Bearer {mail_token}"}, timeout=15)
            d = r.json()
            for m in d.get("hydra:member", []):
                mid = m["id"]
                full = requests.get(f"https://api.mail.tm/messages/{mid}",
                                    headers={"Authorization": f"Bearer {mail_token}"}, timeout=15).json()
                body = full.get("text") or ""
                if not body and isinstance(full.get("html"), list) and full["html"]:
                    body = full["html"][0]
                codes = re.findall(r"\b(\d{6})\b", body)
                if codes:
                    return codes[0]
                # ada email tapi no 6-digit — dump buat debug
                print(f"  [mail] from={m.get('from', {}).get('address')} subj={m.get('subject', '')[:40]} no-code", flush=True)
        except Exception as e:
            print("  poll err:", repr(e)[:60], flush=True)
        time.sleep(6)
    return None


def make_account(proxy=None):
    out = {"proxy": proxy, "email": None, "token": None, "error": None}
    try:
        email, mail_token = new_temp_email()
        out["email"] = email
        print(f"[+] email: {email}", flush=True)
        print(f"[+] proxy: {proxy or 'direct'}", flush=True)

        kw = {"headless": "virtual", "geoip": True, "humanize": True,
              "locale": ["en-US"], "window": (1366, 768),
              "i_know_what_im_doing": True}
        if proxy:
            kw["proxy"] = {"server": proxy}

        with Camoufox(**kw) as b:
            p = b.new_page()
            p.goto("https://gonkarouter.io", wait_until="domcontentloaded", timeout=60000)
            p.wait_for_timeout(5000)
            # buka modal sign-in
            p.evaluate("""() => {
                const bs = [...document.querySelectorAll('button,a')].filter(e =>
                    e.offsetParent !== null && /sign in/i.test(e.textContent));
                (bs[bs.length-1] || bs[0]).click();
            }""")
            p.wait_for_timeout(3000)
            # type email NATIVE (React-safe)
            inp = p.locator('[role=dialog] input[type=email], [role=dialog] input').first
            inp.click()
            p.keyboard.type(email, delay=45 + random.randint(0, 25))
            # engagement signal: mouse move + klik (kunci lolos challenge CF)
            p.mouse.move(400, 320); p.wait_for_timeout(300)
            p.mouse.move(660, 420); p.wait_for_timeout(500)
            p.mouse.click(660, 420); p.wait_for_timeout(2000)
            # klik Continue — text exact match, retry sampai dialog pindah ke OTP
            # (hydration timing: first click sering gak ke-register, attempt 2-3 masuk)
            for attempt in range(6):
                p.evaluate("""() => {
                    const bs = [...document.querySelectorAll('[role=dialog] button')].filter(e =>
                        e.offsetParent !== null && e.textContent.trim() === 'Continue');
                    if (bs[0]) bs[0].click();
                }""")
                p.wait_for_timeout(5000)
                dlg = p.evaluate("[...document.querySelectorAll('[role=dialog]')].map(d=>d.innerText.slice(0,120))")
                if any("verification code to" in d for d in dlg):
                    print(f"[+] OTP page reached (attempt {attempt+1})", flush=True)
                    break
                p.wait_for_timeout(2000)
            print("[+] submitted email", flush=True)
            p.wait_for_timeout(6000)
            dlg = p.evaluate("[...document.querySelectorAll('[role=dialog]')].map(d=>d.innerText.slice(0,150))")
            print(f"[+] dialog: {str(dlg)[:160]}", flush=True)
            if not any("verification code" in d for d in dlg):
                out["error"] = "email submit gagal — dialog gak pindah ke OTP"
                return out

            # poll OTP
            print("[*] waiting OTP email…", flush=True)
            code = poll_otp(mail_token)
            if not code:
                out["error"] = "no OTP in 180s"
                return out
            print(f"[+] OTP: {code}", flush=True)

            # type OTP — cari input OTP di dialog (bisa single atau multi-box)
            otp_inputs = p.locator('[role=dialog] input')
            n = otp_inputs.count()
            print(f"[+] OTP inputs: {n}", flush=True)
            if n == 1:
                otp_inputs.first.click()
                p.keyboard.type(code, delay=90)
            else:
                for idx in range(min(n, len(code))):
                    otp_inputs.nth(idx).click()
                    p.keyboard.type(code[idx], delay=60)
            p.wait_for_timeout(1500)
            # klik verify/continue
            p.evaluate("""() => {
                const bs = [...document.querySelectorAll('[role=dialog] button')].filter(e =>
                    e.offsetParent !== null && /continue|verify|submit/i.test(e.textContent.trim()));
                if (bs[0]) bs[0].click();
            }""")
            print("[+] submitted OTP, waiting auth_token…", flush=True)
            deadline = time.time() + 45
            while time.time() < deadline:
                cookies = p.context.cookies("https://gonkarouter.io")
                auth = next((c["value"] for c in cookies if c["name"] == "auth_token"), None)
                if auth:
                    out["token"] = auth
                    print(f"[+] SUCCESS auth_token len={len(auth)}", flush=True)
                    return out
                p.wait_for_timeout(1500)
            dlg = p.evaluate("[...document.querySelectorAll('[role=dialog]')].map(d=>d.innerText.slice(0,200))")
            out["error"] = f"no auth_token after OTP; dialog={str(dlg)[:150]}"
            return out
    except Exception as e:
        out["error"] = repr(e)[:200]
        return out


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    pool = [p for p in load_resi_pool() if proxy_alive(p)]
    print(f"[+] alive resi proxy: {len(pool)}", flush=True)
    if not pool:
        print("[-] no alive proxy", flush=True)
        return
    ok = fail = 0
    for i in range(n):
        proxy = pool[i % len(pool)]
        print(f"=== account {i+1}/{n} ===", flush=True)
        r = make_account(proxy)
        if r.get("token"):
            ok += 1
            with open(RESULT_FILE, "a") as f:
                f.write(f"{r['email']}|{r['token']}|{proxy}\n")
        else:
            fail += 1
            print(f"[-] FAIL: {r.get('error')}", flush=True)
        time.sleep(random.randint(5, 12))
    print(f"[DONE] ok={ok} fail={fail}", flush=True)


if __name__ == "__main__":
    main()
