"""Lấy token hoantienshopee bằng trình duyệt thật (Playwright).

Vì sao cần file này: site đăng nhập bằng **Google/Shopee OAuth** (không có API email+mật khẩu),
nên tool KHÔNG thể tự "gõ mật khẩu". Thay vào đó ta giữ sẵn một **hồ sơ trình duyệt đã đăng
nhập Google** (thư mục `.pw-profile/`). Mỗi lần cần token, chỉ mở lại site: phiên Google còn
sống → site tự đăng nhập lại → ta chộp `access_token` (Bearer trong header request) + cookie
`shop_refresh_token` mới nhất, trả về cho tool.

Nhờ vậy khi bạn vào web làm việc (làm server tăng `ver`, giết phiên cũ của tool), lần chạy kế
tiếp tool chỉ cần mở trình duyệt là **tự lành** — không phải copy cURL tay nữa.

Chạy trực tiếp:
  python browser_login.py setup   # LẦN ĐẦU: mở cửa sổ, bạn tự đăng nhập Google 1 lần
  python browser_login.py test    # thử tự lấy token headless (sau khi đã setup)
"""
import base64
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(HERE, ".pw-profile")  # hồ sơ Chromium bền (giữ phiên Google) — .gitignore
SITE_URL = "https://hoantienshopee.me/"
API_HOST = "api.hoantienshopee.me"
REFRESH_COOKIE = "shop_refresh_token"


class NeedsInteractiveLogin(Exception):
    """Headless mở được nhưng chưa đăng nhập (phiên Google hết) → cần chạy `setup` mở tay."""


def _looks_like_shop_jwt(tok):
    """True nếu chuỗi là JWT của site (payload có typ='shop' và exp)."""
    try:
        parts = tok.split(".")
        if len(parts) != 3:
            return False
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        d = json.loads(base64.urlsafe_b64decode(payload))
        return d.get("typ") == "shop" and "exp" in d
    except Exception:
        return False


def _jwt_seconds_left(tok):
    try:
        parts = tok.split(".")
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        d = json.loads(base64.urlsafe_b64decode(payload))
        return d.get("exp", 0) - time.time()
    except Exception:
        return None


def _refresh_cookie(ctx):
    for c in ctx.cookies():
        if c.get("name") == REFRESH_COOKIE:
            return c.get("value")
    return None


def acquire(user_agent, headless=True, wait_login_s=30, settle_s=4):
    """Mở trình duyệt (hồ sơ bền) → chờ đăng nhập → trả {access_token, refresh_token}.

    - headless=True: dùng cho lần chạy tự động (Task Scheduler). Nếu chưa đăng nhập sẵn thì
      raise NeedsInteractiveLogin.
    - Chộp access token từ header `Authorization: Bearer` của BẤT KỲ request nào tới API
      (bền vững, khỏi cần biết SPA lưu token ở đâu). Lấy bản mới nhất (server xoay token liên tục).
    """
    from playwright.sync_api import sync_playwright

    captured = {"access": None}

    def on_request(req):
        try:
            if API_HOST in req.url:
                auth = req.headers.get("authorization", "")
                if auth[:7].lower() == "bearer ":
                    tok = auth[7:].strip()
                    if _looks_like_shop_jwt(tok):
                        captured["access"] = tok  # ghi đè → giữ token mới nhất
        except Exception:
            pass

    def wire(pg):
        pg.on("request", on_request)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=headless,
            user_agent=user_agent,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            ctx.on("page", wire)  # bắt cả popup OAuth
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            wire(page)
            page.goto(SITE_URL, wait_until="domcontentloaded", timeout=45000)

            # Chờ tới khi có cookie refresh (dấu hiệu đã đăng nhập) VÀ chộp được access token.
            deadline = time.time() + wait_login_s
            rt = None
            while time.time() < deadline:
                rt = _refresh_cookie(ctx)
                if rt and captured["access"]:
                    break
                page.wait_for_timeout(500)

            if not rt:
                raise NeedsInteractiveLogin(
                    "Chưa đăng nhập trong hồ sơ trình duyệt. Chạy: python hoantien_checkin.py browser-login"
                )

            # Có cookie nhưng chưa chộp được Bearer → nán thêm cho app gọi API rồi thử lại.
            waited = 0
            while not captured["access"] and waited < settle_s:
                page.wait_for_timeout(500)
                waited += 0.5
            # Chộp cookie mới nhất ngay trước khi đóng (server có thể vừa xoay).
            rt = _refresh_cookie(ctx) or rt

            return {"access_token": captured["access"], "refresh_token": rt}
        finally:
            ctx.close()  # đóng để Chromium lưu lại hồ sơ (phiên Google) vào .pw-profile


def interactive_setup(user_agent, wait_login_s=300):
    """LẦN ĐẦU: mở cửa sổ thật để bạn tự đăng nhập Google. Chờ tối đa 5 phút."""
    print(
        "→ Cửa sổ trình duyệt đang mở. Hãy ĐĂNG NHẬP hoantienshopee.me bằng Google/Shopee.\n"
        "  Đăng nhập xong cứ để yên vài giây, script sẽ tự lấy token rồi đóng.\n"
        f"  (Chờ tối đa {wait_login_s // 60} phút.)"
    )
    return acquire(user_agent, headless=False, wait_login_s=wait_login_s, settle_s=6)


def _load_ua():
    creds_path = os.path.join(HERE, "creds.json")
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    try:
        with open(creds_path, encoding="utf-8") as f:
            ua = json.load(f).get("user_agent") or ua
    except Exception:
        pass
    return ua


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "setup"
    ua = _load_ua()
    if cmd == "setup":
        got = interactive_setup(ua)
    elif cmd == "test":
        got = acquire(ua, headless=True)
    else:
        sys.exit("Dùng: setup | test")
    tok = got.get("access_token")
    left = _jwt_seconds_left(tok) if tok else None
    print(json.dumps({
        "access_token": (tok[:32] + "…") if tok else None,
        "access_ttl_s": int(left) if left else None,
        "refresh_token": (got.get("refresh_token", "")[:16] + "…") if got.get("refresh_token") else None,
    }, ensure_ascii=False, indent=2))
    if not tok:
        sys.exit("⚠️ Không chộp được access token.")
