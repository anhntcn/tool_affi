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
import random
import subprocess
import sys
import time
from datetime import datetime, time as dt_time, timedelta, timezone

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


def fire_direct_post_via_cdp(fire_offset_ms=-40, burst_count=8, burst_gap_ms=35):
    """PRE-FETCH tokens TRƯỚC midnight, calibrate giờ SERVER, warm connection, rồi BURST POST.

    Cải tiến so với bản 1-phát:
      1. Server-time calibration: probe header `Date` liên tục, bắt đúng khoảnh khắc giây
         server nhảy (edge-detection) → ghim mốc giây server ±1 RTT. Header Date chỉ có độ
         phân giải giây nên edge-detection là cách duy nhất lấy sub-second offset.
      2. Warm connection: gửi HEAD ~1.5s trước fire để giữ TLS/HTTP2 alive, POST thật khỏi handshake.
      3. Burst: bắn `burst_count` POST cách nhau `burst_gap_ms`, mỗi phát ts/nonce/signature RIÊNG,
         canh sao cho các phát ĐẾN server trải quanh 00:00:00 server + fire_offset_ms.

    fire_offset_ms = thời điểm ĐẾN server (ms) của phát đầu, so với midnight server.
      Ví dụ -40 → phát đầu đến trước midnight 40ms (thường bị reject "hôm qua"), các phát sau
      lần lượt +35ms rơi ngay sau midnight → phát hợp lệ đầu tiên thắng.
    Trả về (success, message)."""
    try:
        ws = _cdp_get_ws()

        js = r"""
        (async function(){
            const LOG = [];
            function log(m){ LOG.push(m); }
            try {
                const FIRE_OFFSET_MS = __FIRE_OFFSET__;   // arrival của phát đầu, so với server midnight
                const BURST_N        = __BURST_N__;
                const BURST_GAP_MS   = __BURST_GAP__;

                // ---- creds ----
                const html = document.documentElement.outerHTML;
                const csrfMatch = html.match(/csrfToken:\s*'([^']+)'/);
                const userIdMatch = html.match(/googleId:\s*'([^']+)'/);
                if (!csrfMatch || !userIdMatch) return {ok:false, err:'creds regex fail', log:LOG};
                const csrfToken = csrfMatch[1];
                const userId = userIdMatch[1];

                // ---- signing token ----
                const signResp = await fetch('/api/v2/security/signature-token', {credentials:'same-origin', cache:'no-store'}).then(r=>r.json());
                if (!signResp.success || !signResp.data || !signResp.data.signatureToken) return {ok:false, err:'signing token fail: '+JSON.stringify(signResp).slice(0,150), log:LOG};
                const signingToken = signResp.data.signatureToken;

                // ---- Turnstile token (retry: getToken hay timeout "Xác minh bảo mật quá thời gian") ----
                if (!window.CheckInTurnstile || typeof window.CheckInTurnstile.getToken !== 'function') return {ok:false, err:'CheckInTurnstile not loaded', log:LOG};
                function withTimeout(p, ms){ return Promise.race([p, new Promise((_,rej)=>setTimeout(()=>rej(new Error('getToken timeout '+ms+'ms')), ms))]); }
                let tsToken = null;
                for (let attempt=0; attempt<4 && !tsToken; attempt++){
                    try {
                        if (attempt>0 && window.turnstile && typeof window.turnstile.reset==='function') { try{ window.turnstile.reset(); }catch(e){} }
                        tsToken = await withTimeout(Promise.resolve(window.CheckInTurnstile.getToken()), 12000);
                    } catch(e){ log('turnstile attempt '+attempt+' err: '+String(e).slice(0,90)); }
                    if (!tsToken) await new Promise(r=>setTimeout(r, 700));
                }
                if (!tsToken) return {ok:false, err:'Turnstile getToken fail sau 4 lần', log:LOG};

                // ---- import HMAC key 1 lần, dùng lại cho mọi phát ----
                const encoder = new TextEncoder();
                const hmacKey = await crypto.subtle.importKey('raw', encoder.encode(signingToken), {name:'HMAC',hash:'SHA-256'}, false, ['sign']);
                async function makeSig(ts, nonce){
                    const sig = await crypto.subtle.sign('HMAC', hmacKey, encoder.encode(ts + '.' + nonce + '.' + userId));
                    return Array.from(new Uint8Array(sig)).map(b=>b.toString(16).padStart(2,'0')).join('');
                }

                // ---- nominal midnight (calendar-based, không lệ thuộc clock skew) ----
                const N = (function(){ const d = new Date(); d.setHours(24,0,0,0); return d.getTime(); })();

                // ---- coarse wait tới ~T-9s trước khi calibrate (calibrate gần fire cho chuẩn RTT) ----
                while (Date.now() < N - 9000) {
                    const rem = N - 9000 - Date.now();
                    await new Promise(r=>setTimeout(r, Math.min(rem, 5000)));
                }

                // ---- CALIBRATION: edge-detection giây server qua header Date ----
                async function probe(){
                    const t0 = Date.now();
                    let dh = null;
                    try {
                        const r = await fetch('/api/v2/security/signature-token', {method:'HEAD', cache:'no-store', credentials:'same-origin'});
                        dh = r.headers.get('date');
                    } catch(e){}
                    const t1 = Date.now();
                    const serverMs = dh ? new Date(dh).getTime() : null; // giây, .000
                    return {serverMs, mid:(t0+t1)/2, rtt:t1-t0};
                }
                function median(arr){ const s=arr.slice().sort((a,b)=>a-b); const m=Math.floor(s.length/2); return s.length%2 ? s[m] : (s[m-1]+s[m])/2; }
                let prevSec = null, prevMid = null, minRtt = 99999, samples = 0;
                const edgeOffsets = [];
                const calDeadline = N - 3500;  // ~5.5s probing
                while (Date.now() < calDeadline) {
                    const p = await probe();
                    samples++;
                    if (p.serverMs === null) continue;
                    if (p.rtt < minRtt) minRtt = p.rtt;
                    if (prevSec !== null && p.serverMs > prevSec) {
                        // giây server nhảy giữa probe trước (prevMid) và probe này (p.mid).
                        // Mốc .000 của giây mới nằm TRONG khoảng (prevMid, p.mid) → ước lượng tốt nhất = trung điểm.
                        // Sai số = ±(p.mid-prevMid)/2 = ±nửa probe gap (~vài chục ms) thay vì cả 1 giây.
                        const boundaryLocal = (prevMid + p.mid) / 2;
                        edgeOffsets.push(p.serverMs - boundaryLocal);
                    }
                    prevSec = p.serverMs;
                    prevMid = p.mid;
                }
                let offset;
                if (edgeOffsets.length) {
                    offset = median(edgeOffsets);  // median chống outlier từ probe bị mạng giật
                } else {
                    // không bắt được edge → fallback coarse: dùng probe cuối (±1s). RỦI RO nếu clock lệch lớn.
                    const p = await probe();
                    if (p.serverMs !== null) { offset = p.serverMs - p.mid; log('WARN: no edge, coarse offset ±1s'); }
                    else return {ok:false, err:'calibration fail: no Date header', log:LOG};
                }
                const edges = edgeOffsets.length;
                const oneWay = minRtt / 2;
                const spread = edges >= 2 ? Math.round(Math.max.apply(null,edgeOffsets)-Math.min.apply(null,edgeOffsets)) : 0;
                log('cal: offset='+Math.round(offset)+'ms minRtt='+minRtt+'ms edges='+edges+' spread='+spread+'ms samples='+samples);

                // ---- lịch fire: send_local_k = (N - offset) - oneWay + FIRE_OFFSET_MS + k*GAP ----
                const sendBase = (N - offset) - oneWay + FIRE_OFFSET_MS;
                const sends = [];
                for (let k=0;k<BURST_N;k++) sends.push(sendBase + k*BURST_GAP_MS);

                // ---- warm connection ~1.5s trước phát đầu ----
                const warmAt = sends[0] - 1500;
                while (Date.now() < warmAt) await new Promise(r=>setTimeout(r, Math.min(warmAt-Date.now(), 300)));
                try { await fetch('/api/v2/security/signature-token', {method:'HEAD', cache:'no-store', credentials:'same-origin'}); log('warm sent'); } catch(e){}

                // ---- BURST ----
                async function fireOne(k){
                    const sendTgt = sends[k];
                    while (Date.now() < sendTgt) {
                        const rem = sendTgt - Date.now();
                        if (rem > 3) await new Promise(r=>setTimeout(r, 1));
                        // else spin
                    }
                    const ts = Date.now().toString();
                    const nonce = Math.random().toString(36).substring(2, 15) + k;
                    const sigHex = await makeSig(ts, nonce);
                    const t0 = Date.now();
                    let res;
                    try {
                        res = await fetch('/api/v2/xeng/check-in-secure', {
                            method: 'POST', credentials: 'same-origin',
                            headers: {'Content-Type':'application/json','x-csrf-token':csrfToken,'x-signature':sigHex,'x-timestamp':ts,'x-nonce':nonce},
                            body: JSON.stringify({'cf-turnstile-response': tsToken})
                        }).then(r=>r.json());
                    } catch(e){ res = {success:false, error:String(e)}; }
                    const t1 = Date.now();
                    let raw;
                    try { raw = JSON.stringify(res).slice(0,180); } catch(e){ raw = String(res).slice(0,180); }
                    return {
                        k, sentSrv: Math.round(t0 + offset), lat: t1-t0,
                        ok: !!res.success,
                        pos: res && res.data ? (res.data.position || res.data.todayCheckInPosition) : undefined,
                        err: res && res.success ? undefined : String((res&&(res.error||res.message))||'').slice(0,80),
                        raw: raw
                    };
                }
                // schedule tất cả, chờ xong hết
                const results = await Promise.all(Array.from({length:BURST_N}, (_,k)=>fireOne(k)));
                const winner = results.find(r=>r.ok);

                return {
                    ok: !!winner,
                    offsetMs: Math.round(offset), minRttMs: minRtt, edges, spreadMs: spread,
                    fireStart: new Date(Math.round(sendBase)).toISOString(),
                    winner: winner || null,
                    results,
                    log: LOG
                };
            } catch (e) {
                return {ok:false, err:'exception: '+String(e).slice(0,200), log:LOG};
            }
        })()
        """.replace("__FIRE_OFFSET__", str(int(fire_offset_ms))) \
           .replace("__BURST_N__", str(int(burst_count))) \
           .replace("__BURST_GAP__", str(int(burst_gap_ms)))

        # JS block cho tới sau midnight. Từ 23:57 tới fire = ~183s + burst.
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": js, "awaitPromise": True, "returnByValue": True, "timeout": 300000},
        }))
        ws.settimeout(360)  # 6 phút, buffer đủ
        result = json.loads(ws.recv())
        ws.close()

        value = result.get("result", {}).get("result", {}).get("value", {})
        # Tóm tắt burst gọn cho Telegram
        results = value.get("results") or []
        brief = " ".join(
            f"#{r.get('k')}:{'OK' if r.get('ok') else 'x'}"
            + (f"p{r.get('pos')}" if r.get('pos') is not None else "")
            for r in results
        )
        # Nhóm response theo raw text → thấy rõ endpoint nói gì HAI PHÍA ranh giới (data cho bước 2)
        groups = {}
        for r in results:
            raw = r.get("raw") or r.get("err") or "?"
            groups.setdefault(raw, []).append(r.get("k"))
        resp_block = " || ".join(
            f"#{','.join(str(k) for k in ks)}: {raw}" for raw, ks in groups.items()
        )
        cal = f"offset={value.get('offsetMs')}ms rtt={value.get('minRttMs')}ms edges={value.get('edges')} spread={value.get('spreadMs')}ms"
        if value.get("ok"):
            w = value.get("winner") or {}
            return True, f"FIRE OK | {cal} | winner #{w.get('k')} pos={w.get('pos')} lat={w.get('lat')}ms | burst[{brief}]\n📋 resp: {resp_block[:600]}"
        else:
            err = value.get("err")
            if err:
                return False, f"FIRE FAIL | {err} | log={value.get('log')}"
            return False, f"FIRE FAIL | {cal} | burst[{brief}]\n📋 resp: {resp_block[:600]}\nlog={value.get('log')}"
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


def cdp_check_logged_in(timeout=25):
    """Poll CDP tabs sau khi launch. Trả về (ok, detail).
    ok=True nếu tìm thấy tab caffiliate ĐANG ở /rewards (đã login).
    ok=False + detail='login' nếu bị redirect về /login (session hết hạn).
    ok=False + detail='no-tab' nếu chưa thấy tab nào (Chrome chưa load / CDP chưa sẵn sàng)."""
    import urllib.request
    deadline = time.time() + timeout
    last = "no-tab"
    while time.time() < deadline:
        try:
            tabs = json.loads(urllib.request.urlopen(
                f"http://localhost:{CDP_PORT}/json", timeout=5).read())
            pages = [t.get("url", "").lower() for t in tabs if t.get("type") == "page"]
            if any("caffiliate" in u and "rewards" in u for u in pages):
                return True, "rewards"
            if any("caffiliate" in u and "login" in u for u in pages):
                last = "login"
        except Exception:
            pass
        time.sleep(1)
    return False, last


def kill_chrome():
    """Kill CHỈ Chrome của profile caffiliate (SeleniumChromeProfile) — KHÔNG đụng Chrome tool khác
    (saffi/hoantien khác profile) hay Chrome cá nhân. Match theo command line qua WMIC.
    Trước đây taskkill /IM chrome.exe giết sạch mọi Chrome → mìn với multi-tool; đã bỏ."""
    marker = os.path.basename(PROFILE_DIR)  # "SeleniumChromeProfile"
    ps = (
        "Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
        f"Where-Object {{ $_.CommandLine -match '{marker}' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True)


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


def _http_server_datetime():
    """Lấy giờ SERVER caffiliate (UTC, tz-aware) từ header Date qua HTTPS. None nếu fail.
    Dùng HTTPS thay vì NTP vì máy này NTP client hỏng + mạng rớt UDP 123; header Date luôn lấy được."""
    import urllib.request
    from email.utils import parsedate_to_datetime
    try:
        req = urllib.request.Request(REWARDS_URL, method="HEAD")
        with urllib.request.urlopen(req, timeout=8) as r:
            date_hdr = r.headers.get("Date")
        if not date_hdr:
            return None
        return parsedate_to_datetime(date_hdr)  # tz-aware (GMT)
    except Exception:
        return None


def _http_date_offset(url):
    """Fetch HEAD, trả (offset_ms, None) với offset = server_Date_ms - local_mid_ms; hoặc (None, err).
    offset đo tương đối so với CÙNG clock local nên hiệu giữa các nguồn triệt tiêu clock máy."""
    import urllib.request
    from email.utils import parsedate_to_datetime
    try:
        req = urllib.request.Request(url, method="HEAD", headers={
            "Cache-Control": "no-cache",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=8) as r:
            date_hdr = r.headers.get("Date")
        t1 = time.time()
        if not date_hdr:
            return None, "no Date header"
        server_ms = parsedate_to_datetime(date_hdr).timestamp() * 1000.0
        mid_ms = (t0 + t1) / 2 * 1000.0
        return server_ms - mid_ms, None
    except Exception as e:
        return None, f"{type(e).__name__}"


def detect_clock_skew():
    """So header Date của caffiliate với các nguồn HTTPS ĐỘC LẬP (Google, Cloudflare).
    Nếu caffiliate lệch khỏi nhóm kia > ~2s → caffi có thể đang skew giờ để bẫy tool. Trả (skewed, detail).
    Vì mỗi offset đo so với cùng clock máy, hiệu số triệt tiêu sai số clock máy → phát hiện được dù máy lệch."""
    caffi, e_caffi = _http_date_offset(REWARDS_URL)
    ref_urls = ["https://www.google.com/", "https://www.cloudflare.com/",
                "https://www.bing.com/", "https://www.microsoft.com/"]
    refs = [x for x in (_http_date_offset(u)[0] for u in ref_urls) if x is not None]
    if caffi is None:
        return False, f"caffi fail ({e_caffi})"
    if not refs:
        return False, "không có nguồn tham chiếu (Google/CF fail)"
    ref = sorted(refs)[len(refs) // 2]  # median các nguồn tham chiếu
    div = caffi - ref  # ms
    # Header Date chỉ tới giây → nhiễu ±1s bình thường; cờ khi > 2s
    skewed = abs(div) > 2000
    return skewed, f"caffi vs refs divergence={div/1000:.1f}s (caffi_off={caffi/1000:.1f}s ref_off={ref/1000:.1f}s, n_ref={len(refs)})"


def sync_windows_clock():
    """Sync đồng hồ Windows theo giờ SERVER caffiliate qua header Date (HTTPS, KHÔNG dùng NTP/UDP).
    Trả về (ok, detail). Cần Task Scheduler chạy quyền admin (SetSystemTime yêu cầu SE_SYSTEMTIME).

    Bối cảnh: máy này lệch ~10.4s (đo 2026-08-11) và w32tm/NTP không sync được (mạng rớt UDP 123).
    Header Date chỉ chính xác tới GIÂY → OS còn lệch ≤1s, nhưng đủ vai trò lưới an toàn; calibration
    lúc fire vẫn tự bù phần dưới giây. Mục tiêu chỉ là tránh lệch cả chục giây khi calibration miss edge."""
    import ctypes
    import ctypes.wintypes as wt
    try:
        dt = _http_server_datetime()
        if dt is None:
            return False, "không lấy được header Date (mạng?)"
        u = dt.astimezone(timezone.utc)

        class SYSTEMTIME(ctypes.Structure):
            _fields_ = [("wYear", wt.WORD), ("wMonth", wt.WORD), ("wDayOfWeek", wt.WORD),
                        ("wDay", wt.WORD), ("wHour", wt.WORD), ("wMinute", wt.WORD),
                        ("wSecond", wt.WORD), ("wMilliseconds", wt.WORD)]

        st = SYSTEMTIME(u.year, u.month, 0, u.day, u.hour, u.minute, u.second, 0)
        before = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        ok = ctypes.windll.kernel32.SetSystemTime(ctypes.byref(st))  # SetSystemTime nhận UTC
        if ok:
            after = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            return True, f"UTC={u.strftime('%H:%M:%S')} | local {before}→{after}"
        err = ctypes.windll.kernel32.GetLastError()
        return False, f"SetSystemTime fail err={err} (cần quyền admin)"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:150]}"


def fallback_click_after_midnight(deadline_s=90):
    """Khi fire (direct POST) fail: CHỜ qua midnight rồi reload+click nút THẬT lặp lại tới khi được.
    Chậm/rank thấp nhưng CỨU STREAK. Turnstile để trang tự xử lý qua widget (không cần getToken tay).
    Trả (ok, detail)."""
    now = datetime.now()
    if now.hour == 23:
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=1, microsecond=0)
        print(f"[fallback] chờ tới {nxt} rồi click...")
        precise_wait_until(nxt)
    deadline = time.time() + deadline_s
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        ok, msg = reload_and_click_via_cdp(max_attempts=20, poll_interval_ms=150)
        if ok:
            return True, f"click OK sau {attempt} vòng reload | {msg[:150]}"
        time.sleep(1)
    return False, f"hết {deadline_s}s sau {attempt} vòng, nút vẫn không click được"


RANK_DATA_PATH = os.path.join(HERE, "rank_data.md")

_RANK_MD_HEADER = (
    "# Rank data — caffiliate điểm danh\n\n"
    "`arrival_ms` = mốc server đóng dấu (`history.createdAt`), số ms sau 00:00 ICT — KHÔNG lệ thuộc clock máy.\n"
    "`position` = rank hôm đó. `mode` = aggressive/coast/historical.\n\n"
    "| checkInDate | arrival_ms | position | mode | offset_used_ms | coast_delay_ms | streak |\n"
    "|---|---|---|---|---|---|---|\n"
)


def _arrival_ms_from_history(status, target_date):
    """Từ history của status, lấy createdAt (server đóng dấu, ms) của ngày target_date →
    số ms sau 00:00 ICT. Server lưu createdAt dạng UTC; 00:00 ICT = ngày-1 17:00:00Z.
    Trả int ms hoặc None. Đây là 'giờ đến' CHUẨN SERVER, không lệ thuộc clock máy."""
    from email.utils import parsedate_to_datetime  # noqa
    try:
        hist = status.get("history") or []
        tgt = str(target_date)
        entry = next((h for h in hist if h.get("checkInDate") == tgt), None)
        if not entry or not entry.get("createdAt"):
            return None
        ca = entry["createdAt"].replace("Z", "+00:00")
        created = datetime.fromisoformat(ca)  # tz-aware UTC
        # 00:00 ICT của checkInDate = (checkInDate) 00:00 +07:00
        y, m, d = [int(x) for x in tgt.split("-")]
        ict_midnight = datetime(y, m, d, tzinfo=timezone(timedelta(hours=7)))
        return int((created - ict_midnight).total_seconds() * 1000)
    except Exception as e:
        print(f"[arrival_ms err] {e}")
        return None


def log_rank_datapoint(status, target_date, mode, offset_used, coast_delay):
    """Ghi 1 hàng bảng Markdown (checkInDate, arrival_ms, position, mode, offset, coast_delay, streak)
    vào rank_data.md. Tích luỹ mapping giờ-đến ↔ rank qua nhiều đêm để sau này nhường có cơ sở."""
    try:
        arrival_ms = _arrival_ms_from_history(status, target_date)
        pos = status.get("todayCheckInPosition")
        streak = status.get("currentStreak")
        new = not os.path.isfile(RANK_DATA_PATH)
        with open(RANK_DATA_PATH, "a", encoding="utf-8") as f:
            if new:
                f.write(_RANK_MD_HEADER)
            f.write(f"| {target_date} | {arrival_ms} | {pos} | {mode} | {offset_used} | {coast_delay} | {streak} |\n")
        return arrival_ms, pos
    except Exception as e:
        print(f"[log_rank err] {e}")
        return None, None


def stealth_plan(cfg):
    """Chiến lược: GIỮ #1 (giữ thưởng) + nguỵ trang timing; định kỳ RÒ BIÊN #2-3 bằng bước nhỏ.
    Thưởng: top1 > top2=top3 > (top4+=0). Không có băng #2-3 êm (lao vào đám đông ~sau 490ms →
    khuếch đại → #7 mất trắng), nên nhường phải dò CỰC cẩn thận.

    Mỗi đêm thường: #1 an toàn, offset jitter lệch âm (giờ-đến trải, đỡ vân tay), trần +20ms.
    Mỗi `stealth_probe_every_nights` đêm: 1 đêm PROBE — nhích offset lên từng bước nhỏ để tìm biên
    #1→#2-3, đọc rank thật rồi tự điều chỉnh (xem stealth_update). Ngừng probe khi 'found' đã ổn
    hoặc 'cliff' (không có băng #2-3).
    Trả (fire_offset_ms, burst_count, burst_gap_ms, mode, note)."""
    if not cfg.get("stealth_enabled", True):
        return cfg.get("fire_offset_ms", -40), cfg.get("burst_count", 8), cfg.get("burst_gap_ms", 35), "off", "stealth OFF"

    jitter = cfg.get("stealth_jitter_ms", 120)
    consec = cfg.get("stealth_consec_top1", 0)

    # --- Đêm PROBE? Lịch NGẪU NHIÊN (gap 3/4/5 đêm, không cố định chu kỳ → giống người) ---
    probe_on = cfg.get("stealth_probe_enabled", False) and cfg.get("stealth_probe_status", "probing") != "cliff"
    countdown = cfg.get("stealth_probe_countdown", 3)
    if probe_on and countdown <= 1:
        off = int(cfg.get("stealth_probe_offset_ms", 0))
        off = max(-400, min(off, 500))   # CAP CỨNG: không bao giờ nhảy sâu vào đám đông (đêm #7 là +520)
        bc = cfg.get("stealth_burst_coast", 3)
        gap = random.randint(130, 230)
        st = cfg.get("stealth_probe_status", "probing")
        return off, int(bc), int(gap), "probe", f"probe off={off}ms step={cfg.get('stealth_probe_step_ms', 30)} status={st}"

    # --- Đêm thường: #1 an toàn + jitter lệch âm ---
    base = -40
    bc = cfg.get("stealth_burst_top1", 4)
    gap = random.randint(40, 90)             # giãn kiểu người retry, đỡ lộ burst
    off = base + random.randint(-jitter, jitter // 4)
    off = max(-400, min(off, 20))            # trần +20ms → không chạm crowd
    return int(off), int(bc), int(gap), "aggressive", f"aggressive off={int(off)}ms burst {bc}×~{gap}ms consec1={consec}"


def stealth_update(cfg, rank, mode, offset_used):
    """Sau khi biết rank thật: đếm đêm (cho lịch probe) + học biên probe (staircase an toàn)."""
    if mode == "off":
        return
    if rank is None:
        save_config(cfg)   # đêm lỗi: không đụng lịch/biên, giữ nguyên để đêm sau thử lại
        return
    cfg["stealth_consec_top1"] = (cfg.get("stealth_consec_top1", 0) + 1) if rank == 1 else 0

    if mode == "probe":
        step = cfg.get("stealth_probe_step_ms", 30)
        safe = cfg.get("stealth_probe_safe_offset_ms", -40)
        if rank == 1:
            # vẫn trước đám đông → an toàn, ghi mốc, nhích thêm bước nhỏ
            cfg["stealth_probe_safe_offset_ms"] = int(offset_used)
            cfg["stealth_probe_offset_ms"] = int(offset_used) + step
            cfg["stealth_probe_status"] = "probing"
        elif rank in (2, 3):
            # TÌM THẤY biên #2-3 → khoá offset này để tái dùng cho đêm nhường
            cfg["stealth_probe_status"] = "found"
            cfg["stealth_probe_offset_ms"] = int(offset_used)
            cfg["stealth_coast_delay_ms"] = int(offset_used)
        else:  # rank >= 4: nhảy quá biên (khuếch đại) → LÙI về dưới mốc an toàn + giảm nửa bước
            newstep = max(10, step // 2)
            cfg["stealth_probe_step_ms"] = newstep
            cfg["stealth_probe_offset_ms"] = int(safe) + newstep
            if step <= 10:
                # bước đã nhỏ mà vẫn 1→≥4 (bỏ qua #2-3) → VÁCH ĐÁ, không có băng #2-3 → ngừng nhường
                cfg["stealth_probe_status"] = "cliff"
        # đặt lịch đêm nhường KẾ TIẾP ngẫu nhiên (gap 3/4/5), không cố định
        cfg["stealth_probe_countdown"] = random.choice(cfg.get("stealth_probe_gap_choices", [2, 3, 4]))
    else:
        # đêm thường #1 → đếm ngược tới đêm nhường kế
        cfg["stealth_probe_countdown"] = max(1, cfg.get("stealth_probe_countdown", 3) - 1)
    save_config(cfg)


def cmd_run():
    """Production: task scheduler chạy lệnh này ~23:57."""
    print(f"=== RUN MODE ({datetime.now()}) ===")

    # 0. Sync clock TRƯỚC (cần chạy Task Scheduler với quyền admin để SetSystemTime ăn).
    sync_ok, sync_detail = sync_windows_clock()
    print(f"[clock sync] ok={sync_ok} | {sync_detail}")

    # 0b. Dò skew: caffi có đang chỉnh giờ nhanh/chậm để bẫy tool không?
    skewed, skew_detail = detect_clock_skew()
    print(f"[skew] skewed={skewed} | {skew_detail}")

    cfg = load_config()
    offset_ms = cfg.get("fire_offset_ms", FIRE_OFFSET_MS)

    # Safety: nếu đã quá midnight, abort để tránh chờ 24h
    now = datetime.now()
    if now.hour < 23 and now.hour >= 0:
        msg = f"🚨 Đã quá midnight ({now.strftime('%H:%M:%S')}) khi start, abort để tránh chờ 24h."
        print(msg)
        send_telegram(msg)
        return

    # NGÀY MỚI cần điểm danh = hôm nay + 1. Dùng để date-guard verify (chống báo nhầm status ngày cũ).
    target_date = (now + timedelta(days=1)).date()

    # 1. Launch Chrome ngay
    clock_note = f"🕐 clock synced ({sync_detail[:60]})" if sync_ok else f"⚠️ clock sync FAIL ({sync_detail[:80]}) — Task cần quyền admin?"
    skew_note = f"🚨 CAFFI SKEW GIỜ: {skew_detail}" if skewed else f"🧭 skew check OK: {skew_detail}"
    send_telegram(f"🟢 Bắt đầu auto-click ngày {now.strftime('%Y-%m-%d')} — launch Chrome...\n{clock_note}\n{skew_note}")
    launch_chrome()

    # 2. Chờ Chrome load + Turnstile pass (~30s)
    time.sleep(20)

    # 2b. Check session: nếu bị redirect về /login → báo NGAY để đăng nhập tay
    #     kịp trước midnight (còn ~3 phút), thay vì fail lúc fire.
    ok_login, detail = cdp_check_logged_in(timeout=25)
    if not ok_login:
        if detail == "login":
            send_telegram(
                "🔴 SESSION HẾT HẠN! Tab đang ở /login. "
                "Đăng nhập TAY vào Chrome NGAY (còn vài phút trước 00:00) để cứu streak. "
                "Profile: C:\\SeleniumChromeProfile"
            )
        else:
            send_telegram(
                f"🟠 Chưa thấy tab caffiliate/rewards qua CDP (detail={detail}). "
                "Kiểm tra Chrome đã mở đúng chưa."
            )
        # Không return — vẫn thử fire, vì user có thể đăng nhập tay kịp lúc.

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
    #    - Extract creds + fetch signing/Turnstile token TRƯỚC midnight
    #    - CALIBRATE giờ server (edge-detection header Date) → offset + RTT
    #    - Warm connection ~1.5s trước fire
    #    - BURST nhiều POST canh ĐẾN server quanh 00:00:00 server + fire_offset_ms
    #    → không lệ thuộc clock máy; chịu được jitter; log offset/RTT/pos để tinh chỉnh.
    # fire_offset_ms ở đây = arrival của phát ĐẦU so với midnight server (âm = trước, để phát sau bắt boundary).
    fire_offset_ms, burst_count, burst_gap_ms, stealth_mode, stealth_note = stealth_plan(cfg)
    mode_icon = "🎯" if stealth_mode == "aggressive" else ("🕶️" if stealth_mode == "coast" else "🚀")
    send_telegram(f"{mode_icon} Fire [{stealth_note}]. arrival phát đầu = midnight{fire_offset_ms:+d}ms server.")
    ok, msg = fire_direct_post_via_cdp(fire_offset_ms=fire_offset_ms, burst_count=burst_count, burst_gap_ms=burst_gap_ms)
    if ok:
        send_telegram(f"🖱️ {msg[:500]}")
    else:
        # Fire (direct POST) fail → FALLBACK: chờ qua midnight rồi click nút thật để cứu streak.
        send_telegram(f"⚠️ Fire fail: {msg[:350]}\n↻ Thử FALLBACK click nút sau midnight...")
        fb_ok, fb_msg = fallback_click_after_midnight(deadline_s=90)
        if fb_ok:
            send_telegram(f"🖱️ FALLBACK OK: {fb_msg}")
        else:
            send_telegram(f"❌ FALLBACK fail: {fb_msg}\n‼️ CLICK TAY NGAY để cứu streak!")

    # 6. Verify — CHỈ tin status khi ĐÃ qua ngày mới (target_date), tránh đọc nhầm status NGÀY CŨ.
    time.sleep(3)
    now2 = datetime.now()
    now_ts = now2.strftime("%H:%M:%S.%f")[:-3]
    if now2.date() < target_date:
        # Chưa qua midnight (fire fail sớm) → status hiện tại là ngày cũ, KHÔNG dùng để kết luận.
        send_telegram(
            f"⚠️ Verify SKIP lúc {now_ts}: chưa qua midnight (ngày {target_date} chưa tới). "
            f"Status lúc này là NGÀY CŨ, không tin được. "
            + ("Fire báo OK, chờ verify lại sau." if ok else "‼️ CLICK TAY để cứu streak!")
        )
        return
    actual_rank = None
    status = None
    try:
        creds = load_creds_from_file()
        status = get_checkin_status(creds)
        if status and status.get("todayCheckedIn"):
            actual_rank = status.get("todayCheckInPosition")
            streak = status.get("currentStreak")
            send_telegram(f"✅ ĐÃ điểm danh NGÀY MỚI ({target_date}) lúc {now_ts} | streak={streak}" + (f" | 🏆 top {actual_rank}" if actual_rank else ""))
        elif status:
            send_telegram(f"⚠️ Chưa thấy checkedIn cho {target_date}. Check lại sau 30s...")
            time.sleep(30)
            status = get_checkin_status(creds)
            if status and status.get("todayCheckedIn"):
                actual_rank = status.get("todayCheckInPosition")
                send_telegram(f"✅ Retry OK ({target_date}) | streak={status.get('currentStreak')} | top {actual_rank}")
            else:
                send_telegram(f"❌ Sau retry vẫn CHƯA điểm danh {target_date}. ‼️ CLICK TAY NGAY để cứu streak!")
        else:
            send_telegram(f"⚠️ Không query được status (cookies?). Kiểm tra tay cho chắc.")
    except Exception as e:
        send_telegram(f"⚠️ Verify fail: {type(e).__name__}: {str(e)[:200]}")

    # Ghi datapoint mapping (giờ-đến chuẩn server ↔ rank) + học stealth cho đêm sau.
    try:
        prev_coast = cfg.get("stealth_coast_delay_ms", 0)
        arrival_ms = None
        if status:
            arrival_ms, _ = log_rank_datapoint(status, target_date, stealth_mode, fire_offset_ms, prev_coast)
        stealth_update(cfg, actual_rank, stealth_mode, fire_offset_ms)
        if actual_rank is not None:
            warn = " ⚠️RỚT TOP3(0 thưởng)!" if actual_rank >= 4 else ""
            extra = ""
            if stealth_mode == "probe":
                extra = (f" | 🔎 probe status={cfg.get('stealth_probe_status')} "
                         f"next_off={cfg.get('stealth_probe_offset_ms')}ms step={cfg.get('stealth_probe_step_ms')} "
                         f"safe_off={cfg.get('stealth_probe_safe_offset_ms')}ms | đêm nhường kế sau {cfg.get('stealth_probe_countdown')} đêm")
            send_telegram(
                f"🎚️ stealth: mode={stealth_mode} rank={actual_rank}{warn} | "
                f"arrival={arrival_ms}ms (server) | offset_dùng={fire_offset_ms}ms | "
                f"consec1={cfg.get('stealth_consec_top1')}{extra} | 📊 rank_data.md"
            )
    except Exception as e:
        print(f"[stealth_update/log err] {e}")


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
