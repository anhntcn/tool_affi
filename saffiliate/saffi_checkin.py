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

# Burst trải qua mốc reset: bắt đầu bắn TRƯỚC 00:00 một chút rồi kéo dài QUA mốc.
# Lý do: server chỉ mở điểm danh ngày mới đúng lúc rollover. Request tới trước mốc trả
# "đã điểm danh (hôm qua)" — vô hại; request tới sau mốc mới success. Phải phủ cả 2 phía
# mốc thì mới vừa giành được early-bird vừa không bao giờ trượt.
BURST_LEAD_MS = 500       # bắt đầu bắn trước 00:00 bao nhiêu ms
BURST_WINDOW_MS = 3000    # tổng thời gian bắn (kéo dài qua mốc reset)
BURST_INTERVAL_MS = 60    # khoảng cách giữa các request → ~50 request phủ đều cửa sổ
CONFIRM_AFTER_MS = 3200   # sau mốc reset bao lâu thì xác nhận qua status (đảm bảo server đã rollover)

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


def do_burst(creds, count=None, interval_ms=BURST_INTERVAL_MS):
    """Bắn burst rồi báo cáo. Nếu burst không có phát nào success (ví dụ toàn bộ rơi
    trước mốc reset → 'đã điểm danh hôm qua'), xác nhận qua /status và tự bắn cứu."""
    if count is None:
        count = max(1, BURST_WINDOW_MS // interval_ms)
    done = fire_burst(creds, count, interval_ms)
    successes = [r for r in done if r[2].get("success")]

    if successes:
        successes.sort(key=lambda r: r[1])  # phát bắn sớm nhất mà thành công → early-bird tốt nhất
        idx, sent, payload, recv = successes[0]
        latency = (recv - sent).total_seconds() * 1000
        meta = f"burst #{idx + 1}/{count} · fire {sent.strftime('%H:%M:%S.%f')[:-3]} · {latency:.0f}ms"
        send_telegram(build_success_block(creds, payload, meta))
        return

    # Không phát nào success → dùng /status làm nguồn sự thật (server đã rollover ngày mới chưa?)
    st = get_status(creds)
    if st and st.get("checked_in_today"):
        # Đã điểm danh ngày mới (có thể 1 phát thành công nhưng bị coi là trùng do đua)
        send_telegram(build_success_block(creds, status_as_payload(st), "xác nhận qua /status"))
        return

    # Server báo CHƯA điểm danh hôm nay → burst vừa rồi rơi hết trước mốc. Bắn cứu ngay (giờ đã qua mốc).
    n_already = sum(1 for r in done if already_checked(r[2]))
    send_telegram(f"🛟 Burst trượt mốc ({n_already}/{len(done)} rơi vào hôm qua) — bắn cứu sau nửa đêm…")
    for attempt in range(1, 6):
        sent, payload = post_checkin(creds)
        if payload.get("success"):
            latency = (datetime.now() - sent).total_seconds() * 1000
            send_telegram(build_success_block(creds, payload, f"recovery #{attempt} · {latency:.0f}ms"))
            return
        if already_checked(payload):
            send_telegram("ℹ️ Đã điểm danh hôm nay (xác nhận sau mốc reset).")
            return
        time.sleep(0.3)
    err = payload.get("message") or payload.get("error") or payload.get("body") or "fail"
    send_telegram(f"❌ Điểm danh FAIL sau burst + 5 lần cứu\n{err}")


def run_scheduled(creds):
    send_telegram(f"⏳ Phiên điểm danh {datetime.now().strftime('%Y-%m-%d')} — chờ 00:00 ICT")
    try:
        _socket.getaddrinfo(API_HOST, 443)  # warm DNS
    except Exception:
        pass

    # Đo RTT lúc 00:00 - 5s (chỉ để log debug — không còn dùng để bù offset).
    wait_next_midnight(offset_ms=-5000)
    samples = [ms for ms in (measure_rtt(creds) for _ in range(3)) if ms is not None]
    rtt_str = f"{[int(s) for s in samples]}ms" if samples else "n/a"
    count = max(1, BURST_WINDOW_MS // BURST_INTERVAL_MS)
    send_telegram(
        f"📡 RTT {rtt_str} · bắn {count} phát trải cửa sổ "
        f"[-{BURST_LEAD_MS}ms → +{BURST_WINDOW_MS - BURST_LEAD_MS}ms] quanh 00:00"
    )

    # Bắt đầu bắn TRƯỚC mốc BURST_LEAD_MS, cửa sổ kéo dài QUA mốc → luôn có phát rơi vào ngày mới.
    wait_next_midnight(offset_ms=-BURST_LEAD_MS)
    do_burst(creds)


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
    else:
        sys.exit("Lệnh không hợp lệ. Dùng: test | now | run | status")


if __name__ == "__main__":
    main()
