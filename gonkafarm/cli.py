"""CLI mode: bikin akun gonkarouter.io tanpa Telegram.

Contoh:
  python3 app.py --email you@gmail.com --password 'secret'               # 1 akun, no proxy
  python3 app.py --email you@gmail.com --password 'secret' --proxy host:port
  python3 app.py --tempmail --count 3 --proxy-resi residential.txt      # temp mail + proxy resi pool

Output: akun.txt (email|auth_token|api_key) + progress live di terminal.
"""
import argparse, json, os, subprocess, sys, threading, time

_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG = os.path.dirname(os.path.abspath(__file__))


def run_factory(email, password, proxy=None):
    cmd = [sys.executable, os.path.join(_PKG, 'google_factory.py'), email, password]
    if proxy:
        cmd.append(proxy)
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=_DIR)
    out = (r.stdout or '').strip().split('\n')[-1] if r.stdout else ''
    try:
        return json.loads(out)
    except Exception:
        return {'email': email, 'error': (r.stderr or 'unknown')[:200]}


def run_email_factory(proxy=None):
    cmd = [sys.executable, os.path.join(_PKG, 'email_factory.py')]
    if proxy:
        cmd.append(proxy)
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=_DIR)
    out = (r.stdout or '').strip().split('\n')[-1] if r.stdout else ''
    try:
        return json.loads(out)
    except Exception:
        return {'error': (r.stderr or 'unknown')[:200]}


def main(args=None):
    ap = argparse.ArgumentParser(description='gonkarouter.io x gsuite account factory (CLI)')
    ap.add_argument('--email', help='akun google email')
    ap.add_argument('--password', help='password google')
    ap.add_argument('--proxy', help='proxy user:pass@host:port / host:port / http://...')
    ap.add_argument('--tempmail', action='store_true', help='mode email signup (temp mail, tanpa google)')
    ap.add_argument('--count', type=int, default=1, help='jumlah akun (default 1)')
    ap.add_argument('--proxy-resi', dest='proxy_resi', help='file daftar proxy residential (1 per baris)')
    ap.add_argument('--out', default='akun.txt', help='file output (default akun.txt)')
    if args is None:
        args = ap.parse_args()

    if not args.tempmail and not (args.email and args.password):
        ap.error('--email + --password wajib (atau pakai --tempmail)')

    # load pool proxy resi
    proxies = [args.proxy] if args.proxy else []
    if args.proxy_resi:
        lines = open(args.proxy_resi).read().splitlines()
        proxies = [l.split('|')[0].strip() for l in lines if '://' in l]

    results = []
    lock = threading.Lock()

    def worker(i):
        email = args.email
        password = args.password
        if args.tempmail:
            r = run_email_factory(proxies[i % len(proxies)] if proxies else None)
        else:
            r = run_factory(email, password, proxies[i % len(proxies)] if proxies else None)
        with lock:
            results.append(r)
            line = f"{r.get('email', email)}|{r.get('token', '')}|{r.get('key', '')}"
            open(args.out, 'a').write(line + '\n')
            print(f"[{i+1}/{args.count}] {'OK' if r.get('key') else 'FAIL'} {r.get('email', email)}")

    threads = []
    for i in range(args.count):
        t = threading.Thread(target=worker, args=(i,))
        t.start()
        threads.append(t)
        time.sleep(1)  # stagger biar browser ga tabrak
    for t in threads:
        t.join()
    print(f"\n=== Selesai: {sum(1 for r in results if r.get('key'))}/{len(results)} sukses ===")
    print(f"Hasil: {args.out}")
