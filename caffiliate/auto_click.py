"""Auto-click checkin button qua pyautogui.
Cách chạy Chrome bình thường (không Selenium) → Turnstile pass tự động → click nút bằng OS-level mouse event.

Task Scheduler nên chạy script này lúc ~23:57.

Setup lần đầu:
  1. `python auto_click.py capture`  → chụp template ảnh nút NHẬN QUÀ
  2. `python auto_click.py test`     → test click ngay (không chờ midnight)
  3. Setup Task Scheduler chạy `python auto_click.py run` mỗi tối 23:57

File deps:
  - button_template.png     : ảnh nút NHẬN QUÀ (enable state) — auto-capture
  - click_config.json        : thời điểm fire, offset...
  - creds.json               : dùng verify status sau khi click
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, time as dt_time, timedelta

import pyautogui

# Reuse telegram + status helpers từ auto_tool.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auto_tool import (
    send_telegram,
    get_checkin_status,
    load_creds_from_file,
    wait_until_next_midnight,
)

REWARDS_URL = "https://app.caffiliate.vn/rewards"
PROFILE_DIR = r"C:\SeleniumChromeProfile"

# Chrome path — auto-detect
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe"),
]

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(HERE, "button_template.png")
CONFIG_PATH = os.path.join(HERE, "click_config.json")

# Timing config
PRE_LOAD_MINUTES_BEFORE = 3       # mở Chrome sớm ngần này phút trước midnight
FIRE_OFFSET_MS = -200             # click trước midnight bao nhiêu ms
BUTTON_SEARCH_TIMEOUT = 60        # tối đa chờ nút xuất hiện sau khi Chrome load
IMAGE_MATCH_CONFIDENCE = 0.75     # ngưỡng nhận diện nút (0-1)

pyautogui.FAILSAFE = True  # Move mouse to corner để abort


def find_chrome():
    for p in CHROME_PATHS:
        if os.path.isfile(p):
            return p
    raise FileNotFoundError("Không tìm thấy chrome.exe. Sửa CHROME_PATHS.")


def load_config():
    if os.path.isfile(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"fire_offset_ms": FIRE_OFFSET_MS}


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


CHROME_WINDOW_POS = "0,0"
CHROME_WINDOW_SIZE = "1920,1080"
CDP_PORT = 9222  # remote debugging port for CDP click (bypass lock screen)


def launch_chrome(url=REWARDS_URL):
    """Launch Chrome với remote-debugging-port để click qua CDP khi lock screen."""
    chrome = find_chrome()
    cmd = [
        chrome,
        f"--user-data-dir={PROFILE_DIR}",
        "--new-window",
        "--disable-features=Translate",
        f"--window-position={CHROME_WINDOW_POS}",
        f"--window-size={CHROME_WINDOW_SIZE}",
        f"--remote-debugging-port={CDP_PORT}",
        "--remote-allow-origins=*",  # Chrome mới bắt buộc để chấp nhận WebSocket từ localhost
        url,
    ]
    proc = subprocess.Popen(cmd)
    return proc


def _cdp_get_ws():
    """Lấy WebSocket connection tới caffiliate tab."""
    import urllib.request
    from websocket import create_connection

    tabs = json.loads(urllib.request.urlopen(f"http://localhost:{CDP_PORT}/json", timeout=5).read())
    caffi_tab = None
    for t in tabs:
        url = t.get("url", "").lower()
        if t.get("type") == "page" and "caffiliate" in url and "rewards" in url:
            caffi_tab = t
            break
    if not caffi_tab:
        raise RuntimeError(f"Không tìm thấy caffiliate tab. Tabs: {[t.get('url','')[:60] for t in tabs]}")
    return create_connection(caffi_tab["webSocketDebuggerUrl"], timeout=5)


def _cdp_send(ws, method, params=None, msg_id=1):
    """Send CDP command, return response."""
    ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    return json.loads(ws.recv())


def click_via_cdp(button_id="btnCheckIn"):
    """Single click via CDP. Return (success, message)."""
    try:
        ws = _cdp_get_ws()
        expression = f"(function(){{var b=document.getElementById('{button_id}');if(!b)return{{ok:false,err:'button not found'}};if(b.disabled)return{{ok:false,err:'button disabled'}};b.click();return{{ok:true}};}})()"
        result = _cdp_send(ws, "Runtime.evaluate", {"expression": expression, "returnByValue": True})
        ws.close()
        value = result.get("result", {}).get("result", {}).get("value", {})
        return (True, "CDP click OK") if value.get("ok") else (False, f"JS returned: {value}")
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}"


def fire_direct_post_via_cdp(fire_offset_ms=50):
    """PRE-FETCH tất cả tokens (Turnstile, signing, csrf) TRƯỚC midnight, POST trực tiếp AT midnight+offset_ms.
    Fire tại 00:00:00.050 → server nhận ~00:00:00.100 → new day → top 1-3.
    Trả về (success, message)."""
    try:
        ws = _cdp_get_ws()

        # 1. Inject setup + fire JS. Chạy trong browser context.
        # Target time = next UTC midnight + 7h (ICT) + fire_offset_ms
        # Since browser Date is client-local, use local midnight tomorrow + offset
        js = r"""
        (async function(){
            try {
                // Extract creds từ HTML (const app.state không expose ra window)
                const html = document.documentElement.outerHTML;
                const csrfMatch = html.match(/csrfToken:\s*'([^']+)'/);
                const userIdMatch = html.match(/googleId:\s*'([^']+)'/);
                if (!csrfMatch || !userIdMatch) return {ok:false, err:'creds regex fail'};
                const csrfToken = csrfMatch[1];
                const userId = userIdMatch[1];

                // Fetch signing token
                const signResp = await fetch('/api/v2/security/signature-token', {credentials:'same-origin', cache:'no-store'}).then(r=>r.json());
                if (!signResp.success || !signResp.data || !signResp.data.signatureToken) return {ok:false, err:'signing token fail: '+JSON.stringify(signResp).slice(0,150)};
                const signingToken = signResp.data.signatureToken;

                // Get Turnstile token
                if (!window.CheckInTurnstile || typeof window.CheckInTurnstile.getToken !== 'function') return {ok:false, err:'CheckInTurnstile not loaded'};
                const tsToken = await window.CheckInTurnstile.getToken();
                if (!tsToken) return {ok:false, err:'Turnstile getToken empty'};

                // Compute fire target: next midnight local + FIRE_OFFSET_MS
                const now = new Date();
                const target = new Date(now);
                target.setHours(24, 0, 0, FIRE_OFFSET_MS_PLACEHOLDER);

                // Wait until target
                while (Date.now() < target.getTime()) {
                    const rem = target.getTime() - Date.now();
                    if (rem > 100) await new Promise(r=>setTimeout(r, rem-50));
                    else await new Promise(r=>setTimeout(r, 1));
                }

                // Compute signature RIGHT BEFORE fire
                const ts = Date.now().toString();
                const nonce = Math.random().toString(36).substring(2, 15);
                const baseString = ts + '.' + nonce + '.' + userId;
                const encoder = new TextEncoder();
                const key = await crypto.subtle.importKey('raw', encoder.encode(signingToken), {name:'HMAC',hash:'SHA-256'}, false, ['sign']);
                const sig = await crypto.subtle.sign('HMAC', key, encoder.encode(baseString));
                const sigHex = Array.from(new Uint8Array(sig)).map(b=>b.toString(16).padStart(2,'0')).join('');

                // FIRE
                const fireStart = Date.now();
                const res = await fetch('/api/v2/xeng/check-in-secure', {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: {
                        'Content-Type': 'application/json',
                        'x-csrf-token': csrfToken,
                        'x-signature': sigHex,
                        'x-timestamp': ts,
                        'x-nonce': nonce
                    },
                    body: JSON.stringify({'cf-turnstile-response': tsToken})
                }).then(r=>r.json()).catch(e=>({success:false, error:String(e)}));
                const fireEnd = Date.now();

                return {
                    ok: !!res.success,
                    fireStart: new Date(fireStart).toISOString(),
                    fireEnd: new Date(fireEnd).toISOString(),
                    latencyMs: fireEnd - fireStart,
                    response: res
                };
            } catch (e) {
                return {ok:false, err:'exception: '+String(e).slice(0,200)};
            }
        })()
        """.replace("FIRE_OFFSET_MS_PLACEHOLDER", str(int(fire_offset_ms)))

        # JS block cho tới midnight+offset. Từ 23:57 tới 00:00 = ~183s.
        # WS timeout = 300s (5 min) để cover cả preload + wait + fire + response.
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": js, "awaitPromise": True, "returnByValue": True, "timeout": 300000},
        }))
        ws.settimeout(360)  # 6 phút, buffer đủ
        result = json.loads(ws.recv())
        ws.close()

        value = result.get("result", {}).get("result", {}).get("value", {})
        if value.get("ok"):
            return True, f"FIRE OK | fireStart={value.get('fireStart')} | latency={value.get('latencyMs')}ms | response={json.dumps(value.get('response'))[:200]}"
        else:
            return False, f"FIRE FAIL | value={json.dumps(value)[:400]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}"


def reload_and_click_via_cdp(button_id="btnCheckIn", max_attempts=15, poll_interval_ms=100):
    """Reload page, poll button state, click first moment button enabled.
    Chiến lược cho midnight rollover: state client cần sync với server ngày mới."""
    try:
        ws = _cdp_get_ws()

        # 1. Reload page (Page.reload)
        _cdp_send(ws, "Page.reload", msg_id=1)
        reload_ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[{reload_ts}] CDP Page.reload sent")

        # 2. Wait 800ms cho page bắt đầu load
        time.sleep(0.8)

        # 3. Poll button state + click ngay khi enabled
        click_expression = (
            f"(function(){{"
            f"var b=document.getElementById('{button_id}');"
            f"if(!b)return{{state:'not-found'}};"
            f"if(b.disabled||(b.classList&&b.classList.contains('pointer-events-none')))return{{state:'disabled'}};"
            f"b.click();"
            f"return{{state:'clicked',at:new Date().toISOString()}};"
            f"}})()"
        )
        for i in range(max_attempts):
            result = _cdp_send(ws, "Runtime.evaluate",
                              {"expression": click_expression, "returnByValue": True}, msg_id=100 + i)
            value = result.get("result", {}).get("result", {}).get("value", {})
            state = value.get("state")
            now_ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            if state == "clicked":
                ws.close()
                return True, f"Click OK sau {i+1} poll, at {value.get('at')} ({now_ts})"
            elif state == "not-found":
                # Page có thể chưa load xong
                pass
            time.sleep(poll_interval_ms / 1000.0)

        ws.close()
        return False, f"Poll {max_attempts} lần button vẫn disabled/not-found"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}"


def kill_chrome():
    """Kill toàn bộ Chrome (chỉ nếu bạn không dùng Chrome cho việc khác lúc này)."""
    subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True)


def locate_button():
    """Return center (x, y) của nút NHẬN QUÀ trên màn hình, hoặc None."""
    if not os.path.isfile(TEMPLATE_PATH):
        return None
    try:
        return pyautogui.locateCenterOnScreen(
            TEMPLATE_PATH,
            confidence=IMAGE_MATCH_CONFIDENCE,
            grayscale=False,
        )
    except pyautogui.ImageNotFoundException:
        return None
    except Exception as e:
        print(f"locate error: {e}")
        return None


def wait_for_button(timeout=BUTTON_SEARCH_TIMEOUT):
    """Chờ nút xuất hiện. Trả về (x, y) hoặc None nếu timeout."""
    print(f"Chờ nút xuất hiện (max {timeout}s)...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        pos = locate_button()
        if pos:
            print(f"  Tìm thấy nút tại {pos}")
            return pos
        time.sleep(0.5)
    return None


def precise_wait_until(target_datetime):
    """Sleep chính xác tới target_datetime."""
    while True:
        rem = (target_datetime - datetime.now()).total_seconds()
        if rem <= 0:
            return
        if rem > 5:
            time.sleep(min(rem - 1, 30))
        elif rem > 0.1:
            time.sleep(0.01)
        else:
            time.sleep(0.0001)


def do_click(pos):
    """Move + click tại pos."""
    fire_ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    pyautogui.moveTo(pos[0], pos[1], duration=0.1)
    pyautogui.click(pos[0], pos[1])
    print(f"[{fire_ts}] CLICK tại {pos}")
    return fire_ts


# ===================== COMMANDS =====================

def cmd_capture():
    """Setup lần đầu: launch Chrome ở vị trí cố định, capture template + save toạ độ tâm nút."""
    print("=== CAPTURE MODE ===")
    print("Chrome sẽ mở ở position/size cố định để nút luôn ở same pixel mỗi lần run.")
    launch_chrome()
    input("Nhấn Enter khi nút ĐIỂM DANH hiển thị rõ trên màn hình...")

    print("Trong 5s: di chuột lên GÓC TRÊN-TRÁI của nút.")
    for i in range(5, 0, -1):
        print(f"  {i}...")
        time.sleep(1)
    tl = pyautogui.position()
    print(f"  Top-left: {tl}")

    print("Trong 5s: di chuột xuống GÓC DƯỚI-PHẢI của nút.")
    for i in range(5, 0, -1):
        print(f"  {i}...")
        time.sleep(1)
    br = pyautogui.position()
    print(f"  Bottom-right: {br}")

    region = (int(tl.x), int(tl.y), int(br.x - tl.x), int(br.y - tl.y))
    img = pyautogui.screenshot(region=region)
    img.save(TEMPLATE_PATH)

    # Tâm nút
    center_x = int((tl.x + br.x) // 2)
    center_y = int((tl.y + br.y) // 2)

    cfg = load_config()
    cfg["button_x"] = center_x
    cfg["button_y"] = center_y
    cfg["button_region"] = region
    cfg["chrome_window_size"] = CHROME_WINDOW_SIZE
    save_config(cfg)

    print(f"✅ Template: {TEMPLATE_PATH} region={region}")
    print(f"✅ Cached tâm nút: ({center_x}, {center_y}) → {CONFIG_PATH}")

    # Verify
    pos = locate_button()
    if pos:
        print(f"✅ Verify screen match: {pos}")
    else:
        print("⚠️ Verify screen match FAIL (có thể vẫn OK khi run mode dùng cached coords)")


def cmd_test():
    """Test flow đầy đủ nhưng click NGAY (không chờ midnight)."""
    print("=== TEST MODE ===")
    launch_chrome()
    print("Chờ 15s để Chrome load + Turnstile pass...")
    time.sleep(15)

    pos = wait_for_button(timeout=30)
    if not pos:
        print("❌ Không tìm thấy nút. Chạy `capture` lại.")
        return

    print(f"CLICK sau 3s tại {pos}...")
    time.sleep(3)
    do_click(pos)
    time.sleep(5)


def cmd_run():
    """Production: task scheduler chạy lệnh này ~23:57."""
    print(f"=== RUN MODE ({datetime.now()}) ===")

    cfg = load_config()
    offset_ms = cfg.get("fire_offset_ms", FIRE_OFFSET_MS)

    # Safety: nếu đã quá midnight, abort để tránh chờ 24h
    now = datetime.now()
    if now.hour < 23 and now.hour >= 0:
        msg = f"🚨 Đã quá midnight ({now.strftime('%H:%M:%S')}) khi start, abort để tránh chờ 24h."
        print(msg)
        send_telegram(msg)
        return

    # 1. Launch Chrome ngay
    send_telegram(f"🟢 Bắt đầu auto-click ngày {now.strftime('%Y-%m-%d')} — launch Chrome...")
    launch_chrome()

    # 2. Chờ Chrome load + Turnstile pass (~30s)
    time.sleep(20)

    # 3. Locate button — thử screen match, fallback cached coords (dùng khi lock screen)
    pos = wait_for_button(timeout=15)
    if not pos:
        # Fallback: dùng cached coords từ capture
        cached_x = cfg.get("button_x")
        cached_y = cfg.get("button_y")
        if cached_x and cached_y:
            pos = (cached_x, cached_y)
            send_telegram(f"📍 Screen match fail (có thể lock screen). Dùng cached coords {pos}.")
        else:
            send_telegram("❌ Không tìm thấy nút + không có cached coords. Chạy `capture` lại.")
            return
    else:
        send_telegram(f"📍 Locked nút (screen match) tại {pos}. Chờ tới midnight{offset_ms:+d}ms...")

    # 4. FIRE approach: inject JS vào Chrome. JS sẽ:
    #    - Extract creds từ HTML
    #    - Fetch signing token + Turnstile token TRƯỚC midnight
    #    - Wait until 00:00:00 + offset_ms (bên trong browser)
    #    - Fire POST /check-in-secure trực tiếp
    #    → Fire chính xác 00:00:00.050 (client), server nhận ~+30-100ms → top 1-3
    # Test aggressive: fire pre-midnight. Turnstile token có TTL 5min nên OK.
    # Nếu server reject "đã điểm hôm qua" → biết được server không queue → revert offset.
    # Cứu streak: bạn click tay sáng hôm sau nếu fail.
    fire_offset_ms = -500  # sweet spot: top 5-8 ceiling từ home + Turnstile
    send_telegram(f"🚀 Inject fire JS. Sẽ fire tại midnight{fire_offset_ms:+d}ms.")
    ok, msg = fire_direct_post_via_cdp(fire_offset_ms=fire_offset_ms)
    if ok:
        send_telegram(f"🖱️ {msg[:500]}")
    else:
        send_telegram(f"⚠️ Fire fail: {msg[:400]}\nStreak có thể mất, click tay ngay để cứu!")

    # 6. Chờ 3s cho Chrome xử lý, verify status
    time.sleep(3)
    now_ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    try:
        creds = load_creds_from_file()
        status = get_checkin_status(creds)
        if status and status.get("todayCheckedIn"):
            pos_rank = status.get("todayCheckInPosition")
            streak = status.get("currentStreak")
            send_telegram(f"✅ Click OK lúc {now_ts} | streak={streak}" + (f" | 🏆 top {pos_rank}" if pos_rank else ""))
        elif status:
            send_telegram(f"⚠️ Click lúc {now_ts} nhưng status chưa update. Sẽ check lại 30s...")
            time.sleep(30)
            status = get_checkin_status(creds)
            if status and status.get("todayCheckedIn"):
                send_telegram(f"✅ Retry check OK | streak={status.get('currentStreak')} | top {status.get('todayCheckInPosition')}")
            else:
                send_telegram(f"❌ Sau retry vẫn chưa checkedIn. Có thể click miss hoặc Turnstile chặn.")
        else:
            send_telegram(f"⚠️ Click lúc {now_ts} nhưng không query được status (cookies?).")
    except Exception as e:
        send_telegram(f"⚠️ Verify fail: {type(e).__name__}: {str(e)[:200]}")


def cmd_test_cdp():
    """Test CDP click flow: launch Chrome + wait + CDP click."""
    print("=== TEST CDP MODE ===")
    launch_chrome()
    print("Chờ 15s để Chrome load + CDP port ready...")
    time.sleep(15)

    print("Test CDP click (single click, button có thể disabled)...")
    ok, msg = click_via_cdp()
    print(f"Single click result: ok={ok}, msg={msg}")

    if not ok:
        print("\nTest CDP reload+poll+click (simulate midnight rollover)...")
        ok2, msg2 = reload_and_click_via_cdp(max_attempts=15, poll_interval_ms=100)
        print(f"Reload+click result: ok={ok2}, msg={msg2}")

    time.sleep(5)


def main():
    if len(sys.argv) < 2:
        print("Usage: python auto_click.py [capture|test|test-cdp|run]")
        return
    cmd = sys.argv[1]
    if cmd == "capture":
        cmd_capture()
    elif cmd == "test":
        cmd_test()
    elif cmd == "test-cdp":
        cmd_test_cdp()
    elif cmd == "run":
        cmd_run()
    else:
        print(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
