# Saffi — điểm danh tự động app.saffi.vn

Tool gọi thẳng API check-in (`POST /api/spoint/checkin`), không cần Selenium/Chrome.
Site dùng Bearer token (Laravel Sanctum) + cookie, **không có** chữ ký HMAC như caffiliate.
Reset điểm danh lúc **00:00 giờ Việt Nam (ICT)**; bấm sớm được thưởng *early-bird*.

## Cài đặt

1. `creds.json` — copy từ `creds.example.json` rồi điền:
   - `bearer_token` — giá trị sau `Bearer ` trong header `authorization`
   - `cookies` — nguyên chuỗi cookie (`XSRF-TOKEN=...; saffi_api_session=...`)
   - `xsrf_token` — giá trị header `x-xsrf-token` (bản đã URL-decode)
   - `user_agent` — header `user-agent`

   Lấy nhanh: mở app.saffi.vn → DevTools → Network → bấm điểm danh → chuột phải request
   `checkin` → Copy as cURL, rồi tách các giá trị trên ra.

2. `.env` (tùy chọn) — để nhận báo cáo Telegram. Dùng **chung channel** với caffiliate:
   copy đúng 2 dòng `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` từ `../caffiliate/.env` sang.
   Cách nhanh (PowerShell, chạy trong thư mục gốc project):

   ```powershell
   Copy-Item caffiliate\.env saffiliate\.env
   ```

   Bỏ trống / không có `.env` thì tool chỉ in ra console. Tin nhắn saffi có prefix `[Saffi]`
   nên phân biệt được với caffiliate trong cùng channel.

## Chạy

```
python saffi_checkin.py test       # điểm danh 1 phát ngay — kiểm tra creds (đã điểm danh thì chỉ báo lại)
python saffi_checkin.py now        # burst ngay lập tức
python saffi_checkin.py run        # chờ tới 00:00 ICT rồi burst — dùng cho Task Scheduler
python saffi_checkin.py status     # xem tình trạng hôm nay + early-bird + BXH (không điểm danh)
python saffi_checkin.py cdp        # điểm danh qua Chrome thật (vượt Turnstile) — xem bên dưới
python saffi_checkin.py cdp-login  # LẦN ĐẦU: mở Chrome hồ sơ riêng để đăng nhập saffi (cho chế độ cdp)
```

Sau khi điểm danh thành công, tool tự gọi thêm status/leaderboard và đính kèm thứ hạng
BXH của bạn vào tin nhắn báo cáo.

## Định dạng thông báo Telegram

Mọi tin đều có **header thống nhất** để phân biệt tool trong cùng channel với caffiliate:

- Saffi: `🌱 SAFFI · HH:MM:SS`
- Caffi: `☕ CAFFI · HH:MM:SS` (bản Oracle: `☕ CAFFI-Oracle · …`)

Ví dụ tin điểm danh thành công của Saffi:

```
🌱 SAFFI · 00:00:01
✅ Điểm danh thành công
🔥 streak 2 · +1 (+1 eb) → 3 S-Point
🏆 early-bird #2
🏅 BXH #4/10 · 9 S-Point (gold)
⚙️ burst #3/10 · fire 00:00:00.187 · 142ms
```

Dòng `⚙️` là thông tin debug: request thứ mấy trong burst thắng, thời điểm bắn, độ trễ.
Khi fail, tin có dạng `❌ Không giành được checkin HÔM NAY sau Ns` + chi tiết ở dòng dưới.

## Cơ chế burst (quan trọng)

Quan sát thực tế: **saffi mở điểm danh ngày mới TRỄ vài phút sau 00:00** (early-bird #1 thường
check-in lúc ~00:03–00:08 ICT), và server **throttle nếu bị bắn dồn dập**. Nên tool:

1. **Burst NHỎ** (~7 phát, `BURST_INTERVAL_MS`) quanh mốc 00:00 — phòng khi hôm đó mở đúng mốc.
2. Chỉ coi là xong khi nhận `success` có `checkin_date` **đúng ngày hôm nay** (parse theo ICT).
   Request rơi trước khi cửa mở trả "đã điểm danh (ngày cũ)" → bỏ qua.
3. Nếu chưa được → **poll NHẸ 1 request mỗi `POLL_GAP_MS` (4s)** cho tới khi cửa mở, tối đa
   `POLL_MAX_SECONDS` (15'). Nhẹ để không bị throttle, lâu để bắt được lúc rollover trễ; poll
   ngay khi cửa mở còn có cơ hội giành early-bird #1.
4. **Vớt streak ngày quên:** nếu hôm qua chưa điểm danh, một request đầu sẽ điểm danh cho ngày
   cũ (success ngày cũ). Tool ghi nhận "đã vớt" nhưng **vẫn poll tiếp** để giành đúng hôm nay.

Hằng số chỉnh ở đầu [saffi_checkin.py](saffi_checkin.py) (`BURST_*`, `POLL_*`, `ICT_OFFSET_HOURS`).
Nếu sau này thấy cửa mở muộn hơn 15', tăng `POLL_MAX_SECONDS`.

## Cloudflare Turnstile (từ 2026-08-10) — vì sao cần Chrome thật

Từ 2026-08-10 saffi thêm **Cloudflare Turnstile**: `POST /api/spoint/checkin` cần body
`{cf_turnstile_response: <token>}`. Bắn API trần (body rỗng) → server trả **400 "hoàn thành xác
thực chống bot (Captcha)"**. Tool phát hiện lỗi này (`captcha_required`) và **tự chuyển sang điểm
danh bằng Chrome thật** — xem [saffi_browser.py](saffi_browser.py).

Cơ chế frontend (component `CheckinHero`): Turnstile chạy **invisible**, tự giải khi load trang
(~5-7s). Nút hiện "Chờ xác thực..." rồi chuyển **"Điểm danh ngay"** khi có token; bấm 1 lần là
gửi token thật.

**Quan trọng — phải là Chrome THẬT, không phải Playwright:** Turnstile phát hiện trình duyệt do
Playwright khởi động (cờ automation / CDP fingerprint) và **không giải** challenge (kẹt "Chờ xác
thực" mãi). Giải pháp: tool **tự chạy Chrome thật** (process riêng, hồ sơ `.chrome-profile/`) với
`--remote-debugging-port`, chờ ~8s cho Turnstile tự giải khi browser còn "sạch", **rồi mới** dùng
Playwright `connect_over_cdp` để bấm nút. Đây là cách vượt Turnstile bền nhất.

**Cài 1 lần:**
```powershell
pip install playwright ; python -m playwright install chromium   # nếu chưa có
python saffi_checkin.py cdp-login   # Chrome mở → đăng nhập saffi → đóng cửa sổ (hồ sơ được lưu)
```
Sau đó `run` lúc 00:00 sẽ tự: bắn API → gặp captcha → mở Chrome thật (hồ sơ đã đăng nhập) →
điểm danh. Khi phiên Chrome hết hạn, tool báo Telegram nhắc chạy lại `cdp-login`.

- Cần **Google Chrome** cài sẵn. `.chrome-profile/` chứa phiên đăng nhập → đã `.gitignore`,
  **không commit**.
- ⚠️ Turnstile được thêm để chặn chính loại tool này. Tự động vượt nó có thể vi phạm ToS saffi và
  **rủi ro khóa tài khoản** — cân nhắc trước khi dùng.

## Task Scheduler (Windows)

Chạy `run` mỗi tối lúc **23:55**: tool chờ tới 00:00, burst nhẹ rồi poll tới khi điểm danh
được (xem "Cơ chế burst"). Phiên có thể kéo dài tới ~15' sau nửa đêm nếu cửa mở muộn.

Tạo bằng lệnh (mở PowerShell **Run as Administrator**, sửa đường dẫn python nếu cần):

```powershell
$py = (Get-Command python).Source
schtasks /Create /TN "Saffi Checkin" /SC DAILY /ST 23:55 `
  /TR "`"$py`" `"d:\project\anhnt\caffiliate_tool\saffiliate\saffi_checkin.py`" run" /RL HIGHEST /F
```

Hoặc tạo tay qua Task Scheduler GUI:

- **Trigger**: Daily, lúc `23:55`
- **Action** → Start a program:
  - Program/script: `python` (hoặc đường dẫn đầy đủ `python.exe`)
  - Add arguments: `saffi_checkin.py run`
  - Start in: `d:\project\anhnt\caffiliate_tool\saffiliate`
- **Conditions**: bỏ chọn "Start the task only if the computer is on AC power" (nếu dùng laptop);
  tick "Wake the computer to run this task" nếu muốn máy tự thức dậy điểm danh.
- ⚠️ **Do có Turnstile → phải mở Chrome thật:** đặt task **"Run only when user is logged on"** và
  **không** khóa màn hình lúc 00:00, nếu không cửa sổ Chrome không bật được → điểm danh thất bại.

Kiểm tra task chạy tay: `schtasks /Run /TN "Saffi Checkin"`

## Lưu ý về creds

- **Bearer token** là thứ chính để xác thực. Token dạng `379|...` của Sanctum thường sống
  lâu, nhưng nếu API trả 401 thì cần lấy lại token/cookie mới (đăng nhập lại qua Google rồi
  copy cURL như bước cài đặt).
- `creds.json` và `.env` đã được `.gitignore` — không bị commit lên git.
