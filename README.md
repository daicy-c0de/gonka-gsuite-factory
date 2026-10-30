# gonka-gsuite-factory

Account factory buat **gonkarouter.io** signup via Google (gsuite / akun gmail biasa)
atau email signup dengan temp mail. Menghasilkan `email|auth_token|api_key`.

Dua mode jalan:
1. **Telegram bot** — inline flow, live status per akun
2. **CLI** — tanpa Telegram, langsung terminal

## Yang diperluin
- Python 3.10+
- `pip install -r requirements.txt`
- Playwright chromium: `playwright install chromium`
- Camoufox (untuk mode email signup): `python -m camoufox fetch`
- Akun Google lu sendiri (jangan pune orang lain)

## Jalan CLI (tanpa Telegram)
```bash
# Signup via akun Google
python3 app.py --email lu@gmail.com --password 'secret' --proxy user:pass@host:port

# Signup via temp mail (tanpa google), 3 akun paralel + proxy resi
python3 app.py --tempmail --count 3 --proxy-resi residential.txt

# Hasil di akun.txt: email|auth_token|api_key
```

## Jalan Telegram bot
```bash
cp .env.example .env
# isi BOT_TOKEN (bot lu sendiri dari @BotFather) + AUTH_USERS (telegram id lu)
python3 app.py --tg
```
Flow: paste akun google (email|password per baris) → pilih jumlah thread →
pilih proxy → live status per step → hasil file .txt.

## Struktur
```
gonkafarm/
├── __init__.py
├── config.py          # env/.env loader + RESI_POOL_FILE / RESULT_FILE / PROGRESS_DIR
├── google_factory.py  # worker: Google OAuth flow (playwright chromium)
├── email_factory.py   # worker: email signup + OTP (camoufox)
├── telegram.py        # Telegram bot (python-telegram-bot)
└── cli.py             # argparse + worker threads
app.py                 # entry: CLI (--email/--password/--tempmail) atau bot (--tg)
```

## Environment variables (.env)
| Var | Wajib | Keterangan |
|---|---|---|
| BOT_TOKEN | utk TG mode | token bot lu |
| AUTH_USERS | utk TG mode | Telegram ID lu (koma buat multi) |
| RESI_POOL_FILE | opsional | file proxy residential (email signup mode) |
| RESULT_FILE | opsional | output file (default gonka_email_accounts.txt) |
| PROGRESS_DIR | opsional | folder live status (default ./progress) |

## Disclaimer
Buat edukasi / testing di akun & infrastruktur lu sendiri. Jangan dipake
buat abuse. Gonkarouter ToS mungkin dilarang — resiko tanggung sendiri.
