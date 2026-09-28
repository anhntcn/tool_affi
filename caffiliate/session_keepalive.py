"""Keep-alive + cảnh báo sớm + điểm danh ban ngày cho caffiliate.

Mục đích:
  - Mở profile Chrome mỗi ngày (vd 20:00) để cookie Google OAuth tự gia hạn
    → session gần như không bao giờ hết hạn → không phải login tay.
  - Nếu phát hiện bị redirect về /login (session đã hết hạn) → báo Telegram NGAY.
  - ĐIỂM DANH BAN NGÀY:
      * Đêm NHƯỜNG (cede_date == hôm nay): midnight đã cố tình bỏ qua → điểm danh giờ
        (random 20:00-20:10) để rank thấp, phá pattern 'luôn #1', vẫn giữ streak.
      * CỨU STREAK: nếu hôm nay chưa điểm danh mà KHÔNG phải đêm nhường → midnight fire
        đã lỗi → điểm danh NGAY để cứu streak + báo động.

Chạy:
  python session_keepalive.py            # yên lặng: chỉ báo khi có vấn đề / khi điểm danh
  python session_keepalive.py --verbose  # báo cả khi OK

Setup Task Scheduler chạy `python session_keepalive.py` mỗi ngày ~20:00.
"""
import os
import random
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auto_tool import send_telegram, load_creds_from_file, get_checkin_status
from auto_click import (
    launch_chrome,
    cdp_check_logged_in,
    kill_chrome,
    reload_and_click_via_cdp,
    load_config,
    log_rank_datapoint,
)


def _do_checkin(creds, label, mode):
    """Click điểm danh (trang tự xử Turnstile), verify, báo Telegram. Trả True nếu điểm danh OK."""
    ok, msg = reload_and_click_via_cdp(max_attempts=25, poll_interval_ms=200)
    time.sleep(4)
    for attempt in range(2):
        st = get_checkin_status(creds)
        if st and st.get("todayCheckedIn"):
            send_telegram(
                f"{label}: ✅ điểm danh OK"
                + (" (lần 2)" if attempt else "")
                + f" | streak={st.get('currentStreak')} | rank={st.get('todayCheckInPosition')}"
            )
            # Ghi rank_data.md để đêm nhường KHÔNG còn là điểm mù (trước đây keepalive không log).
            try:
                log_rank_datapoint(st, datetime.now().date(), mode, "", "")
            except Exception as e:
                print(f"[log_rank keepalive err] {e}")
            return True
        if attempt == 0:
            reload_and_click_via_cdp(max_attempts=25, poll_interval_ms=200)
            time.sleep(4)
    send_telegram(
        f"🔴🔴 {label}: ĐIỂM DANH 20:00 THẤT BẠI ({msg[:130]})\n"
        f"‼️‼️ STREAK SẮP MẤT — VÀO app.caffiliate.vn/rewards CLICK TAY NGAY (còn tới 23:59 hôm nay)!"
    )
    return False


def main():
    verbose = "--verbose" in sys.argv

    # 1. Launch Chrome (kèm remote-debugging-port qua launch_chrome).
    proc = launch_chrome()

    # 2. Chờ Chrome load + CDP port sẵn sàng.
    time.sleep(15)

    # 3. Check session qua CDP.
    ok, detail = cdp_check_logged_in(timeout=30)
    if ok:
        if verbose:
            send_telegram("🟢 Keep-alive OK — session caffiliate còn hạn, cookie đã gia hạn.")
    else:
        if detail == "login":
            send_telegram(
                "🔴 Keep-alive: SESSION HẾT HẠN! Trang ra /login.\n"
                "Login TAY vào Chrome (profile C:\\SeleniumChromeProfile) lúc rảnh "
                "để đêm nay auto-click chạy được, khỏi cuống lúc 23:57."
            )
        else:
            send_telegram(
                f"🟠 Keep-alive: không thấy tab caffiliate/rewards qua CDP (detail={detail}). "
                "Kiểm tra Chrome/CDP."
            )

    # 4. ĐIỂM DANH BAN NGÀY (đêm nhường hoặc cứu streak).
    try:
        cfg = load_config()
        today = str(datetime.now().date())
        creds = load_creds_from_file()
        status = get_checkin_status(creds)
        already = bool(status and status.get("todayCheckedIn"))
        is_cede = cfg.get("cede_date") == today

        if already:
            if verbose:
                send_telegram(f"🟢 Hôm nay đã điểm danh (rank {status.get('todayCheckInPosition')}), keepalive không cần làm gì.")
        elif is_cede:
            # Đêm nhường: random 0-10 phút để giờ điểm danh không cố định (20:00-20:10).
            delay = random.randint(0, 600)
            print(f"[cede] đêm nhường — chờ {delay}s rồi điểm danh...")
            time.sleep(delay)
            _do_checkin(creds, "🌙 NHƯỜNG", "cede-day")
        else:
            # KHÔNG phải đêm nhường mà chưa điểm danh → midnight fire LỖI → cứu streak NGAY.
            _do_checkin(creds, "🛟 CỨU STREAK (midnight có thể đã lỗi)", "rescue-day")
    except Exception as e:
        send_telegram(f"⚠️ Keepalive điểm danh lỗi: {type(e).__name__}: {str(e)[:150]}")

    # 5. Đóng Chrome để cookie ghi xuống đĩa.
    time.sleep(3)
    try:
        proc.terminate()
        time.sleep(2)
    except Exception:
        pass
    kill_chrome()  # chỉ giết Chrome của profile caffiliate


if __name__ == "__main__":
    main()
