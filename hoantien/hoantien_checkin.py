"""Điểm danh tự động cho hoantienshopee.me — gọi API trực tiếp, không Selenium.

Khác biệt quan trọng so với saffi:
  - Access token là JWT HẾT HẠN SAU ~15 PHÚT. Có `shop_refresh_token` (cookie) để lấy token
    mới. Task chạy 23:55 phải REFRESH token trước khi bắn lúc 00:00, nếu không sẽ 401.
  - `checkinDate` trả về là ngày phẳng "YYYY-MM-DD" (khỏi parse UTC như saffi).
  - status trả `today` + `checkedInToday` + `canCheckin` + `blockReason` rất rõ ràng.
  - Reward cố định (500đ/ngày, mốc 7 ngày +5000), KHÔNG có early-bird bonus → burst chủ yếu
    để đua rank leaderboard và để không bao giờ trượt.

Reset điểm danh: 00:00 giờ Việt Nam (ICT).

Cách chạy:
  python hoantien_checkin.py test     # điểm danh 1 phát ngay — kiểm tra creds
  python hoantien_checkin.py now      # burst ngay lập tức
  python hoantien_checkin.py run      # chờ tới 00:00 ICT rồi burst — dùng cho Task Scheduler
  python hoantien_checkin.py status   # xem tình trạng + leaderboard (không điểm danh)
  python hoantien_checkin.py refresh  # thử refresh access token rồi lưu lại creds.json

File deps (cùng thư mục, đã .gitignore):
  - creds.json : access_token, refresh_token, user_agent, [refresh_url]  (xem creds.example.json)
  - .env       : TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (tùy chọn)
"""

import base64
import json
import os
import random
import socket as _socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time as dt_time, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

API_HOST = "api.hoantienshopee.me"
SITE_HOST = "hoantienshopee.me"
CHECKIN_API_URL = f"https://{API_HOST}/checkin"
STATUS_API_URL = f"https://{API_HOST}/checkin/status"
LEADERBOARD_API_URL = f"https://{API_HOST}/checkin/leaderboard?limit=10"
TASKS_API_URL = f"https://{API_HOST}/tasks"  # GET → data.items[]; claim: POST /tasks/{id}/claim

# Lệnh `tasks-run` (cho Task Scheduler) ngủ ngẫu nhiên trong khoảng này trước khi claim,
# để mỗi ngày chạy một giờ khác nhau (tránh bị phát hiện bot). Chỉ 3–5 phút để không phải
# chờ lâu / không phải giữ cmd mở lâu.
TASKS_RANDOM_DELAY_MIN_S = 1 * 60  # tối thiểu 1 phút
TASKS_RANDOM_DELAY_MAX_S = 10 * 60  # tối đa 10 phút
# Endpoint refresh token — ĐÃ XÁC NHẬN hoạt động (server xoay refresh token qua Set-Cookie,
# code tự lưu lại vào creds.json). Ghi đè bằng creds.json["refresh_url"] nếu sau này site đổi.
REFRESH_API_URL_DEFAULT = f"https://{API_HOST}/auth/refresh"
# Đăng nhập cố định bằng email+mật khẩu (không cần Google/trình duyệt) — cách BỀN nhất để tự
# lấy token mới khi refresh chết. Cũng tính là "đăng nhập thật" nên thỏa mãn nhiệm vụ login_day.
LOGIN_API_URL_DEFAULT = f"https://{API_HOST}/auth/login"

# Burst trải qua mốc reset (giống saffi) — xem giải thích trong README.
BURST_LEAD_MS = 500
BURST_WINDOW_MS = 3000
BURST_INTERVAL_MS = 60

# Sau burst đầu, nếu chưa được thì poll NHẸ (tránh throttle) tới khi cửa mở / token ok.
POLL_GAP_MS = 4000  # 1 request mỗi 4s
POLL_MAX_SECONDS = 900  # kiên trì tới 15 phút

TOKEN_REFRESH_MARGIN_S = 180  # còn dưới ngần này giây là hết hạn → refresh trước

HERE = os.path.dirname(os.path.abspath(__file__))
CREDS_PATH = os.path.join(HERE, "creds.json")
ENV_PATH = os.path.join(HERE, ".env")


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
    print(msg)
    if not TG_TOKEN or not TG_CHAT:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    # Header thống nhất với caffiliate/saffi: emoji + site + giờ
    text = f"🛒 HOANTIEN · {datetime.now().strftime('%H:%M:%S')}\n{msg}"[:3900]
    data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as r:
            r.read()
    except Exception as e:
        print(f"TG fail: {e}")


# ---------- creds + token ----------


def load_creds():
    if not os.path.isfile(CREDS_PATH):
        sys.exit(f"Thiếu {CREDS_PATH}. Copy creds.example.json → creds.json rồi điền token.")
    with open(CREDS_PATH, encoding="utf-8") as f:
        creds = json.load(f)
    missing = [k for k in ("access_token", "refresh_token", "user_agent") if not creds.get(k)]
    if missing:
        sys.exit(f"creds.json thiếu field: {', '.join(missing)}")
    return creds


def save_creds(creds):
    with open(CREDS_PATH, "w", encoding="utf-8") as f:
        json.dump(creds, f, ensure_ascii=False, indent=2)


def jwt_exp(token):
    """Đọc claim `exp` (unix seconds) từ JWT mà không cần verify chữ ký."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("exp")
    except Exception:
        return None


def token_seconds_left(creds):
    exp = jwt_exp(creds.get("access_token", ""))
    if not exp:
        return None
    return exp - time.time()


def refresh_token(creds, verbose=True):
    """Lấy access token mới bằng refresh_token (cookie). Cập nhật creds + lưu creds.json.
    Trả về True nếu thành công. Endpoint/định dạng response CẦN XÁC NHẬN (xem README)."""
    url = creds.get("refresh_url") or REFRESH_API_URL_DEFAULT
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": f"https://{SITE_HOST}",
        "Referer": f"https://{SITE_HOST}/",
        "User-Agent": creds["user_agent"],
        "Cookie": f"shop_refresh_token={creds['refresh_token']}",
    }
    req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read())
            set_cookie = r.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as e:
        if verbose:
            send_telegram(f"❌ Refresh token FAIL: HTTP {e.code} @ {url}\n{e.read().decode(errors='replace')[:200]}")
        return False
    except Exception as e:
        if verbose:
            send_telegram(f"❌ Refresh token lỗi: {str(e)[:200]} @ {url}")
        return False

    # Response shape chưa chắc chắn → dò nhiều key thường gặp
    d = body.get("data", body) if isinstance(body, dict) else {}
    new_access = d.get("accessToken") or d.get("access_token") or d.get("token") or body.get("accessToken")
    if not new_access:
        if verbose:
            send_telegram(f"⚠️ Refresh trả về nhưng không tìm thấy access token. Keys: {list(d)[:8]}")
        return False
    creds["access_token"] = new_access
    # Nếu server set refresh token mới qua Set-Cookie thì cập nhật luôn
    for c in set_cookie:
        if c.startswith("shop_refresh_token="):
            creds["refresh_token"] = c.split(";", 1)[0].split("=", 1)[1]
    save_creds(creds)
    if verbose:
        left = token_seconds_left(creds)
        send_telegram(f"🔑 Refresh token OK — token mới còn ~{int(left) if left else '?'}s")
    return True


def browser_acquire(creds, headless=True):
    """Lấy token MỚI bằng trình duyệt thật (Playwright) khi refresh token đã chết (ver cũ).
    Dùng hồ sơ Google đã lưu ở .pw-profile → tự đăng nhập lại, không cần copy cURL tay."""
    sys.path.insert(0, HERE)
    try:
        import browser_login
    except Exception as e:
        send_telegram(f"❌ Chưa cài Playwright ({str(e)[:120]}).\nChạy: pip install playwright && python -m playwright install chromium")
        return False
    try:
        got = browser_login.acquire(creds["user_agent"], headless=headless)
    except browser_login.NeedsInteractiveLogin:
        send_telegram("🔐 Phiên Google của tool đã hết — cần đăng nhập lại 1 lần (chỉ 1 lần).\nChạy: python hoantien_checkin.py browser-login")
        return False
    except Exception as e:
        send_telegram(f"❌ Lấy token bằng trình duyệt lỗi: {str(e)[:200]}")
        return False
    if not got or not got.get("access_token"):
        send_telegram("⚠️ Trình duyệt mở được nhưng không chộp được access token — thử lại / chạy browser-login.")
        return False
    creds["access_token"] = got["access_token"]
    if got.get("refresh_token"):
        creds["refresh_token"] = got["refresh_token"]
    save_creds(creds)
    left = token_seconds_left(creds)
    send_telegram(f"🔑 Lấy token mới bằng trình duyệt OK — còn ~{int(left) if left else '?'}s")
    return True


def password_login(creds, verbose=True):
    """Đăng nhập bằng email+mật khẩu qua POST /auth/login → token mới + shop_refresh_token.

    Cách BỀN nhất để tự lấy token: không cần Google/OAuth/trình duyệt nên chạy headless lúc
    màn hình khóa vẫn 100% ăn. Cũng là "đăng nhập thật" nên đánh dấu nhiệm vụ login_day.

    Email/mật khẩu đọc từ .env (HOANTIEN_EMAIL / HOANTIEN_PASSWORD) hoặc creds.json (email/
    password). Chưa cấu hình → trả False lặng lẽ để caller thử cách khác (trình duyệt)."""
    email = os.environ.get("HOANTIEN_EMAIL") or creds.get("email")
    password = os.environ.get("HOANTIEN_PASSWORD") or creds.get("password")
    if not email or not password:
        return False
    url = creds.get("login_url") or LOGIN_API_URL_DEFAULT
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": f"https://{SITE_HOST}",
        "Referer": f"https://{SITE_HOST}/dang-nhap",
        "User-Agent": creds["user_agent"],
    }
    data = json.dumps({"email": email, "password": password}).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read())
            set_cookie = r.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as e:
        if verbose:
            send_telegram(f"❌ Login email+mật khẩu FAIL: HTTP {e.code}\n{e.read().decode(errors='replace')[:200]}")
        return False
    except Exception as e:
        if verbose:
            send_telegram(f"❌ Login email+mật khẩu lỗi: {str(e)[:200]}")
        return False

    # Cập nhật refresh token mới (Set-Cookie) trước — kể cả khi phải refresh để lấy access.
    for c in set_cookie:
        if c.startswith("shop_refresh_token="):
            creds["refresh_token"] = c.split(";", 1)[0].split("=", 1)[1]
    d = body.get("data", body) if isinstance(body, dict) else {}
    new_access = d.get("accessToken") or d.get("access_token") or d.get("token") or body.get("accessToken")
    if new_access:
        creds["access_token"] = new_access
        save_creds(creds)
        if verbose:
            left = token_seconds_left(creds)
            send_telegram(f"🔑 Login email+mật khẩu OK — token mới còn ~{int(left) if left else '?'}s")
        return True
    # Body không kèm access token nhưng đã có refresh cookie mới → lấy access qua refresh.
    if any(c.startswith("shop_refresh_token=") for c in set_cookie):
        save_creds(creds)
        return refresh_token(creds, verbose=verbose)
    if verbose:
        send_telegram(f"⚠️ Login trả về nhưng không có access token / cookie. Keys: {list(d)[:8]}")
    return False


def reacquire_token(creds):
    """Refresh đã chết → lấy token mới: ưu tiên login email+mật khẩu (bền), fallback OAuth
    trình duyệt. Trả về True nếu lấy được token dùng được."""
    if password_login(creds, verbose=True):
        return True
    return browser_acquire(creds)


def ensure_token(creds):
    """Token còn hạn → dùng luôn. Sắp hết → thử refresh (nhanh). Refresh chết (ver cũ, do bạn
    vừa đăng nhập web) → tự lấy token mới (login email+mật khẩu, rồi trình duyệt). True nếu ok."""
    left = token_seconds_left(creds)
    if left is None:
        return True  # không đọc được exp → cứ thử dùng
    if left > TOKEN_REFRESH_MARGIN_S:
        return True
    if refresh_token(creds, verbose=False):
        return True
    return reacquire_token(creds)


# ---------- HTTP ----------


def auth_headers(creds, with_content_type=True):
    h = {
        "Accept": "application/json",
        "Authorization": f"Bearer {creds['access_token']}",
        "Origin": f"https://{SITE_HOST}",
        "Referer": f"https://{SITE_HOST}/",
        "User-Agent": creds["user_agent"],
        "Cookie": f"shop_refresh_token={creds['refresh_token']}",
    }
    if with_content_type:
        h["Content-Type"] = "application/json"
    return h


def post_checkin(creds):
    """Trả về (sent_dt, payload_dict)."""
    req = urllib.request.Request(CHECKIN_API_URL, data=b"{}", headers=auth_headers(creds), method="POST")
    sent = datetime.now()
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return sent, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:400]
        try:
            payload = json.loads(body)
            payload.setdefault("httpError", e.code)
            return sent, payload
        except Exception:
            return sent, {"httpError": e.code, "body": body}
    except Exception as e:
        return sent, {"error": str(e)[:300]}


def get_json(url, creds):
    req = urllib.request.Request(url, headers=auth_headers(creds, with_content_type=False), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except Exception:
        return None


def get_status(creds):
    d = get_json(STATUS_API_URL, creds)
    return d.get("data") if isinstance(d, dict) else None


def get_leaderboard(creds):
    d = get_json(LEADERBOARD_API_URL, creds)
    if isinstance(d, dict):
        return (d.get("data") or {}).get("items")
    return None


# ---------- checkin logic ----------


def checkin_success(payload):
    """success = response có data.checkin (site không trả cờ 'success' riêng)."""
    return bool((payload.get("data") or {}).get("checkin"))


def already_checked(payload):
    msg = ((payload.get("message") or "") + " " + (payload.get("blockReason") or "")).lower()
    return payload.get("httpError") in (400, 409, 422) or "đã điểm danh" in msg or "already" in msg


def success_for_date(payload, target_str):
    """True nếu là checkin thành công đúng cho ngày target (chuỗi 'YYYY-MM-DD')."""
    if not checkin_success(payload):
        return False
    d = (payload["data"]["checkin"]).get("checkinDate")
    return d is None or d == target_str


def leaderboard_line(creds):
    board = get_leaderboard(creds)
    if not board:
        return None
    n = len(board)
    for e in board:
        if e.get("isMe"):
            return f"🏅 BXH #{e.get('rank')}/{n} · streak {e.get('streak')}"
    return f"🏅 ngoài BXH top {n}"


def build_success_block(creds, payload, meta):
    c = (payload.get("data") or {}).get("checkin") or {}
    wallet = (payload.get("data") or {}).get("wallet") or {}
    daily = c.get("dailyReward", 0)
    milestone = c.get("milestoneReward") or 0
    ms_str = f" (+{milestone} mốc {c.get('milestoneDays')} ngày)" if milestone else ""
    bal = wallet.get("available")
    bal_str = f" · ví {bal:,}đ".replace(",", ".") if bal is not None else ""
    lines = [
        "✅ Điểm danh thành công",
        f"🔥 streak {c.get('streak')} · +{daily}đ{ms_str}{bal_str}",
    ]
    lb = leaderboard_line(creds)
    if lb:
        lines.append(lb)
    if meta:
        lines.append(f"⚙️ {meta}")
    return "\n".join(lines)


def target_today(now=None):
    now = now or datetime.now()
    d = (now + timedelta(minutes=30)).date() if now.hour == 23 else now.date()
    return d.isoformat()


def fire_burst(creds, count, interval_ms):
    _socket.setdefaulttimeout(5)
    results = [None] * count

    def fire(idx):
        time.sleep(idx * interval_ms / 1000.0)
        sent, payload = post_checkin(creds)
        results[idx] = (idx, sent, payload, datetime.now())

    threads = [threading.Thread(target=fire, args=(i,), daemon=True) for i in range(count)]
    for t in threads:
        t.start()
    deadline = time.time() + count * interval_ms / 1000.0 + 12
    for t in threads:
        rem = deadline - time.time()
        if rem <= 0:
            break
        t.join(timeout=rem)
    return [r for r in results if r]


def status_as_payload(st):
    """Bọc /status thành dạng giống checkin để tái dùng build_success_block."""
    return {
        "data": {
            "checkin": {
                "streak": st.get("streak"),
                "dailyReward": st.get("dailyReward", 0),
                "milestoneReward": 0,
                "checkinDate": st.get("lastCheckinDate"),
            }
        }
    }


def _report(creds, result, count, is_status=False):
    idx, sent, payload, recv = result
    latency = (recv - sent).total_seconds() * 1000
    meta = f"burst #{idx + 1}/{count} · fire {sent.strftime('%H:%M:%S.%f')[:-3]} · {latency:.0f}ms"
    send_telegram(build_success_block(creds, payload, meta))


def do_single(creds, label="checkin"):
    ensure_token(creds)
    sent, payload = post_checkin(creds)
    latency = (datetime.now() - sent).total_seconds() * 1000
    if checkin_success(payload):
        send_telegram(build_success_block(creds, payload, f"{latency:.0f}ms"))
    elif already_checked(payload):
        send_telegram(f"ℹ️ Hôm nay đã điểm danh rồi.\n{payload.get('blockReason') or payload.get('message', '')}".strip())
    else:
        err = payload.get("message") or payload.get("error") or payload.get("body") or "fail"
        send_telegram(f"❌ {label} FAIL\n{err}")
    return payload


def do_burst(creds, count=None, interval_ms=BURST_INTERVAL_MS):
    """Burst dày lúc 00:00 (giành #1 nếu cửa mở đúng mốc), rồi poll NHẸ tới khi giành checkin
    HÔM NAY. Nếu token/refresh chết (401) thì DỪNG SỚM thay vì hammer vô ích."""
    if count is None:
        count = max(1, BURST_WINDOW_MS // interval_ms)
    target = target_today()
    recovered_prev = False

    def scan(results):
        """Trả (hit_hôm_nay, số_lỗi_401). Đồng thời ghi nhận vớt ngày cũ."""
        nonlocal recovered_prev
        hits, auth_err = [], 0
        for r in results:
            p = r[2]
            if checkin_success(p):
                if success_for_date(p, target):
                    hits.append(r)
                else:
                    recovered_prev = True
            elif p.get("httpError") == 401:
                auth_err += 1
        hits.sort(key=lambda r: r[1])
        return (hits[0] if hits else None), auth_err

    # Đợt 1: burst dày quanh mốc.
    hit, auth_err = scan(fire_burst(creds, count, interval_ms))
    if hit:
        if recovered_prev:
            send_telegram("🛟 Đã vớt điểm danh ngày cũ trước mốc — tiếp tục lấy hôm nay:")
        _report(creds, hit, count)
        return

    # Toàn bộ 401 → token chết. Thử refresh nhanh, fail thì tự lấy token mới bằng trình duyệt.
    if auth_err and not refresh_token(creds, verbose=False) and not reacquire_token(creds):
        send_telegram("❌ Token chết và không tự lấy lại được — KHÔNG điểm danh được.\nĐặt HOANTIEN_EMAIL/PASSWORD trong .env, hoặc chạy: python hoantien_checkin.py browser-login.")
        return

    if recovered_prev:
        send_telegram("🛟 Đã vớt điểm danh ngày cũ — tiếp tục cho HÔM NAY…")
    else:
        send_telegram(f"Cửa chưa mở — poll nhẹ mỗi {POLL_GAP_MS // 1000}s tới khi được (≤{POLL_MAX_SECONDS // 60}')…")

    deadline = time.time() + POLL_MAX_SECONDS
    polls, auth_streak = 0, 0
    while time.time() < deadline:
        polls += 1
        ensure_token(creds)  # refresh nếu JWT sắp hết hạn
        sent, p = post_checkin(creds)
        recv = datetime.now()
        if checkin_success(p) and success_for_date(p, target):
            _report(creds, (0, sent, p, recv), 1)
            return
        if checkin_success(p):
            recovered_prev = True
        if p.get("httpError") == 401:
            auth_streak += 1
            if auth_streak >= 3 and not refresh_token(creds, verbose=False) and not reacquire_token(creds):
                send_telegram("❌ Token chết giữa chừng, không tự lấy lại được — dừng. Đặt HOANTIEN_EMAIL/PASSWORD hoặc chạy browser-login.")
                return
        else:
            auth_streak = 0
        time.sleep(POLL_GAP_MS / 1000.0)

    st = get_status(creds)
    if st and st.get("checkedInToday") and st.get("lastCheckinDate") == target:
        send_telegram(build_success_block(creds, status_as_payload(st), f"xác nhận qua /status sau {polls} lần poll"))
        return
    prev_note = " (đã vớt được ngày cũ)" if recovered_prev else ""
    send_telegram(f"❌ Không điểm danh được HÔM NAY sau {POLL_MAX_SECONDS // 60}' / {polls} lần poll{prev_note}")


def run_scheduled(creds):
    send_telegram(f"⏳ Phiên điểm danh {datetime.now().strftime('%Y-%m-%d')} — chờ 00:00 ICT")
    try:
        _socket.getaddrinfo(API_HOST, 443)
    except Exception:
        pass

    # Refresh token sớm để chắc chắn còn hạn xuyên suốt burst quanh 00:00.
    wait_next_midnight(offset_ms=-20000)
    ok = ensure_token(creds)
    left = token_seconds_left(creds)
    if not ok and (left is None or left < 0):
        send_telegram("❌ Refresh token hết hạn TRƯỚC burst — không thể điểm danh đêm nay.\nLấy access_token + shop_refresh_token MỚI từ phiên đăng nhập riêng (xem README) rồi cập nhật creds.json.")
        return

    count = max(1, BURST_WINDOW_MS // BURST_INTERVAL_MS)
    send_telegram(f"🔑 token còn ~{int(left) if left else '?'}s · burst {count} phát lúc 00:00 rồi poll mỗi {POLL_GAP_MS // 1000}s tới khi cửa mở (≤{POLL_MAX_SECONDS // 60}')")

    wait_next_midnight(offset_ms=-BURST_LEAD_MS)
    do_burst(creds)


def wait_next_midnight(offset_ms=0):
    target = datetime.combine(datetime.now().date() + timedelta(days=1), dt_time()) + timedelta(milliseconds=offset_ms)
    while True:
        rem = (target - datetime.now()).total_seconds()
        if rem <= 0:
            return
        time.sleep(min(rem * 0.5, 0.1) if rem > 0.1 else 0.0001)


def do_status(creds):
    ensure_token(creds)
    st = get_status(creds)
    if not st:
        send_telegram("❌ Không lấy được status (token hết hạn / refresh chưa cấu hình?)")
        return
    checked = "✅ đã điểm danh" if st.get("checkedInToday") else "⬜ chưa điểm danh"
    lines = [
        f"📊 Hôm nay ({st.get('today')}): {checked}",
        f"   streak={st.get('streak')} · thưởng ngày {st.get('dailyReward')}đ",
    ]
    lb = leaderboard_line(creds)
    if lb:
        lines.append("   " + lb)
    if not st.get("checkedInToday") and st.get("blockReason"):
        lines.append(f"   ⛔ {st.get('blockReason')}")
    send_telegram("\n".join(lines))


def get_tasks(creds):
    d = get_json(TASKS_API_URL, creds)
    if isinstance(d, dict):
        return (d.get("data") or {}).get("items") or []
    return []


def post_claim(creds, task_id):
    url = f"{TASKS_API_URL}/{task_id}/claim"
    req = urllib.request.Request(url, data=b"{}", headers=auth_headers(creds), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        try:
            p = json.loads(body)
            p.setdefault("httpError", e.code)
            return p
        except Exception:
            return {"httpError": e.code, "body": body}
    except Exception as e:
        return {"error": str(e)[:200]}


def claimable_daily(items):
    """Nhiệm vụ hàng ngày tự-claim được: nhiệm vụ đăng nhập, hoặc daily đã hoàn thành chờ nhận.
    Bỏ qua cái đã nhận (da_nhan) và cái đang làm dở (dang_lam) cần thao tác thật."""
    out = []
    for it in items:
        rule, cat, st = it.get("ruleType"), it.get("category"), it.get("status")
        if st == "da_nhan":
            continue
        if rule == "login_day" or (cat == "hang_ngay" and st != "dang_lam"):
            out.append(it)
    return out


def do_tasks(creds):
    if not ensure_token(creds):
        send_telegram("❌ Không claim nhiệm vụ được — token/refresh hết hạn. Cần token mới (xem README).")
        return
    items = get_tasks(creds)
    if not items:
        send_telegram("⚠️ Không lấy được danh sách nhiệm vụ (/tasks) — token lỗi?")
        return

    login = next((it for it in items if it.get("ruleType") == "login_day"), None)
    # login_day CHỈ được server đánh dấu khi có ĐĂNG NHẬP THẬT bằng trình duyệt (OAuth Google).
    # refresh shop-token KHÔNG tính là "đăng nhập" → nếu không mở trình duyệt, tiến độ mãi 0/1.
    # Vì vậy khi nhiệm vụ này chưa nhận, chủ động đăng nhập lại bằng hồ sơ Playwright rồi refetch.
    if login and login.get("status") != "da_nhan":
        # login_day cần ĐĂNG NHẬP THẬT. Ưu tiên login email+mật khẩu (bền), fallback OAuth trình duyệt.
        if reacquire_token(creds):
            items = get_tasks(creds) or items
            login = next((it for it in items if it.get("ruleType") == "login_day"), login)
        else:
            # Đã tự báo Telegram (cần .env email/pass hoặc browser-login tay). Vẫn thử claim tiếp
            # phòng khi login_day đã được đánh dấu từ lần vào web khác trong ngày.
            send_telegram("⚠️ Không tự đăng nhập được — login_day có thể chưa đủ tiến độ.")
    todo = claimable_daily(items)
    if not todo:
        note = "đã nhận hôm nay" if (login and login.get("status") == "da_nhan") else "không có nhiệm vụ ngày để claim"
        send_telegram(f"🎯 Nhiệm vụ ngày: {note}")
        return

    lines, total, ok = [], 0, 0
    for it in todo:
        resp = post_claim(creds, it["id"])
        data = resp.get("data") or {}
        if data.get("status") == "da_nhan" or data.get("grant"):
            amt = (data.get("grant") or {}).get("amountVnd") or it.get("reward") or 0
            total += amt
            ok += 1
            lines.append(f"• {it.get('title')}: +{amt}đ ✅")
        else:
            err = resp.get("message")
            e = resp.get("error")
            if not err and isinstance(e, dict):
                err = e.get("message")
            err = err or (e if isinstance(e, str) else None) or resp.get("body") or "claim fail"
            lines.append(f"• {it.get('title')}: {err}")
    head = f"🎯 Nhiệm vụ ngày: claim {ok} việc, +{total}đ" if ok else "🎯 Nhiệm vụ ngày: không claim được việc nào"
    send_telegram(head + "\n" + "\n".join(lines))


def do_tasks_scheduled(creds):
    """Dùng cho Task Scheduler: ngủ ngẫu nhiên (giờ khác nhau mỗi ngày) rồi mới claim."""
    delay = random.randint(TASKS_RANDOM_DELAY_MIN_S, TASKS_RANDOM_DELAY_MAX_S)
    print(f"[tasks-run] {datetime.now().strftime('%H:%M:%S')} — ngủ {delay // 60} phút rồi claim")
    time.sleep(delay)
    do_tasks(creds)


def do_browser_login(creds):
    """LẦN ĐẦU (mở cửa sổ): đăng nhập Google 1 lần → lưu token + hồ sơ trình duyệt.
    Sau bước này tool tự đăng nhập lại được (headless) mỗi khi phiên bị web giành mất."""
    sys.path.insert(0, HERE)
    try:
        import browser_login
    except Exception as e:
        sys.exit(f"Chưa cài Playwright: {e}\n  pip install playwright && python -m playwright install chromium")
    got = browser_login.interactive_setup(creds["user_agent"])
    if not got or not got.get("access_token"):
        sys.exit("⚠️ Không lấy được token. Đăng nhập chưa xong? Thử lại: python hoantien_checkin.py browser-login")
    creds["access_token"] = got["access_token"]
    if got.get("refresh_token"):
        creds["refresh_token"] = got["refresh_token"]
    save_creds(creds)
    left = token_seconds_left(creds)
    send_telegram(f"✅ browser-login OK — đã lưu token (còn ~{int(left) if left else '?'}s) & hồ sơ Google.\nTừ giờ tool tự đăng nhập lại được, khỏi copy cURL tay.")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    creds = load_creds()
    if cmd == "test":
        do_single(creds, "test")
    elif cmd == "now":
        ensure_token(creds)
        do_burst(creds)
    elif cmd == "run":
        run_scheduled(creds)
    elif cmd == "status":
        do_status(creds)
    elif cmd == "refresh":
        refresh_token(creds)
    elif cmd == "browser-login":
        do_browser_login(creds)
    elif cmd == "browser-open":
        import browser_login
        browser_login.open_interactive(creds["user_agent"])
    elif cmd == "browser-refresh":
        browser_acquire(creds, headless=True)
    elif cmd == "tasks":
        do_tasks(creds)
    elif cmd == "tasks-run":
        do_tasks_scheduled(creds)
    else:
        sys.exit("Lệnh không hợp lệ. Dùng: test | now | run | status | refresh | browser-login | browser-open | browser-refresh | tasks | tasks-run")


if __name__ == "__main__":
    main()
