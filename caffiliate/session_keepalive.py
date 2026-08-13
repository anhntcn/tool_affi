"""Keep-alive + cảnh báo sớm cho session caffiliate.

Mục đích:
  - Mở profile Chrome mỗi ngày (vd 20:00) để cookie Google OAuth tự gia hạn
    → session gần như không bao giờ hết hạn → không phải login tay.
  - Nếu phát hiện bị redirect về /login (session đã hết hạn) → báo Telegram NGAY
    để bạn login lúc rảnh, thay vì 3 phút trước midnight.

Chạy:
  python session_keepalive.py            # chế độ yên lặng: chỉ báo khi có vấn đề
  python session_keepalive.py --verbose  # báo Telegram cả khi OK (để test)

Setup Task Scheduler chạy `python session_keepalive.py` mỗi ngày ~20:00.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auto_tool import send_telegram
from auto_click import launch_chrome, cdp_check_logged_in, kill_chrome


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

    # 4. Đóng Chrome để cookie ghi xuống đĩa (đóng sạch, không kill cứng nếu tránh được).
    time.sleep(3)
    try:
        proc.terminate()
        time.sleep(2)
    except Exception:
        pass
    kill_chrome()  # đảm bảo không để tiến trình treo giữ profile lock


if __name__ == "__main__":
    main()
