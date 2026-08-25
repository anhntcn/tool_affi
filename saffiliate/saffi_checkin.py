"""Điểm danh (check-in) tự động cho app.saffi.vn — gọi API trực tiếp, không cần Selenium.

Site này đơn giản hơn caffiliate: không có chữ ký HMAC, chỉ cần Bearer token (Laravel
Sanctum) + cookie. Endpoint checkin nhận body rỗng và trả sẵn `early_bird_rank`, nên báo
cáo top lấy luôn từ response — không cần gọi status riêng.

Reset điểm danh: 00:00 giờ Việt Nam (ICT). Ai bấm sớm được thưởng "early bird".

Cách chạy:
  python saffi_checkin.py test   # điểm danh 1 phát ngay bây giờ — dùng để kiểm tra creds
  python saffi_checkin.py now    # burst ngay lập tức (dùng khi đã qua/đang gần giờ reset)
  python saffi_checkin.py run    # chờ tới 00:00 ICT rồi burst — dùng cho Task Scheduler

Task Scheduler: chạy `python saffi_checkin.py run` mỗi tối lúc ~23:55.

File deps (cùng thư mục, đều đã được .gitignore):
  - creds.json : bearer_token, cookies, xsrf_token, user_agent  (xem creds.example.json)
  - .env       : TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (tùy chọn, để nhận báo cáo)
"""
import json
import os
import socket as _socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time as dt_time, timedelta

# Ép UTF-8 để print tiếng Việt không crash trên Windows console
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

API_HOST = "app.saffi.vn"
CHECKIN_API_URL = f"https://{API_HOST}/api/spoint/checkin"
STATUS_API_URL = f"https://{API_HOST}/api/spoint/status"
LEADERBOARD_API_URL = f"https://{API_HOST}/api/spoint/leaderboard"
SITE_ROOT = f"https://{API_HOST}/qua-tang"  # dùng để đo RTT

# QUAN TRỌNG — saffi mở điểm danh ngày mới TRỄ vài phút sau 00:00 (quan sát: early-bird #1
# check-in lúc ~00:03–00:08 ICT), và server THROTTLE nếu bị bắn dồn dập. Nên chiến lược là:
# burst NHỎ lúc 00:00 (phòng khi mở đúng mốc), rồi POLL NHẸ 1 request mỗi vài giây, kiên trì
# tới ~15 phút cho tới khi cửa mở. Vừa tránh throttle vừa bắt được rollover trễ (poll ngay khi
# cửa mở còn có thể giành early-bird #1).
BURST_LEAD_MS = 300       # bắt đầu bắn trước 00:00 bao nhiêu ms
BURST_WINDOW_MS = 2000    # burst đợt đầu trải cửa sổ quanh mốc
BURST_INTERVAL_MS = 300   # → ~7 request nhẹ nhàng (KHÔNG hammer để tránh bị throttle)

POLL_GAP_MS = 4000        # sau burst đầu: poll 1 request mỗi 4s (nhẹ, không throttle)
POLL_MAX_SECONDS = 900    # kiên trì tới 15 phút (rollover saffi có thể trễ vài phút)

ICT_OFFSET_HOURS = 7          # server reset theo 00:00 giờ VN (ICT = UTC+7)

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
    # Header thống nhất với caffiliate: emoji + site + giờ → dễ phân biệt trong cùng channel
    text = f"🌱 SAFFI · {datetime.now().strftime('%H:%M:%S')}\n{msg}"[:3900]
    data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as r:
            r.read()
    except Exception as e:
        print(f"TG fail: {e}")


def load_creds():
    if not os.path.isfile(CREDS_PATH):
        sys.exit(f"Thiếu {CREDS_PATH}. Copy creds.example.json → creds.json rồi điền token/cookie.")
    with open(CREDS_PATH, encoding="utf-8") as f:
        creds = json.load(f)
    missing = [k for k in ("bearer_token", "cookies", "user_agent") if not creds.get(k)]
    if missing:
        sys.exit(f"creds.json thiếu field: {', '.join(missing)}")
    return creds


def build_headers(creds):
    h = {
        "Accept": "application/json",
        "Authorization": f"Bearer {creds['bearer_token']}",
        "Content-Length": "0",
        "Origin": f"https://{API_HOST}",
        "Referer": SITE_ROOT,
        "User-Agent": creds["user_agent"],
        "Cookie": creds["cookies"],
    }
    if creds.get("xsrf_token"):
        h["X-XSRF-TOKEN"] = creds["xsrf_token"]
    return h


def post_checkin(creds):
    """Trả về (sent_dt, payload_dict)."""
    req = urllib.request.Request(CHECKIN_API_URL, data=b"", headers=build_headers(creds), method="POST")
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
            return sent, {"success": False, "httpError": e.code, "body": body}
    except Exception as e:
        return sent, {"success": False, "error": str(e)[:300]}


def get_json(url, creds):
    req = urllib.request.Request(url, headers=build_headers(creds), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except Exception:
        return None


def get_status(creds):
    d = get_json(STATUS_API_URL, creds)
    return d.get("data") if d and d.get("success") else None


def get_leaderboard(creds):
    d = get_json(LEADERBOARD_API_URL, creds)
    return d.get("data") if d and d.get("success") else None


def leaderboard_line(creds, user_id):
    """Dòng '🏅 BXH #k/n · X S-Point (rank)' cho báo cáo, hoặc None nếu không lấy được."""
    board = get_leaderboard(creds)
    if not board:
        return None
    n = len(board)
    for i, e in enumerate(board):
        if e.get("id") == user_id:
            return f"🏅 BXH #{i + 1}/{n} · {e.get('spoint_total')} S-Point ({e.get('rank')})"
    return f"🏅 ngoài BXH top {n}"


def measure_rtt(creds):
    req = urllib.request.Request(SITE_ROOT, headers={
        "User-Agent": creds["user_agent"], "Accept": "*/*", "Cookie": creds["cookies"],
    }, method="GET")
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            r.read(1)
        return (time.time() - start) * 1000
    except urllib.error.HTTPError:
        return (time.time() - start) * 1000  # vẫn tính là round-trip hợp lệ
    except Exception:
        return None


def wait_next_midnight(offset_ms=0):
    target = datetime.combine(datetime.now().date() + timedelta(days=1), dt_time()) + timedelta(milliseconds=offset_ms)
    while True:
        rem = (target - datetime.now()).total_seconds()
        if rem <= 0:
            return
        time.sleep(min(rem * 0.5, 0.1) if rem > 0.1 else 0.0001)


def build_success_block(creds, payload, meta):
    """Block báo cáo điểm danh thành công, nhiều dòng gọn gàng."""
    data = payload.get("data") or {}
    c = data.get("checkin") or {}
    total = data.get("spoint_total", c.get("total_points"))
    eb = c.get("early_bird_points") or 0
    eb_str = f" (+{eb} eb)" if eb else ""
    lines = [
        "✅ Điểm danh thành công",
        f"🔥 streak {c.get('streak_count')} · +{c.get('base_points')}{eb_str} → {total} S-Point",
    ]
    rank = c.get("early_bird_rank")
    if rank:
        lines.append(f"🏆 early-bird #{rank}")
    lb = leaderboard_line(creds, c.get("user_id"))
    if lb:
        lines.append(lb)
    if meta:
        lines.append(f"⚙️ {meta}")
    return "\n".join(lines)


def already_checked(payload):
    msg = (payload.get("message") or "").lower()
    return payload.get("httpError") in (409, 422) or "đã điểm danh" in msg or "already" in msg


def captcha_required(payload):
    """Từ 2026-08-10 saffi thêm cổng chống bot: POST /checkin trả 400 'hoàn thành xác thực
    chống bot (Captcha)' khi phiên chưa giải captcha (Cloudflare Turnstile). Bắn API trần
    không có token thì luôn dính 400 → phát hiện sớm để chuyển sang điểm danh bằng trình duyệt."""
    msg = (payload.get("message") or "").lower()
    return payload.get("httpError") == 400 and ("captcha" in msg or "chống bot" in msg)


def browser_checkin(creds, note="", deadline_s=POLL_MAX_SECONDS):
    """Vượt Turnstile bằng trình duyệt thật: mở qua-tang (đã đăng nhập) và bấm nút Điểm danh,
    để site tự sinh token. Trả True nếu điểm danh được. Xem saffi_browser.py."""
    sys.path.insert(0, HERE)
    try:
        import saffi_browser
    except Exception as e:
        send_telegram(
            f"❌ Thiếu Playwright để vượt captcha ({str(e)[:100]}).\n"
            "  pip install playwright && python -m playwright install chromium"
        )
        return False
    send_telegram("🌐 Gặp captcha Turnstile — chuyển sang điểm danh bằng Chrome thật (CDP)…")
    try:
        # Chrome THẬT + CDP để vượt Turnstile (Playwright tự mở bị Cloudflare chặn). Giữ deadline
        # dài (tới POLL_MAX_SECONDS ~15') để bao được cả đêm saffi mở cửa trễ (00:03–00:08+): nút
        # "Điểm danh ngay" hiện sẵn nhưng server chưa mở → _wait_and_click bấm lại tới khi được.
        payload = saffi_browser.checkin_cdp(creds, deadline_s=min(deadline_s, 900))
    except saffi_browser.NeedsInteractiveLogin:
        send_telegram("🔐 Chrome chưa đăng nhập saffi — chạy 1 lần: python saffi_checkin.py cdp-login")
        return False
    except Exception as e:
        send_telegram(f"❌ Điểm danh qua trình duyệt lỗi: {str(e)[:160]}")
        return False
    if payload and (payload.get("success") or payload.get("status") == "success"):
        send_telegram(build_success_block(creds, payload, f"qua trình duyệt {note}".strip()))
        return True
    if payload and already_checked(payload):
        send_telegram("ℹ️ (trình duyệt) Hôm nay đã điểm danh rồi.")
        return True
    send_telegram(f"❌ Điểm danh qua trình duyệt CHƯA được. resp: {str(payload)[:160]}")
    return False


def checkin_ict_date(checkin_date_str):
    """checkin_date của server ở dạng UTC (vd '2026-07-29T17:00:00.000000Z' = 00:00 ICT ngày 30).
    Trả về ngày theo lịch ICT, hoặc None nếu không parse được."""
    if not checkin_date_str:
        return None
    try:
        s = checkin_date_str.replace("Z", "")[:19]
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S") + timedelta(hours=ICT_OFFSET_HOURS)
        return dt.date()
    except Exception:
        return None


def success_for_date(payload, target_date):
    """True nếu payload là checkin THÀNH CÔNG đúng cho target_date (không phải vớt ngày cũ)."""
    if not payload.get("success"):
        return False
    c = (payload.get("data") or {}).get("checkin") or {}
    d = checkin_ict_date(c.get("checkin_date"))
    return d is None or d == target_date  # nếu không có date thì tạm coi là hợp lệ


def do_single(creds, label="checkin"):
    sent, payload = post_checkin(creds)
    latency = (datetime.now() - sent).total_seconds() * 1000
    if payload.get("success"):
        send_telegram(build_success_block(creds, payload, f"{latency:.0f}ms"))
    elif already_checked(payload):
        send_telegram(f"ℹ️ Hôm nay đã điểm danh rồi.\n{payload.get('message', '')}".strip())
    else:
        err = payload.get("message") or payload.get("error") or payload.get("body") or "fail"
        send_telegram(f"❌ {label} FAIL\n{err}")
    return payload


def fire_burst(creds, count, interval_ms):
    """Bắn `count` request cách nhau interval_ms (chạy song song, mỗi thread tự delay).
    Trả về list [(idx, sent_dt, payload, recv_dt)] theo thứ tự đã hoàn thành."""
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
    """Bọc data từ /status thành dạng giống response checkin để tái dùng build_success_block."""
    return {"success": True, "data": {
        "checkin": st.get("today_checkin") or {},
        "spoint_total": st.get("spoint_total"),
    }}


def target_today(now=None):
    """Ngày ICT ta muốn điểm danh: nếu đang ở 23h (sắp qua mốc) thì là ngày mai, ngược lại hôm nay."""
    now = now or datetime.now()
    return (now + timedelta(minutes=30)).date() if now.hour == 23 else now.date()


def _report_today_success(creds, result, count, target_date):
    idx, sent, payload, recv = result
    latency = (recv - sent).total_seconds() * 1000
    meta = f"burst #{idx + 1}/{count} · fire {sent.strftime('%H:%M:%S.%f')[:-3]} · {latency:.0f}ms"
    send_telegram(build_success_block(creds, payload, meta))


def do_burst(creds, count=None, interval_ms=BURST_INTERVAL_MS):
    """Burst nhẹ lúc 00:00, rồi poll nhẹ tới khi giành ĐÚNG checkin HÔM NAY (saffi mở cửa trễ
    vài phút và throttle nếu bắn dồn). Vớt streak ngày cũ xong vẫn poll tiếp cho hôm nay."""
    if count is None:
        count = max(1, BURST_WINDOW_MS // interval_ms)
    target = target_today()
    recovered_prev = False   # đã có phát vớt điểm danh cho ngày cũ chưa

    def scan(results):
        """Trả về result thành công cho HÔM NAY (bắn sớm nhất), hoặc None. Đồng thời phát hiện vớt ngày cũ."""
        nonlocal recovered_prev
        today_hits = []
        for r in results:
            p = r[2]
            if p.get("success"):
                if success_for_date(p, target):
                    today_hits.append(r)
                else:
                    recovered_prev = True  # success nhưng của ngày cũ = vừa vớt streak
        today_hits.sort(key=lambda r: r[1])
        return today_hits[0] if today_hits else None

    # Đợt 1: burst NHỎ quanh mốc (phòng khi server mở đúng 00:00 như hoantien).
    first = fire_burst(creds, count, interval_ms)
    hit = scan(first)
    if hit:
        if recovered_prev:
            send_telegram("🛟 Đã vớt điểm danh ngày hôm qua trước mốc — tiếp tục lấy hôm nay:")
        _report_today_success(creds, hit, count, target)
        return

    # Cổng captcha chống bot → API trần vô dụng, chuyển sang điểm danh bằng trình duyệt (Turnstile).
    if any(captcha_required(r[2]) for r in first):
        browser_checkin(creds, "burst đầu")
        return

    # Chưa mở → POLL NHẸ 1 request mỗi POLL_GAP_MS cho tới khi cửa mở (saffi thường trễ vài phút).
    deadline = time.time() + POLL_MAX_SECONDS
    send_telegram(
        ("🛟 Đã vớt hôm qua. " if recovered_prev else "")
        + f"Cửa chưa mở — poll nhẹ mỗi {POLL_GAP_MS // 1000}s tới khi điểm danh được (≤{POLL_MAX_SECONDS // 60}')…"
    )
    polls = 0
    throttled = 0
    while time.time() < deadline:
        polls += 1
        sent, p = post_checkin(creds)
        recv = datetime.now()
        if p.get("success") and success_for_date(p, target):
            _report_today_success(creds, (0, sent, p, recv), 1, target)
            return
        if p.get("success"):
            recovered_prev = True  # success nhưng của ngày cũ
        if captcha_required(p):
            remain = max(60, int(deadline - time.time()))
            browser_checkin(creds, f"sau {polls} lần poll", deadline_s=remain)
            return
        if p.get("httpError") == 429 or "quá nhiều" in (p.get("message") or "").lower():
            throttled += 1
            time.sleep(5)  # bị throttle → lùi thêm
        time.sleep(POLL_GAP_MS / 1000.0)

    # Hết 15' vẫn chưa được → xác nhận cuối bằng /status.
    st = get_status(creds)
    if st and st.get("checked_in_today"):
        tc = st.get("today_checkin") or {}
        if checkin_ict_date(tc.get("checkin_date")) == target:
            send_telegram(build_success_block(creds, status_as_payload(st), f"xác nhận qua /status sau {polls} lần poll"))
            return
    thr = f", throttle {throttled}x" if throttled else ""
    prev_note = " (đã vớt được ngày hôm qua)" if recovered_prev else ""
    send_telegram(f"❌ Không điểm danh được HÔM NAY sau {POLL_MAX_SECONDS // 60}' / {polls} lần poll{thr}{prev_note}")


# Bao lâu chịu poll /status chờ cửa mở, và nhịp poll.
# Reset thực đo được ~00:09 ICT (statuswatch 2026-08-22). Poll DÀY để bắt đúng giây mở → giành #1.
STATUS_POLL_MAX_S = 3 * 3600   # kiên trì tối đa 3h (bao sai lệch mốc reset)
STATUS_POLL_GAP_S = 5          # kiểm tra /status mỗi 5s (bắn sát mốc mở)


def run_scheduled(creds):
    """QUAN TRỌNG: saffi KHÔNG reset điểm danh lúc 00:00 ICT như tưởng trước đây — quan sát cho thấy
    cửa ngày mới mở muộn (khoảng 07:00 ICT = 00:00 UTC). Thay vì đoán giờ, POLL /status tới khi
    `checked_in_today=false` (cửa đã mở cho ngày mới) rồi điểm danh qua Chrome (vượt Turnstile).
    Đặt Task Scheduler chạy quanh 06:45 ICT để không phải thức cả đêm."""
    send_telegram(f"⏳ Phiên điểm danh {datetime.now().strftime('%Y-%m-%d')} — poll /status chờ cửa mở…")
    try:
        _socket.getaddrinfo(API_HOST, 443)  # warm DNS
    except Exception:
        pass

    # Chống chạy trùng: nếu ĐÃ điểm danh hôm nay và KHÔNG ở quanh mốc reset (00:09) → thoát ngay.
    # Nhờ vậy task dự phòng buổi sáng (nếu có) không poll vô ích 3h. Khoảng 23:30–00:30 vẫn poll
    # (đó là lúc chờ cửa lật sang ngày mới).
    st0 = get_status(creds)
    if st0 and st0.get("checked_in_today"):
        h = datetime.now().hour
        in_reset_window = (h == 23 and datetime.now().minute >= 30) or (h == 0 and datetime.now().minute <= 30)
        if not in_reset_window:
            send_telegram("ℹ️ Hôm nay đã điểm danh rồi — bỏ qua phiên này.")
            return

    deadline = time.time() + STATUS_POLL_MAX_S
    announced = False
    fails = 0
    while time.time() < deadline:
        st = get_status(creds)
        if st is None:
            fails += 1
            if fails == 3:
                send_telegram("⚠️ /status lỗi liên tục — token/cookie hết hạn? Vẫn thử lại…")
            time.sleep(STATUS_POLL_GAP_S)
            continue
        fails = 0
        if not st.get("checked_in_today"):
            send_telegram("🚪 Cửa điểm danh đã mở — điểm danh qua Chrome thật…")
            if browser_checkin(creds, "cửa mở"):
                return
            time.sleep(STATUS_POLL_GAP_S)  # lỗi tạm → thử lại vòng sau
            continue
        if not announced:
            send_telegram(
                f"😴 Chưa tới giờ reset (đã điểm danh hôm qua) — poll /status mỗi "
                f"{STATUS_POLL_GAP_S // 60}' tới khi cửa mở (≤{STATUS_POLL_MAX_S // 3600}h)…"
            )
            announced = True
        time.sleep(STATUS_POLL_GAP_S)

    send_telegram(f"❌ Sau {STATUS_POLL_MAX_S // 3600}h vẫn chưa điểm danh được — kiểm tra lại (cửa mở muộn hơn?).")


def do_statuswatch(creds, gap_s=60, hours=9, wait_until=None):
    """Ghi /status mỗi `gap_s` giây để TÌM GIỜ RESET: khi `checked_in_today` lật true→false là
    cửa ngày mới mở. Ghi ra saffi_status_watch.log + báo Telegram đúng khoảnh khắc mở. Chạy qua
    đêm (giữ máy thức). Ctrl+C để dừng. Sau khi biết giờ reset → chỉnh lịch bắn sát mốc.

    wait_until='HH:MM' → chạy lệnh sớm nhưng NGỦ tới giờ đó mới bắt đầu poll (khỏi poll vô ích)."""
    if wait_until:
        try:
            hh, mm = map(int, wait_until.split(":"))
            now = datetime.now()
            target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            delay = (target - now).total_seconds()
            if delay > 0:
                print(f"[statuswatch] chờ tới {wait_until} rồi mới poll (~{int(delay // 60)} phút nữa)…", flush=True)
                time.sleep(delay)
        except Exception as e:
            print(f"[statuswatch] wait_until lỗi ({e}) — bắt đầu ngay.", flush=True)
    path = os.path.join(HERE, "saffi_status_watch.log")
    end = time.time() + hours * 3600
    prev = None
    reset_found = False
    print(f"[statuswatch] ghi mỗi {gap_s}s trong ≤{hours}h → {path}", flush=True)
    while time.time() < end:
        st = get_status(creds)
        cit = None if st is None else bool(st.get("checked_in_today"))
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} checked_in_today={cit} streak={(st or {}).get('spoint_streak')}"
        print(line, flush=True)
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        if prev is True and cit is False and not reset_found:
            msg = f"🚪 SAFFI CỬA MỞ (reset) lúc {datetime.now().strftime('%H:%M:%S')} — ghi nhớ mốc này!"
            send_telegram(msg)
            reset_found = True
        if cit is not None:
            prev = cit
        time.sleep(gap_s)
    if not reset_found:
        send_telegram("⚠️ statuswatch hết giờ mà chưa bắt được lúc cửa mở (reset ngoài khoảng theo dõi?).")


def do_status(creds):
    st = get_status(creds)
    if not st:
        send_telegram("❌ Không lấy được status (token hết hạn?)")
        return
    checked = "✅ đã điểm danh" if st.get("checked_in_today") else "⬜ chưa điểm danh"
    tc = st.get("today_checkin") or {}
    eb = tc.get("early_bird_rank")
    eb_str = f", early-bird #{eb}" if eb else ""
    uid = tc.get("user_id")
    board = get_leaderboard(creds) or []
    my_rank = next((f"#{i + 1}/{len(board)}" for i, e in enumerate(board) if e.get("id") == uid), f"ngoài top {len(board)}")
    lines = [
        f"📊 Hôm nay: {checked}{eb_str}",
        f"   streak={st.get('spoint_streak')} · tổng {st.get('spoint_total')} S-Point · BXH {my_rank}",
    ]
    ebs = st.get("early_birds_today") or []
    if ebs:
        top = ", ".join(f"#{e.get('early_bird_rank')} {(e.get('user') or {}).get('name', '?')}" for e in ebs[:3])
        lines.append(f"   🐦 Early-bird hôm nay: {top}")
    send_telegram("\n".join(lines))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    creds = load_creds()
    if cmd == "test":
        do_single(creds, "test")
    elif cmd == "now":
        do_burst(creds)
    elif cmd == "run":
        run_scheduled(creds)
    elif cmd == "status":
        do_status(creds)
    elif cmd == "statuswatch":
        gap = int(sys.argv[2]) if len(sys.argv) > 2 else 60
        hrs = int(sys.argv[3]) if len(sys.argv) > 3 else 9
        wu = sys.argv[4] if len(sys.argv) > 4 else None  # 'HH:MM' — chờ tới giờ này mới poll
        do_statuswatch(creds, gap_s=gap, hours=hrs, wait_until=wu)
    elif cmd == "browser":
        browser_checkin(creds, "chạy tay", deadline_s=180)
    elif cmd == "cdp":
        sys.path.insert(0, HERE)
        import saffi_browser
        res = saffi_browser.checkin_cdp(creds, deadline_s=180)
        print(res)
    elif cmd == "cdp-login":
        sys.path.insert(0, HERE)
        import saffi_browser
        saffi_browser.cdp_login()
    elif cmd == "browser-login":
        sys.path.insert(0, HERE)
        import saffi_browser
        saffi_browser.interactive_login(creds)
    else:
        sys.exit("Lệnh không hợp lệ. Dùng: test | now | run | status | statuswatch | browser | cdp | cdp-login | browser-login")


if __name__ == "__main__":
    main()
