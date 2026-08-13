# Caffiliate — điểm danh tự động app.caffiliate.vn

Điểm danh site **app.caffiliate.vn** (reset 00:00 ICT, giữ streak). Đây là site khó nhất trong
bộ tool vì có **2 lớp chống bot**:

1. **Chữ ký HMAC động** — mỗi request `POST /api/v2/xeng/check-in-secure` phải kèm
   `x-signature = HMAC_SHA256(signing_token, "{timestamp}.{nonce}.{userId}")`. `signing_token`
   lấy từ `GET /api/v2/security/signature-token` (TTL server-side, phải fetch gần lúc bắn).
2. **Cloudflare Turnstile** — body cần `cf-turnstile-response`. Token này **chỉ giải được trong
   Chrome thật** (Playwright/Selenium bị Turnstile phát hiện và không giải).

Vì vậy có **nhiều cách chạy**, chọn theo hoàn cảnh (xem [Chọn cách chạy](#chọn-cách-chạy)).

## Chọn cách chạy

| Cách chạy | File | Vượt Turnstile? | Chạy ở đâu | Khi nào dùng |
|---|---|---|---|---|
| **Auto-click CDP** (khuyến nghị) | [auto_click.py](auto_click.py) | ✅ Chrome thật | Máy Windows có Chrome | Cách bền nhất hiện tại |
| API burst (Selenium) | [auto_tool.py](auto_tool.py) | ❌ | Máy có Chrome + Selenium | Khi site chưa bật Turnstile |
| API server (no-Selenium) | [auto_tool_server.py](auto_tool_server.py) | ❌ | Oracle Cloud / VPS Linux | Chạy 24/7 khỏi bật máy nhà |
| Cloudflare Worker | [worker.js](worker.js) | ❌ | Cloudflare (cron) | Serverless, khỏi VPS |
| Deno Deploy | [deno_deploy.ts](deno_deploy.ts) | ❌ | Deno Deploy (cron) | Serverless thay thế |

> Từ khi caffiliate bật **Turnstile**, chỉ **auto-click CDP** (chạy Chrome thật) là vượt được.
> Các cách bắn API trần (`auto_tool.py api`, server, Worker, Deno) chỉ còn dùng được nếu site
> tắt Turnstile — giữ lại làm phương án dự phòng.

## Cài đặt

### 1. Thư viện Python

```powershell
pip install selenium pyautogui pillow websocket-client
```

- `selenium` — extract creds + bản API cũ.
- `pyautogui` + `pillow` — auto-click bằng nhận diện ảnh nút (fallback khi CDP không tìm thấy nút).
- `websocket-client` — nói chuyện CDP với Chrome (`import websocket`).

Bản chạy trên server (`auto_tool_server.py`) chỉ dùng thư viện chuẩn — không cần cài gì.

### 2. Đăng nhập Chrome (profile riêng)

Tool dùng profile Chrome cố định `C:\SeleniumChromeProfile` để giữ phiên đăng nhập:

```powershell
python auto_tool.py login    # mở Chrome profile riêng → đăng nhập caffiliate → Enter để lưu
```

Profile này nằm **ngoài repo** (`C:\SeleniumChromeProfile`) nên không bị commit. Session Google
OAuth ở caffiliate sống lâu; chạy [session_keepalive.py](session_keepalive.py) mỗi tối để gia hạn
cookie và cảnh báo sớm nếu hết hạn (xem [Keep-alive](#keep-alive-session)).

### 3. `creds.json` (cho các cách bắn API + verify status)

Auto-click CDP tự đọc creds trong Chrome nên **không bắt buộc** có `creds.json`, nhưng bước verify
status sau khi click và các cách bắn API trần thì cần. Tạo bằng cách extract tự động từ Chrome:

```powershell
python auto_tool.py dump-creds        # → ghi creds.json (csrf_token, xeng_secret, user_id, cookies, user_agent)
```

`creds.json` đã `.gitignore`. Khi cookie hết hạn thì chạy lại lệnh trên (hoặc [dump-creds.sh](dump-creds.sh)
nếu deploy Cloudflare Worker — nó extract rồi in ra để copy vào Worker secrets).

### 4. `.env` — báo cáo Telegram

Dùng chung channel với saffi/hoantien:

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

Không có `.env` → chỉ in ra console. Tin caffiliate có header `☕ CAFFI · HH:MM:SS`
(bản server Oracle: `☕ CAFFI-Oracle`).

## Chạy — auto-click CDP (khuyến nghị)

```powershell
python auto_click.py capture    # LẦN ĐẦU: mở Chrome, chỉ vị trí nút NHẬN QUÀ → lưu template ảnh + toạ độ
python auto_click.py test       # test click NGAY (không chờ midnight)
python auto_click.py test-cdp   # test riêng luồng CDP click
python auto_click.py run        # production: Task Scheduler chạy lúc ~23:57
```

`run` làm tuần tự lúc ~23:57:

1. **Sync đồng hồ Windows** theo giờ server caffiliate qua header `Date` (HTTPS, không NTP —
   máy này NTP hỏng). Cần Task chạy **quyền admin** (`SetSystemTime`).
2. **Dò skew**: so header `Date` của caffiliate với Google/Cloudflare — phát hiện nếu caffi cố
   chỉnh giờ để bẫy tool.
3. Mở Chrome (kèm `--remote-debugging-port=9222`), chờ Turnstile tự giải, kiểm tra còn đăng nhập.
4. Lúc gần 00:00: inject JS vào Chrome qua CDP để **calibrate giờ server** (edge-detection header
   `Date` → offset dưới giây), **warm connection**, rồi **burst** nhiều `POST` canh sao cho các
   phát *đến server* trải quanh 00:00:00 — phát hợp lệ đầu tiên thắng.
5. Verify status, báo streak + thứ hạng lên Telegram.

Tinh chỉnh trong [click_config.json](click_config.json):

- `fire_offset_ms` — arrival của phát đầu so với midnight server (âm = trước).
- `burst_count`, `burst_gap_ms` — số phát và khoảng cách giữa các phát.
- `button_x/y`, `button_region` — toạ độ nút (auto-ghi bởi `capture`).

## Chạy — API burst (khi site tắt Turnstile)

```powershell
python auto_tool.py api          # Selenium extract creds → chờ midnight → burst POST
python auto_tool.py api-static   # như trên nhưng đọc creds.json (không cần Selenium)
python auto_tool.py api-now      # bắn 1 phát NGAY để test creds/signing token
python auto_tool.py now          # click nút bằng Selenium ngay (không chờ)
python auto_tool.py healthcheck  # kiểm tra còn đăng nhập không
python auto_tool.py remind       # check status, nhắc Telegram nếu chưa điểm danh hôm nay
```

## Chạy trên server (Oracle Cloud / VPS Linux)

[auto_tool_server.py](auto_tool_server.py) — không cần Chrome/Selenium, chỉ đọc `creds.json`
(extract từ máy nhà bằng `dump-creds` rồi copy lên server) và burst POST lúc midnight. Đặt cron
Linux chạy lúc 16:55 UTC (= 23:55 ICT):

```
55 16 * * * cd /path/to/caffiliate && python3 auto_tool_server.py
```

## Chạy serverless (Cloudflare Worker / Deno Deploy)

- [worker.js](worker.js) — paste vào Cloudflare Workers, đặt Cron Trigger `55 16 * * *`. Secrets:
  `COOKIES, CSRF_TOKEN, XENG_SECRET, USER_ID, USER_AGENT, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID`.
- [deno_deploy.ts](deno_deploy.ts) — paste vào Deno Deploy, cron tương tự, env vars như trên.

Khi Worker báo lỗi auth (cookie hết hạn), chạy [dump-creds.sh](dump-creds.sh) để lấy `COOKIES` +
`CSRF_TOKEN` mới rồi cập nhật vào Worker secrets.

## Keep-alive session

[session_keepalive.py](session_keepalive.py) — chạy mỗi tối ~20:00 để mở profile Chrome (gia hạn
cookie Google) và báo Telegram **ngay** nếu session đã hết hạn, thay vì phát hiện lúc 23:57 khi
không kịp login tay.

```powershell
python session_keepalive.py            # yên lặng: chỉ báo khi có vấn đề
python session_keepalive.py --verbose  # báo cả khi OK (để test)
```

## Task Scheduler (Windows)

Tạo task chạy `run` lúc 23:57 (**Run as Administrator** để sync đồng hồ ăn):

```powershell
$py = (Get-Command python).Source
schtasks /Create /TN "Caffi Autoclick" /SC DAILY /ST 23:57 /RL HIGHEST /F `
  /TR "`"$py`" `"d:\project\anhnt\caffiliate_tool\caffiliate\auto_click.py`" run"
# (khuyến nghị) keep-alive 20:00
schtasks /Create /TN "Caffi Keepalive" /SC DAILY /ST 20:00 /RL HIGHEST /F `
  /TR "`"$py`" `"d:\project\anhnt\caffiliate_tool\caffiliate\session_keepalive.py`""
```

- Đặt task auto-click **"Run only when user is logged on"** và **không khóa màn hình** lúc 00:00 —
  Turnstile cần Chrome thật hiển thị được. Tick "Wake the computer to run this task" nếu dùng laptop.
- Kiểm tra chạy tay: `schtasks /Run /TN "Caffi Autoclick"`.

## Lưu ý bảo mật

`creds.json`, `.env`, `log.txt` và profile Chrome đã `.gitignore` / nằm ngoài repo — **không commit**.
⚠️ Vượt Turnstile + ký HMAC để tự động điểm danh có thể vi phạm ToS caffiliate và **rủi ro khóa
tài khoản**.
