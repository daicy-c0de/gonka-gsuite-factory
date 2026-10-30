#!/usr/bin/env python3
"""GonkaRouter Account Factory — Telegram bot.

Flow:
1. User paste akun Google per baris: email|password
2. Bot tanya jumlah thread (inline keyboard)
3. Bot tanya opsi proxy (inline keyboard)
4. Workers paralel (chromium headful di xvfb per worker)
5. Status live per-step via edit_message_text (1 message, no spam)
6. Hasil: document .txt email|auth_token|api_key

Run: python3 -m src.bot  (atau python3 src/bot.py)
"""
import os, re, json, time, threading, queue, io, subprocess, signal, asyncio, base64
import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

from gonkafarm.config import PROGRESS_DIR

_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

AUTHORIZED_USERS = {int(x) for x in os.environ.get("AUTH_USERS", "").split(",") if x.strip()}
FACTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "google_factory.py")
RESULTS_FILE = os.path.join(_DIR, "gonka_accounts.txt")
PROXY_FILE = os.path.join(_DIR, "proxy.txt")  # optional, isi proxy string

STEP_LABEL = {
    "open_site": "buka gonkarouter.io",
    "open_modal": "buka modal login",
    "click_google": "klik continue w/ google",
    "google_email": "isi email google",
    "google_password": "isi password",
    "accept_tos": "accept ToS google",
    "tos_500_wait": "retry ToS (500)",
    "wait_callback": "tunggu redirect",
    "logged_in": "login ok",
    "key_captured": "API key captured",
}

STOP_EVENT = threading.Event()
WORKER_PROCS = set()
WORKER_PROCS_LOCK = threading.Lock()
MAX_RETRIES = 2


def register_proc(proc):
    with WORKER_PROCS_LOCK:
        WORKER_PROCS.add(proc)


def unregister_proc(proc):
    with WORKER_PROCS_LOCK:
        WORKER_PROCS.discard(proc)


def kill_all_workers():
    STOP_EVENT.set()
    with WORKER_PROCS_LOCK:
        procs = list(WORKER_PROCS)
    for proc in procs:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    try:
        subprocess.run(["pkill", "-9", "-f", "gonka_factory.py"], timeout=10)
        subprocess.run(["pkill", "-f", "xvfb-run.*gonka_factory"], timeout=10)
    except Exception:
        pass


def parse_creds(text: str):
    out = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        email, pw = line.split("|", 1)
        email, pw = email.strip(), pw.strip()
        if "@" in email and len(pw) >= 4:
            out.append((email, pw))
    return out


def run_worker(email: str, password: str, proxy: str | None) -> dict:
    cmd = ["xvfb-run", "-a", "python3", FACTORY, email, password]
    if proxy:
        cmd.append(proxy)
    proc = None
    try:
        if STOP_EVENT.is_set():
            return {"key": None, "token": None, "error": "stopped"}
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        register_proc(proc)
        try:
            stdout, stderr = proc.communicate(timeout=420)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            proc.communicate()
            return {"key": None, "token": None, "error": "timeout"}
        finally:
            unregister_proc(proc)
        if STOP_EVENT.is_set():
            return {"key": None, "token": None, "error": "stopped"}
        for line in reversed((stdout or "").strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return {"key": None, "token": None,
                "error": f"no output: {(stdout or '')[-120:]} {(stderr or '')[-120:]}"}
    except Exception as e:
        return {"key": None, "token": None, "error": str(e)[:200]}


def live_step(email):
    try:
        pf = os.path.join(PROGRESS_DIR, re.sub(r"[^a-zA-Z0-9]", "_", email) + ".json")
        if os.path.exists(pf):
            with open(pf) as f:
                return json.load(f).get("step")
    except Exception:
        pass
    return None


def build_status_text(job) -> str:
    ok_n = sum(1 for c in job["results"] if c["status"] == "done" and c.get("token"))
    fail_n = sum(1 for c in job["results"] if c["status"] == "failed")
    lines = [f"GonkaRouter Factory — {job['threads']} thread | proxy: {'ya' if job.get('proxy') else 'nggak'}",
             f"{len(job['results'])} akun", ""]
    for i, c in enumerate(job["results"]):
        email = c["email"]
        if c["status"] == "done" and c.get("token"):
            lines.append(f"✅ {i+1}. {email}")
        elif c["status"] == "failed":
            lines.append(f"❌ {i+1}. {email} — {(c.get('error') or '')[:50]}")
        elif c["status"] == "running":
            st = live_step(email) or c.get("current_step")
            lines.append(f"🔄 {i+1}. {email} — {STEP_LABEL.get(st, st or 'start')}")
        else:
            lines.append(f"⏳ {i+1}. {email} — pending")
    lines.append("")
    lines.append(f"done {ok_n} | fail {fail_n} | total {len(job['results'])}")
    text = "\n".join(lines)
    if len(text) > 4000:
        summary = []
        for i, c in enumerate(job["results"]):
            email = c["email"]
            if c["status"] == "done" and c.get("token"):
                summary.append(f"✅ {i+1}. {email}")
            elif c.get("error"):
                summary.append(f"❌ {i+1}. {email} — {(c.get('error') or '')[:30]}")
            elif c["status"] == "running":
                summary.append(f"🔄 {i+1}. {email}")
            else:
                summary.append(f"⏳ {i+1}. {email}")
        text = "\n".join([lines[0], ""] + summary + ["", lines[-1]])
        while len(text) > 4000:
            summary = summary[1:]
            text = "\n".join([lines[0], ""] + summary + ["", lines[-1]])
    return text


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in AUTHORIZED_USERS:
        return
    await update.message.reply_text(
        "GonkaRouter Account Factory\n\n"
        "1. Kirim FILE proxy .txt (1 proxy per baris) — atau skip kalau ga mau proxy\n"
        "2. Paste akun Google per baris:\nemail|password\n\n"
        "Contoh akun:\nevandh1067@pesviv.com|qwertyasdzxc\n\n"
        "Bot tanya jumlah thread → pakai proxy (dari file yang barusan dikirim) → gas → hasil .txt\n"
        "Rotate: tiap attempt ambil proxy berikutnya, habis semua = stop.\n"
        "/stop — force stop semua worker"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in AUTHORIZED_USERS:
        return
    # ── 1) Kirim FILE .txt → dianggap daftar proxy, tulis ke proxy.txt
    if update.message.document:
        doc = update.message.document
        if not (doc.file_name or "").lower().endswith(".txt"):
            await update.message.reply_text("Harus file .txt (1 proxy per baris)")
            return
        tg_file = await doc.get_file()
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        try:
            text = buf.getvalue().decode("utf-8", errors="replace")
        except Exception:
            await update.message.reply_text("File ga bisa dibaca (bukan teks)")
            return
        lines = [l.split("|")[0].strip() for l in text.splitlines()]
        lines = [l for l in lines if l and not l.startswith("#")]
        if not lines:
            await update.message.reply_text("File kosong / ga ada proxy valid. 1 proxy per baris.")
            return
        with open(PROXY_FILE, "w") as f:
            f.write("\n".join(lines) + "\n")
        await update.message.reply_text(
            f"proxy.txt ditulis: {len(lines)} proxy. Tiap run nanti akan ditanya pakai proxy atau nggak.")
        return
    # ── 2) fallthrough ke parse creds di bawah
    creds = parse_creds(update.message.text or "")
    if not creds:
        await update.message.reply_text("Format salah. Per baris: email|password")
        return
    with lock:
        user_state[update.effective_user.id] = {"creds": creds, "running": False}
    n = len(creds)
    row = [InlineKeyboardButton(f"{t} thread", callback_data=f"threads:{t}")
           for t in (1, 2, 3) if t <= n]
    kb = [row] if row else []
    kb.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
    await update.message.reply_text(
        f"{n} akun terdeteksi. Pilih jumlah thread paralel (max 3):",
        reply_markup=InlineKeyboardMarkup(kb),
    )


def load_proxy_file():
    """Load proxy.txt — 1 proxy per baris. Format: user:pass@host:port | host:port | http://host:port"""
    if not os.path.exists(PROXY_FILE):
        return []
    out = []
    for line in open(PROXY_FILE):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        proxy = line.split("|")[0].strip()  # buang suffix |CM|... kalau ada
        if proxy:
            out.append(proxy)
    return out


async def threads_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    m = re.match(r"threads:(\d+)$", q.data or "")
    if not m:
        await q.answer()
        return
    n_threads = int(m.group(1))
    with lock:
        st = user_state.get(update.effective_user.id)
        if not st or st.get("running"):
            await q.answer("expired / sudah jalan", show_alert=True)
            return
        st["pending_threads"] = n_threads
    await q.answer()
    # next: proxy choice — file pool di-load saat pilih biar user tahu isinya
    nprox = len(load_proxy_file())
    kb = [
        [InlineKeyboardButton("Tanpa proxy", callback_data=f"proxy:none:{n_threads}")],
        [InlineKeyboardButton(f"Rotate proxy dari proxy.txt ({nprox} proxy)", callback_data=f"proxy:file:{n_threads}")],
        [InlineKeyboardButton("Cancel", callback_data="cancel")],
    ]
    try:
        await q.edit_message_text(
            f"{n_threads} thread dipilih. Pakai proxy?\n"
            f"Isi proxy.txt dulu (1 proxy/baris) kalau mau rotate — tiap attempt pakai proxy beda, "
            f"habis semua = stop.",
            reply_markup=InlineKeyboardMarkup(kb))
    except telegram.error.BadRequest:
        pass


async def proxy_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    m = re.match(r"proxy:(\w+):(\d+)$", q.data or "")
    if not m:
        await q.answer()
        return
    proxy_choice, n_threads = m.group(1), int(m.group(2))
    proxy_val = None
    if proxy_choice == "file":
        pool = load_proxy_file()
        if not pool:
            await q.answer("proxy.txt kosong / ga ada — isi dulu, 1 proxy per baris", show_alert=True)
            return
        proxy_val = pool  # list → dirotate per attempt di batch_worker

    with lock:
        st = user_state.get(update.effective_user.id)
        if not st or st.get("running"):
            await q.answer("expired / sudah jalan", show_alert=True)
            return
        st["running"] = True
        STOP_EVENT.clear()
        job = {
            "chat_id": q.message.chat_id,
            "user_id": update.effective_user.id,
            "threads": n_threads,
            "proxy": proxy_val,
            "results": [
                {"email": e, "password": p, "key": None, "token": None,
                 "error": None, "status": "pending", "current_step": None}
                for e, p in st["creds"]
            ],
            "msg_id": None,
        }
        st["job"] = job

    await q.answer(f"gas {n_threads} thread")
    await q.edit_message_text(f"{len(job['results'])} akun × {n_threads} thread — mulai…")

    msg = await context.bot.send_message(job["chat_id"], build_status_text(job))
    job["msg_id"] = msg.message_id

    t = threading.Thread(target=batch_worker, args=(job,), daemon=True)
    t.start()

    prev = ""
    while True:
        await asyncio.sleep(4)
        txt = build_status_text(job)
        if txt != prev:
            prev = txt
            try:
                await context.bot.edit_message_text(
                    chat_id=job["chat_id"], message_id=job["msg_id"], text=txt)
            except telegram.error.BadRequest as e:
                if "not modified" not in str(e).lower():
                    print("edit fail:", e)
            except telegram.error.TimedOut:
                pass
            except Exception as e:
                print("edit err:", type(e).__name__, str(e)[:100], flush=True)
        if STOP_EVENT.is_set() or all(c["status"] in ("done", "failed") for c in job["results"]):
            break

    if STOP_EVENT.is_set():
        STOP_EVENT.clear()
        with WORKER_PROCS_LOCK:
            WORKER_PROCS.clear()

    ok = [c for c in job["results"] if c.get("token")]
    with open(RESULTS_FILE, "a") as f:
        for c in ok:
            f.write(f"{c['email']}|{c['token']}|{c.get('key') or '-'}\n")

    if ok:
        content = "\n".join(f"{c['email']}|{c['token']}|{c.get('key') or ''}" for c in ok)
        f = io.BytesIO(content.encode())
        f.name = "gonka_accounts.txt"
        await context.bot.send_document(job["chat_id"], f,
                                        caption=f"{len(ok)}/{len(job['results'])} akun sukses (email|auth_token|api_key)")

    with lock:
        if user_state.get(update.effective_user.id):
            user_state[update.effective_user.id]["running"] = False


def batch_worker(job):
    MAX_RETRIES = 1
    q = queue.Queue()
    for i in range(len(job["results"])):
        q.put(i)

    # Proxy pool dari proxy.txt — dirotate atomik per ATTEMPT (bukan per akun).
    # Kegagalan 1 akun dengan proxy X ga buang proxy X: akun lain coba lagi
    # di attempt berikutnya masih bisa dapat X (endpoint/resi bisa recover).
    proxy_pool = job.get("proxy")
    pool_i = [0]  # mutable container, index global — thread-safe via GIL

    def pick_proxy():
        if isinstance(proxy_pool, list) and proxy_pool:
            if pool_i[0] >= len(proxy_pool):
                return None  # pool habis — semua proxy udah dicoba
            p = proxy_pool[pool_i[0]]
            pool_i[0] += 1
            return p
        return proxy_pool  # single proxy atau None

    pool_exhausted = [False]  # flag global: pool habis → stop semua worker

    def mark_remaining_failed(job, reason):
        for c in job["results"]:
            if c["status"] not in ("done", "failed"):
                c["status"] = "failed"
                c["error"] = reason

    def worker():
        while True:
            if STOP_EVENT.is_set() or pool_exhausted[0]:
                return
            try:
                i = q.get_nowait()
            except queue.Empty:
                return
            c = job["results"][i]
            c["status"] = "running"
            proxy = pick_proxy()
            if proxy is None and isinstance(proxy_pool, list):
                # pool habis bahkan sebelum attempt pertama akun ini
                c["status"] = "failed"
                c["error"] = "proxy habis (semua proxy di file udah dicoba)"
                pool_exhausted[0] = True
                mark_remaining_failed(job, "proxy habis (semua proxy di file udah dicoba)")
                return
            try:
                r = run_worker(c["email"], c["password"], proxy)
            except Exception as e:
                r = {"key": None, "token": None, "error": f"worker crash: {type(e).__name__}: {str(e)[:150]}"}
            c.update(r)
            retries = 0
            while not r.get("token") and retries < MAX_RETRIES:
                if STOP_EVENT.is_set():
                    c["status"] = "failed"
                    c["error"] = "stopped"
                    return
                if pool_exhausted[0]:
                    c["status"] = "failed"
                    c["error"] = "proxy habis (semua proxy di file udah dicoba)"
                    return
                err = (r.get("error") or "").lower()
                if any(x in err for x in ("wrong password", "couldn't sign you in",
                                          "account has been disabled", "no sign-in modal",
                                          "google: account not found", "google: account deleted",
                                          "google: gsi/transform stuck",
                                          "google: unusual traffic",
                                          "google: recaptcha", "google: verify you're not",
                                          "signup_limit_reached")):
                    break
                retries += 1
                c["current_step"] = f"retry{retries}"
                proxy = pick_proxy()
                if proxy is None and isinstance(proxy_pool, list):
                    c["status"] = "failed"
                    c["error"] = "proxy habis (semua proxy di file udah dicoba)"
                    pool_exhausted[0] = True
                    mark_remaining_failed(job, "proxy habis (semua proxy di file udah dicoba)")
                    return
                r = run_worker(c["email"], c["password"], proxy)
                c.update(r)
                if not r.get("token") and retries < MAX_RETRIES:
                    time.sleep(30)
            c["status"] = "done" if r.get("token") else "failed"
            if not r.get("token"):
                print(f"[GONKA_FAIL] {c['email']} error={r.get('error','')[:200]} step={c.get('current_step','')}", flush=True)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(job["threads"])]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


async def cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("cancelled")
    with lock:
        user_state.pop(update.effective_user.id, None)
    try:
        await q.edit_message_text("cancel — paste ulang buat mulai lagi")
    except telegram.error.BadRequest:
        pass


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in AUTHORIZED_USERS:
        return
    uid = update.effective_user.id
    kill_all_workers()
    with lock:
        st = user_state.pop(uid, None)
    if st and st.get("job"):
        for c in st["job"]["results"]:
            if c["status"] not in ("done", "failed"):
                c["status"] = "failed"
                c["error"] = "stopped by user"
    await update.message.reply_text("🛑 force stop — semua worker dibunuh, job dibersihin")


user_state = {}
lock = threading.Lock()


async def on_error(update, context):
    print("bot error:", type(context.error).__name__, str(context.error)[:200], flush=True)


def run():
    bot_token = os.environ.get("BOT_TOKEN", "")
    if not bot_token:
        raise SystemExit("BOT_TOKEN belum diset (.env atau env var)")
    app = Application.builder().token(bot_token).build()
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.TXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(threads_cb, pattern=r"^threads:\d+$"))
    app.add_handler(CallbackQueryHandler(proxy_cb, pattern=r"^proxy:"))
    app.add_handler(CallbackQueryHandler(cancel_cb, pattern=r"^cancel$"))
    print("gonka factory bot running", flush=True)
    app.run_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    run()
