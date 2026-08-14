"""Điểm danh saffi qua TRÌNH DUYỆT THẬT (Playwright) để đi qua cổng Cloudflare Turnstile.

Bối cảnh: từ 2026-08-10 app.saffi.vn thêm Turnstile. Request điểm danh giờ là
`POST /api/spoint/checkin` với body `{cf_turnstile_response: <token>}`, token do widget
Cloudflare sinh ra và chỉ hợp lệ trên domain saffi + phiên trình duyệt thật. Thay vì tự "chế"
token, module này điều khiển CHÍNH trình duyệt đã đăng nhập của bạn **bấm nút "Điểm danh"** —
site tự lo việc lấy token & gọi API. Về bản chất là một macro trên tài khoản của bạn.

Phiên đăng nhập được nạp bằng cách seed cookie từ creds.json (saffi_api_session + XSRF-TOKEN)
vào một hồ sơ Chromium bền (`.pw-profile/`, đã .gitignore). Khi cookie hết hạn, chạy
`python saffi_checkin.py browser-login` để đăng nhập lại 1 lần (mở cửa sổ).

Chạy trực tiếp:
  python saffi_browser.py checkin   # điểm danh ngay qua trình duyệt (headful)
  python saffi_browser.py login     # mở cửa sổ đăng nhập tay, lưu hồ sơ
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta

ICT_OFFSET_HOURS = 7  # server saffi reset theo 00:00 giờ VN (ICT = UTC+7)

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(HERE, ".pw-profile")        # hồ sơ Chromium do Playwright quản (cách cũ)
CHROME_PROFILE = os.path.join(HERE, ".chrome-profile")  # hồ sơ Chrome THẬT cho chế độ CDP (bền, .gitignore)
CDP_PORT = 9222
CREDS_PATH = os.path.join(HERE, "creds.json")
SITE_HOST = "app.saffi.vn"
QUATANG_URL = f"https://{SITE_HOST}/qua-tang"
LOGIN_URL = f"https://{SITE_HOST}/dang-nhap"
CHECKIN_PATH = "/api/spoint/checkin"

# Nhãn nút điểm danh — xác nhận từ CheckinHero chunk là "Điểm danh ngay". Vài biến thể phòng hờ.
# KHÔNG để "Điểm danh" trần (trùng heading "Điểm Danh Nhận S-Point" và text khác trên trang).
CHECKIN_BTN_TEXTS = ["Điểm danh ngay", "Điểm danh hôm nay"]


class NeedsInteractiveLogin(Exception):
    """Trang bị đá về /login → phiên hết hạn, cần chạy `login` mở tay đăng nhập lại."""


def _load_creds():
    with open(CREDS_PATH, encoding="utf-8") as f:
        return json.load(f)


def _is_login_page(url):
    return "/dang-nhap" in url or "/login" in url


_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || {runtime: {}};
Object.defineProperty(navigator, 'languages', {get: () => ['vi-VN','vi','en-US','en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
try { Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 1}); } catch(e){}
"""


def _seed_session(ctx, creds):
    """Nạp phiên đăng nhập: cookie (Sanctum) + bearer token vào localStorage['token'].
    SPA saffi đọc token từ localStorage để xác thực → thiếu nó sẽ bị đá về /dang-nhap.
    Kèm stealth để giảm khả năng Cloudflare Turnstile chặn vì phát hiện automation."""
    _seed_cookies(ctx, creds)
    ctx.add_init_script(_STEALTH_JS)
    tok = creds.get("bearer_token") or ""
    if tok:
        # init-script chạy TRƯỚC script trang trên mỗi lần điều hướng → token có sẵn khi app kiểm tra.
        ctx.add_init_script(f"try{{localStorage.setItem('token', {json.dumps(tok)});}}catch(e){{}}")


def _launch(p, creds, headless, fresh):
    """Trả (ctx, browser_or_None). fresh=True → context SẠCH (ephemeral, không đụng .pw-profile) —
    dùng để test tài khoản phụ, tránh bị phiên đã lưu trong profile đè lên cookie seed.

    QUAN TRỌNG cho Turnstile: dùng CHANNEL 'chrome' (Chrome thật, không phải Chromium bundled) và
    BỎ cờ '--enable-automation' để navigator.webdriver=false → Cloudflare khó phát hiện automation.
    Nếu máy không có Chrome thì fallback về Chromium bundled (nhiều khả năng bị Turnstile chặn)."""
    args = ["--disable-blink-features=AutomationControlled"]
    ignore = ["--enable-automation"]
    vp = {"width": 1180, "height": 860}

    def _open(channel):
        if fresh:
            b = p.chromium.launch(headless=headless, args=args, channel=channel, ignore_default_args=ignore)
            ctx = b.new_context(user_agent=creds["user_agent"], viewport=vp)
            _seed_session(ctx, creds)
            return ctx, b
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=headless, user_agent=creds["user_agent"], viewport=vp,
            args=args, channel=channel, ignore_default_args=ignore,
        )
        _seed_session(ctx, creds)
        return ctx, None

    try:
        return _open("chrome")
    except Exception as e:
        print(f"(⚠️ không mở được Chrome thật: {str(e)[:80]} → dùng Chromium bundled)", flush=True)
        return _open(None)


def _seed_cookies(ctx, creds):
    """Nạp phiên đăng nhập từ creds.json vào context để không phải đăng nhập tay."""
    cookies = []
    for part in (creds.get("cookies") or "").split(";"):
        if "=" not in part:
            continue
        name, _, value = part.strip().partition("=")
        if name in ("saffi_api_session", "XSRF-TOKEN"):
            cookies.append({
                "name": name, "value": value, "domain": SITE_HOST, "path": "/",
                "httpOnly": name == "saffi_api_session", "secure": True, "sameSite": "Lax",
            })
    if cookies:
        ctx.add_cookies(cookies)


def _first_clickable(loc, max_n=6):
    """Trả (locator, label) cho phần tử hiển thị đầu tiên trong `loc`, hoặc (None, None)."""
    try:
        n = min(loc.count(), max_n)
    except Exception:
        n = 0
    for i in range(n):
        b = loc.nth(i)
        try:
            if b.is_visible():
                label = (b.inner_text() or "").strip()
                if "lịch sử" in label.lower():
                    continue
                return b, label
        except Exception:
            continue
    return None, None


def _dismiss_popups(page):
    """Đóng các popup/modal (vd modal 'Giới thiệu bạn bè') đang che nút điểm danh → nếu không
    click nút sẽ bị timeout vì bị overlay chặn pointer."""
    for sel in [".ant-modal-close", "button:has-text('Đóng')", "[aria-label='Close']"]:
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 4)):
                b = loc.nth(i)
                if b.is_visible():
                    b.click(timeout=2000)
                    page.wait_for_timeout(400)
        except Exception:
            continue
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


def _click_btn(btn):
    try:
        btn.click(timeout=6000)
    except Exception:
        try:
            btn.click(timeout=2500, force=True)
        except Exception:
            pass


def _checkin_state(page):
    """'done' nếu trang báo đã điểm danh hôm nay; 'waiting' nếu chưa (đang chờ xác thực/cửa mở)."""
    try:
        txt = (page.evaluate("() => document.body.innerText || ''") or "").lower()
    except Exception:
        return "unknown"
    if "đã điểm danh hôm nay" in txt:
        return "done"
    return "waiting"


def _has_text(page, needle):
    """True nếu innerText trang có chứa `needle` (không phân biệt hoa thường)."""
    try:
        txt = (page.evaluate("() => document.body.innerText || ''") or "").lower()
        return needle.lower() in txt
    except Exception:
        return False


def _find_checkin_button(page):
    """Trả về locator nút 'Điểm danh ngay' đang hiển thị, hoặc (None, None).

    Nút nằm trong component CheckinHero; token Turnstile invisible sinh async nên chỉ hiện khi
    CHƯA điểm danh hôm nay (checked-in-today=false). Thử role=button trước, rồi fallback theo text
    (phòng khi là div/span có @click chứ không phải <button> ngữ nghĩa)."""
    for txt in CHECKIN_BTN_TEXTS:
        b, label = _first_clickable(page.get_by_role("button", name=txt, exact=False))
        if b is not None:
            return b, label
    for txt in CHECKIN_BTN_TEXTS:
        b, label = _first_clickable(page.get_by_text(txt, exact=False))
        if b is not None:
            return b, label
    return None, None


def _checkin_ict_date(checkin_date_str):
    """checkin_date server ở UTC (vd '2026-08-11T17:00:00Z' = 00:00 ICT ngày 12). Trả 'YYYY-MM-DD'
    theo lịch ICT, hoặc None nếu không parse được."""
    if not checkin_date_str:
        return None
    try:
        s = checkin_date_str.replace("Z", "")[:19]
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S") + timedelta(hours=ICT_OFFSET_HOURS)
        return dt.date().isoformat()
    except Exception:
        return None


def _target_today_ict(now=None):
    """Ngày ICT ta muốn điểm danh (nếu đang 23h thì là ngày mai)."""
    now = now or datetime.now()
    d = (now + timedelta(minutes=30)).date() if now.hour == 23 else now.date()
    return d.isoformat()


def _in_rollover_window(now=None):
    """00:00–00:09 ICT: saffi CHƯA chắc đã rollover sang ngày mới (mở cửa ~00:03–00:08). Trong
    khoảng này không tin trạng thái 'đã điểm danh' của trang (có thể là của ngày cũ)."""
    now = now or datetime.now()
    return now.hour == 0 and now.minute < 10


def _attach_capture(page, captured, target=None):
    """Bắt response POST /spoint/checkin. CHỈ coi là thành công khi checkin_date ĐÚNG ngày target
    (ICT) — thành công cho ngày CŨ (server chưa rollover lúc ~00:00) chỉ đánh dấu recovered_prev
    để vòng lặp tiếp tục chờ cửa hôm nay, tránh báo nhầm 'thành công' mà thực chưa điểm danh."""
    def on_response(resp):
        try:
            if CHECKIN_PATH in resp.url and resp.request.method == "POST":
                try:
                    body = resp.json()
                except Exception:
                    body = {"httpError": resp.status, "body": resp.text()[:300]}
                if isinstance(body, dict) and body.get("httpError") is None and resp.status >= 400:
                    body["httpError"] = resp.status
                captured["last"] = body
                ok = isinstance(body, dict) and (body.get("success") or body.get("status") == "success")
                if ok:
                    cd = ((body.get("data") or {}).get("checkin") or {}).get("checkin_date")
                    d = _checkin_ict_date(cd)
                    if target is None or d is None or d == target:
                        captured["success"] = body
                    else:
                        captured["recovered_prev"] = True  # success NGÀY CŨ → chưa xong hôm nay
                elif isinstance(body, dict) and resp.status in (409, 422):
                    # 409/422 = "đã điểm danh". Trong cửa sổ rollover có thể là của ngày cũ → không
                    # coi là xong; ngoài cửa sổ đó thì tin là đã điểm danh hôm nay.
                    if not _in_rollover_window():
                        captured["success"] = body
                    else:
                        captured["recovered_prev"] = True
        except Exception:
            pass
    page.on("response", on_response)


def _wait_and_click(page, captured, deadline_s, log):
    """Logic site 2026-08: Turnstile invisible TỰ giải khi load (~5-7s); nút 'Chờ xác thực...' →
    'Điểm danh ngay' khi token sẵn sàng → bấm 1 lần emit token thật. KHÔNG reload (reset xác thực).
    Chỉ dừng khi có checkin ĐÚNG ngày hôm nay (xem _attach_capture)."""
    deadline = time.time() + deadline_s
    attempts = 0
    last_reload = time.time()
    last_hb = 0
    while time.time() < deadline:
        if captured["success"]:
            log("✅ server đã nhận điểm danh.")
            return captured["success"]
        _dismiss_popups(page)
        btn, label = _find_checkin_button(page)  # chỉ khớp 'Điểm danh ngay' (token đã sẵn)
        if btn is None:
            state = _checkin_state(page)
            # Heartbeat mỗi ~5s để soi được nó kẹt ở đâu (chờ xác thực / cửa chưa mở / done ngày cũ).
            if time.time() - last_hb > 5:
                verifying = _has_text(page, "chờ xác thực")
                log(f"   … state={state} verifying={verifying} url={page.url[-30:]} rollover={_in_rollover_window()}")
                last_hb = time.time()
            # 'done' NGOÀI cửa sổ rollover mới tin là đã điểm danh hôm nay. TRONG rollover (00:00–00:09)
            # trang có thể báo "done" cho NGÀY CŨ → phải chờ cửa hôm nay mở, đừng dừng sớm.
            if state == "done" and not _in_rollover_window():
                log("ℹ️ Đã điểm danh hôm nay.")
                return captured["success"] or {"success": True, "note": "already"}
            page.wait_for_timeout(300)  # poll sát để bấm gần như tức thì khi nút mở / cửa mở
            # Lúc rollover reload nhanh hơn (bắt đúng thời điểm server mở cửa); ngoài ra reload chậm.
            reload_gap = 12 if _in_rollover_window() else 45
            if time.time() - last_reload > reload_gap:
                log("… reload để bắt cửa mở / re-render Turnstile…")
                try:
                    page.reload(wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(2500)
                except Exception:
                    pass
                last_reload = time.time()
            continue
        attempts += 1
        log(f"→ [{attempts}] bấm '{label}' (token đã sẵn sàng)")
        _click_btn(btn)
        waited = 0
        while waited < 8 and not captured["success"]:
            page.wait_for_timeout(300)
            waited += 0.3
    # Hết deadline: ưu tiên success hôm nay; nếu chỉ vớt được ngày cũ → báo rõ CHƯA xong hôm nay.
    if captured["success"]:
        return captured["success"]
    if captured.get("recovered_prev"):
        return {"success": False, "recovered_prev": True,
                "message": "Chỉ điểm danh được NGÀY CŨ — cửa hôm nay chưa mở kịp trong thời hạn."}
    return captured["last"]


# ---------- Chế độ CDP: Chrome THẬT (process riêng) + Playwright kết nối vào để bấm ----------
# Cloudflare Turnstile phát hiện trình duyệt do Playwright KHỞI ĐỘNG và không giải challenge. Nếu ta
# tự chạy Chrome thật như người dùng rồi CHỈ kết nối vào qua remote-debugging để bấm, Turnstile thấy
# trình duyệt thật → giải như thường. Đây là cách vượt Turnstile bền nhất.

def _chrome_exe():
    for c in [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]:
        if os.path.isfile(c):
            return c
    return shutil.which("chrome") or shutil.which("chrome.exe")


def _spawn_chrome(url, port=CDP_PORT):
    exe = _chrome_exe()
    if not exe:
        raise RuntimeError("Không tìm thấy Google Chrome. Cài Chrome rồi thử lại.")
    args = [
        exe, f"--remote-debugging-port={port}", f"--user-data-dir={CHROME_PROFILE}",
        "--no-first-run", "--no-default-browser-check", "--start-maximized", url,
    ]
    return subprocess.Popen(args)


def _wait_cdp(port=CDP_PORT, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1).read()
            return True
        except Exception:
            time.sleep(0.5)
    return False


def _seed_cdp_session(ctx, page, creds):
    """Nạp phiên từ creds.json vào Chrome (CDP): cookie Sanctum + localStorage['token']. Dùng để
    TỰ-LÀNH khi phiên hồ sơ .chrome-profile hết hạn — khỏi phải cdp-login tay. Chỉ có tác dụng nếu
    bearer_token/cookies trong creds.json còn sống (token Sanctum saffi thường sống lâu)."""
    try:
        _seed_cookies(ctx, creds)  # add_cookies XSRF-TOKEN + saffi_api_session cho app.saffi.vn
    except Exception:
        pass
    tok = creds.get("bearer_token") or ""
    if tok:
        try:
            if SITE_HOST not in page.url:
                page.goto(f"https://{SITE_HOST}/", wait_until="domcontentloaded", timeout=30000)
            page.evaluate("(t) => { try { localStorage.setItem('token', t); } catch(e){} }", tok)
        except Exception:
            pass


def checkin_cdp(creds=None, port=CDP_PORT, deadline_s=180, verbose=True, auto_launch=True,
                keep_open=False, settle_s=8):
    """Điểm danh qua Chrome THẬT: tự chạy Chrome (hồ sơ .chrome-profile đã đăng nhập) với remote
    debugging, Playwright connect_over_cdp rồi chờ nút 'Điểm danh ngay' và bấm — vượt Turnstile.
    LẦN ĐẦU phải đăng nhập: python saffi_checkin.py cdp-login.

    settle_s: số giây chờ Turnstile TỰ giải khi browser còn 'sạch' (chưa attach CDP) trước khi kết
    nối. Gắn CDP trong lúc challenge đang chạy dễ bị Cloudflare chặn → đừng để quá nhỏ. Mặc định 8s
    (nút thường mở ~5-7s); có thể thử giảm còn ~5s nếu muốn nhanh hơn, nhưng phải test kỹ."""
    from playwright.sync_api import sync_playwright

    creds = creds or _load_creds()
    captured = {"last": None, "success": None}
    log_path = os.path.join(HERE, "saffi_cdp.log")

    def log(m):
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {m}"
        if verbose:
            print(m, flush=True)
        try:  # luôn ghi ra file để soi được cả khi chạy nền (Task Scheduler)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    proc = None
    if auto_launch:
        log("→ khởi động Chrome thật (remote-debugging)…")
        proc = _spawn_chrome(QUATANG_URL, port)
        if not _wait_cdp(port):
            log("❌ Không mở được cổng debug — có thể Chrome đang chạy sẵn bằng hồ sơ khác. "
                "Đóng hết Chrome rồi thử lại.")
        # Chờ Turnstile TỰ giải khi browser còn "sạch" (chưa attach CDP) → tăng tỉ lệ qua challenge.
        log(f"→ chờ Turnstile tự giải (~{settle_s}s) trước khi kết nối…")
        time.sleep(settle_s)
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = next((pg for pg in ctx.pages if SITE_HOST in pg.url), None)
            page = page or (ctx.pages[0] if ctx.pages else ctx.new_page())
            _attach_capture(page, captured, target=_target_today_ict())
            if "qua-tang" not in page.url:
                page.goto(QUATANG_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            if _is_login_page(page.url):
                # Phiên hồ sơ hết hạn → thử TỰ nạp lại từ creds.json rồi vào lại (khỏi cdp-login tay).
                log("→ phiên Chrome hết hạn — thử tự nạp lại từ creds.json…")
                _seed_cdp_session(ctx, page, creds)
                page.goto(QUATANG_URL, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
                if _is_login_page(page.url):
                    raise NeedsInteractiveLogin(
                        "Chrome (CDP) chưa đăng nhập và creds.json cũng hết hạn. "
                        "Chạy 1 lần: python saffi_checkin.py cdp-login"
                    )
                log("→ tự nạp phiên OK.")
            return _wait_and_click(page, captured, deadline_s, log)
    finally:
        if proc and not keep_open:
            try:
                proc.terminate()
            except Exception:
                pass


def cdp_login(port=CDP_PORT, wait_s=300):
    """LẦN ĐẦU: mở Chrome thật (hồ sơ .chrome-profile) để bạn đăng nhập saffi. Hồ sơ giữ lại cho các
    lần chạy CDP sau. Chờ tối đa wait_s giây rồi tự đóng (hoặc bạn tự đóng cửa sổ)."""
    print(
        f"→ Chrome đang mở với hồ sơ riêng. ĐĂNG NHẬP {SITE_HOST} như bình thường.\n"
        f"  Đăng nhập xong cứ để yên (hồ sơ tự lưu). Cửa sổ tự đóng sau {wait_s // 60}' — hoặc bạn tự đóng.",
        flush=True,
    )
    proc = _spawn_chrome(QUATANG_URL, port)  # chưa login sẽ tự redirect sang /dang-nhap
    try:
        proc.wait(timeout=wait_s)
    except Exception:
        pass
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


def checkin(creds=None, headless=False, deadline_s=600, poll_gap_s=5, verbose=True, fresh=False):
    """Mở qua-tang (đã đăng nhập) → bấm nút Điểm danh lặp lại tới khi server nhận (hoặc timeout).

    Trả về dict payload của response /spoint/checkin (giống API), hoặc None nếu không được.
    Mặc định headful (headless=False) vì Turnstile managed tin trình duyệt thật hơn.
    fresh=True → context sạch (test tài khoản phụ, không dùng .pw-profile).
    """
    from playwright.sync_api import sync_playwright

    creds = creds or _load_creds()
    captured = {"last": None, "success": None}

    def on_response(resp):
        try:
            if CHECKIN_PATH in resp.url and resp.request.method == "POST":
                try:
                    body = resp.json()
                except Exception:
                    body = {"httpError": resp.status, "body": resp.text()[:300]}
                if isinstance(body, dict) and body.get("httpError") is None and resp.status >= 400:
                    body["httpError"] = resp.status
                captured["last"] = body
                ok = isinstance(body, dict) and (body.get("success") or body.get("status") == "success")
                already = isinstance(body, dict) and resp.status in (409, 422)
                if ok or already:
                    captured["success"] = body
        except Exception:
            pass

    def log(m):
        if verbose:
            print(m, flush=True)

    with sync_playwright() as p:
        ctx, browser = _launch(p, creds, headless, fresh)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.on("response", on_response)
            page.goto(QUATANG_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)  # cho SPA render + Turnstile nạp

            if _is_login_page(page.url):
                raise NeedsInteractiveLogin(
                    "Bị đá về /dang-nhap — phiên saffi hết hạn. Chạy: python saffi_checkin.py browser-login"
                )

            # Logic MỚI của site (2026-08): Turnstile invisible TỰ giải khi load (~5-7s). Nút hiện
            # "Chờ xác thực..." rồi chuyển "Điểm danh ngay" khi token sẵn sàng; bấm 1 lần là emit
            # token thật. TUYỆT ĐỐI KHÔNG reload (reload reset lại quá trình xác thực).
            deadline = time.time() + deadline_s
            attempts = 0
            last_reload = time.time()
            while time.time() < deadline:
                if captured["success"]:
                    log("✅ server đã nhận điểm danh.")
                    return captured["success"]
                _dismiss_popups(page)  # đóng modal che nút (vd 'Giới thiệu bạn bè')
                btn, label = _find_checkin_button(page)  # chỉ khớp "Điểm danh ngay" (token đã sẵn)
                if btn is None:
                    # Đang "Chờ xác thực..." / cửa chưa mở → CHỜ, không reload để khỏi reset xác thực.
                    st = _checkin_state(page)
                    if st == "done":
                        log("ℹ️ Đã điểm danh hôm nay.")
                        return captured["success"] or {"success": True, "note": "already"}
                    page.wait_for_timeout(800)
                    # Chỉ reload khi kẹt quá lâu (token lỗi/không tới) để buộc re-render Turnstile.
                    if time.time() - last_reload > 45:
                        log("… kẹt lâu, reload để re-render Turnstile…")
                        try:
                            page.reload(wait_until="domcontentloaded", timeout=30000)
                            page.wait_for_timeout(2500)
                        except Exception:
                            pass
                        last_reload = time.time()
                    continue

                # Nút đã sẵn sàng → token có sẵn, bấm 1 lần để emit.
                attempts += 1
                log(f"→ [{attempts}] bấm '{label}' (token đã sẵn sàng)")
                _click_btn(btn)
                waited = 0
                while waited < 8 and not captured["success"]:
                    page.wait_for_timeout(300)
                    waited += 0.3

            return captured["success"] or captured["last"]
        finally:
            ctx.close()
            if browser:
                browser.close()


def interactive_login(creds=None, wait_login_s=300):
    """Mở cửa sổ thật để bạn đăng nhập app.saffi.vn 1 lần → lưu hồ sơ vào .pw-profile."""
    from playwright.sync_api import sync_playwright

    creds = creds or _load_creds()
    print(
        f"→ Cửa sổ đang mở. ĐĂNG NHẬP {SITE_HOST} như bình thường.\n"
        f"  Đăng nhập xong cứ để yên, hồ sơ sẽ tự lưu (chờ tối đa {wait_login_s // 60}').",
        flush=True,
    )
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=False, user_agent=creds["user_agent"],
            viewport={"width": 1180, "height": 860},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            _seed_session(ctx, creds)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(QUATANG_URL, wait_until="domcontentloaded", timeout=45000)
            deadline = time.time() + wait_login_s
            while time.time() < deadline:
                if not _is_login_page(page.url):
                    # Có vẻ đã đăng nhập; chờ thêm chút cho chắc rồi thoát khi user đóng tab.
                    pass
                page.wait_for_timeout(1000)
                if len(ctx.pages) == 0:
                    break
            print("→ Đã lưu hồ sơ .pw-profile.", flush=True)
        finally:
            ctx.close()


def diag(creds=None, fresh=False):
    """Chẩn đoán: mở qua-tang, in mọi nút bấm + phần tử chứa 'điểm danh', chụp ảnh trang.
    Dùng để tìm đúng selector nút điểm danh (chạy vào ngày CHƯA điểm danh sẽ rõ nhất).
    fresh=True → context sạch (test tài khoản phụ, không dùng .pw-profile)."""
    from playwright.sync_api import sync_playwright

    creds = creds or _load_creds()
    shot = os.path.join(HERE, "saffi_diag.png")
    with sync_playwright() as p:
        ctx, browser = _launch(p, creds, False, fresh)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(QUATANG_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            print("URL:", page.url)
            print("Đăng nhập:", "CHƯA (bị đá /dang-nhap)" if _is_login_page(page.url) else "OK")
            info = page.evaluate(r"""() => {
              const clean = s => (s||'').replace(/\s+/g,' ').trim().slice(0,60);
              const btns = [...document.querySelectorAll('button, a, [role=button], .btn')].map(el => ({
                tag: el.tagName.toLowerCase(),
                text: clean(el.innerText),
                cls: clean(el.className),
                disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
                visible: !!(el.offsetParent) || getComputedStyle(el).display !== 'none',
              })).filter(b => b.text || b.cls);
              const dd = [...document.querySelectorAll('*')].filter(el =>
                /điểm danh|checkin|check-in/i.test(el.innerText||'') &&
                (el.children.length === 0 || /điểm danh/i.test(el.getAttribute('class')||''))
              ).slice(0,20).map(el => ({tag: el.tagName.toLowerCase(), text: clean(el.innerText), cls: clean(el.className)}));
              const turnstile = typeof window.turnstile !== 'undefined';
              return {btns, dd, turnstile, key: window.TURNSTILE_SITE_KEY};
            }""")
            print("\n=== window.turnstile sẵn sàng:", info.get("turnstile"), "| sitekey:", info.get("key"))
            print("\n=== BUTTONS / clickable ===")
            for b in info["btns"]:
                print(f"  [{b['tag']}] vis={b['visible']} dis={b['disabled']} text={b['text']!r} cls={b['cls']!r}")
            print("\n=== Phần tử chứa 'điểm danh' ===")
            for d in info["dd"]:
                print(f"  [{d['tag']}] text={d['text']!r} cls={d['cls']!r}")
            page.screenshot(path=shot, full_page=True)
            print("\n📸 Ảnh trang:", shot)
            print("→ Giữ cửa sổ 20s để bạn xem…")
            page.wait_for_timeout(20000)
        finally:
            ctx.close()
            if browser:
                browser.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "checkin"
    arg2 = sys.argv[2] if len(sys.argv) > 2 else None
    # arg2 là file creds phụ cho checkin/diag/login; nhưng là settle_s (số) cho cdp.
    alt = arg2 if cmd in ("checkin", "diag", "login") else None
    creds = json.load(open(alt, encoding="utf-8")) if alt else None
    fresh = alt is not None  # creds phụ → context sạch, không dùng .pw-profile của acc chính
    if cmd == "checkin":
        res = checkin(creds, headless=False, fresh=fresh)
        print(json.dumps(res, ensure_ascii=False)[:600] if res else "None")
    elif cmd == "cdp":
        # Tùy chọn: settle_s (giây chờ Turnstile giải trước khi attach), vd: python saffi_browser.py cdp 5
        settle = float(arg2) if arg2 else 8
        res = checkin_cdp(creds, settle_s=settle)
        print(json.dumps(res, ensure_ascii=False)[:600] if res else "None")
    elif cmd == "cdp-login":
        cdp_login()
    elif cmd == "login":
        interactive_login(creds)
    elif cmd == "diag":
        diag(creds, fresh=fresh)
    else:
        sys.exit("Dùng: checkin | cdp | cdp-login | login | diag  [creds_khac.json]")
