"""Server version: chạy trên Oracle Cloud Linux, không cần Selenium/Chrome.
Đọc creds từ creds.json (extract từ máy nhà bằng `auto_tool.py dump-creds`).
"""
import hashlib
import hmac
import json
import os
import secrets
import socket as _socket
import ssl
import string
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time as dt_time, timedelta

API_HOST = "app.caffiliate.vn"
API_PATH = "/api/v2/xeng/check-in-secure"
CHECKIN_API_URL = f"https://{API_HOST}{API_PATH}"
STATUS_API_URL = f"https://{API_HOST}/api/v2/xeng/check-in/status"

API_BURST_COUNT = 12
API_BURST_SPACING_MS = 25

CREDS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "creds.json")
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def load_env():
    if not os.path.isfile(ENV_PATH):
        return
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")


def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT:
        print(f"[no-tg] {msg}")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": f"[Caffiliate-Oracle] {msg}"[:3900]}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as r:
            r.read()
    except Exception as e:
        print(f"TG fail: {e}")


def generate_nonce():
    return "".join(secrets.choice(string.digits + string.ascii_lowercase) for _ in range(12))


def sign_request(secret, ts, nonce, uid):
    return hmac.new(secret.encode(), f"{ts}.{nonce}.{uid}".encode(), hashlib.sha256).hexdigest()


def build_headers(creds):
    ts = str(int(time.time() * 1000))
    nonce = generate_nonce()
    sig = sign_request(creds["xeng_secret"], ts, nonce, creds["user_id"])
    return {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://app.caffiliate.vn",
        "Referer": "https://app.caffiliate.vn/rewards",
        "User-Agent": creds["user_agent"],
        "Cookie": creds["cookies"],
        "x-csrf-token": creds["csrf_token"],
        "x-signature": sig,
        "x-timestamp": ts,
        "x-nonce": nonce,
    }


def post_checkin(creds):
    req = urllib.request.Request(CHECKIN_API_URL, data=b"{}", headers=build_headers(creds), method="POST")
    sent = datetime.now()
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return sent, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        try:
            return sent, json.loads(body)
        except Exception:
            return sent, {"success": False, "httpError": e.code, "body": body}
    except Exception as e:
        return sent, {"success": False, "error": str(e)[:300]}


def measure_latency(creds):
    req = urllib.request.Request(STATUS_API_URL, headers={
        "Cookie": creds["cookies"], "User-Agent": creds["user_agent"], "Accept": "*/*"
    }, method="GET")
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            r.read()
        return (time.time() - start) * 1000
    except Exception:
        return None


def get_status(creds):
    req = urllib.request.Request(STATUS_API_URL, headers={
        "Cookie": creds["cookies"], "User-Agent": creds["user_agent"], "Accept": "*/*"
    }, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            d = json.loads(r.read())
        return d.get("data") if d.get("success") else None
    except Exception:
        return None


def wait_next_midnight(offset_ms=0):
    target = datetime.combine(datetime.now().date() + timedelta(days=1), dt_time()) + timedelta(milliseconds=offset_ms)
    while True:
        rem = (target - datetime.now()).total_seconds()
        if rem <= 0:
            return
        time.sleep(min(rem * 0.5, 0.1) if rem > 0.1 else 0.0001)


def main():
    with open(CREDS_PATH, encoding="utf-8") as f:
        creds = json.load(f)
    send_telegram(f"Bắt đầu phiên ngày {datetime.now().strftime('%Y-%m-%d')}")

    try:
        _socket.getaddrinfo(API_HOST, 443)
    except Exception:
        pass

    # Đo RTT 3 mẫu
    wait_next_midnight(offset_ms=-5000)
    samples = []
    for _ in range(3):
        ms = measure_latency(creds)
        if ms is not None:
            samples.append(ms)
        time.sleep(0.5)

    if samples:
        max_rtt = max(samples)
        offset = max(-2500, min(-50, -460 - int(max_rtt * 2.5)))
        send_telegram(f"📡 RTT {[int(s) for s in samples]}ms → offset {offset}ms")
    else:
        offset = -700
        send_telegram(f"⚠️ RTT đo fail, dùng offset {offset}ms")

    wait_next_midnight(offset_ms=offset)

    # Parallel burst
    _socket.setdefaulttimeout(3)
    results = [None] * API_BURST_COUNT

    def fire(idx):
        time.sleep(idx * API_BURST_SPACING_MS / 1000.0)
        sent, payload = post_checkin(creds)
        recv = datetime.now()
        latency = (recv - sent).total_seconds() * 1000
        results[idx] = (idx, sent, payload, latency, recv)

    threads = [threading.Thread(target=fire, args=(i,), daemon=True) for i in range(API_BURST_COUNT)]
    for t in threads:
        t.start()
    deadline = time.time() + 10
    for t in threads:
        rem = deadline - time.time()
        if rem <= 0:
            break
        t.join(timeout=rem)

    successes = [r for r in results if r and r[2].get("success")]
    if successes:
        successes.sort(key=lambda r: r[1])
        idx, sent, payload, _, recv = successes[0]
        data = payload.get("data", {})
        latency = (recv - sent).total_seconds() * 1000
        status = get_status(creds)
        pos = status.get("todayCheckInPosition") if status else None
        pos_str = f" | 🏆 top {pos}" if pos else ""
        send_telegram(f"✅ streak={data.get('streakDay')} +{data.get('rewardValue')}{pos_str} | burst #{idx+1} | fire {sent.strftime('%H:%M:%S.%f')[:-3]} | {latency:.0f}ms")
    else:
        done = [r for r in results if r]
        first_err = "no thread done"
        if done:
            p = done[0][2]
            first_err = p.get("message") or p.get("error") or "fail"
        send_telegram(f"❌ Burst x{API_BURST_COUNT} fail ({len(done)}/{API_BURST_COUNT}): {first_err}")


if __name__ == "__main__":
    main()
