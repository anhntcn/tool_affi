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
python saffi_checkin.py test     # điểm danh 1 phát ngay — kiểm tra creds (đã điểm danh thì chỉ báo lại)
python saffi_checkin.py now      # burst ngay lập tức
python saffi_checkin.py run      # chờ tới 00:00 ICT rồi burst — dùng cho Task Scheduler
python saffi_checkin.py status   # xem tình trạng hôm nay + early-bird + BXH (không điểm danh)
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
Khi fail, tin có dạng `❌ Burst xN FAIL (x/N)` + thông báo lỗi ở dòng dưới.

## Task Scheduler (Windows)

Chạy `run` mỗi tối lúc **23:55** để tool tự chờ tới 00:00, đo RTT bù độ trễ mạng rồi burst.

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

Kiểm tra task chạy tay: `schtasks /Run /TN "Saffi Checkin"`

## Lưu ý về creds

- **Bearer token** là thứ chính để xác thực. Token dạng `379|...` của Sanctum thường sống
  lâu, nhưng nếu API trả 401 thì cần lấy lại token/cookie mới (đăng nhập lại qua Google rồi
  copy cURL như bước cài đặt).
- `creds.json` và `.env` đã được `.gitignore` — không bị commit lên git.
