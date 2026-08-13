# Affiliate Check-in Tools

Bộ tool **điểm danh tự động** cho 3 site tích điểm/hoàn tiền. Mỗi site có cơ chế riêng
(chữ ký HMAC, Turnstile, JWT 15 phút…) nên tách thành 3 thư mục độc lập, nhưng dùng
**chung một channel Telegram** để báo cáo (phân biệt bằng emoji header).

| Thư mục | Site | Đặc thù kỹ thuật | README |
|---|---|---|---|
| [caffiliate/](caffiliate/) | app.caffiliate.vn | HMAC ký request + Cloudflare Turnstile → cần Chrome thật (CDP) hoặc chạy API trên server | [caffiliate/README.md](caffiliate/README.md) |
| [saffiliate/](saffiliate/) | app.saffi.vn | Bearer token (Sanctum) + Turnstile; thưởng *early-bird* → burst quanh 00:00 | [saffiliate/README.md](saffiliate/README.md) |
| [hoantien/](hoantien/) | hoantienshopee.me | JWT sống ~15', **1 tài khoản = 1 phiên** → tool tự login lại bằng Playwright | [hoantien/README.md](hoantien/README.md) |

Reset điểm danh cả 3 site đều là **00:00 giờ Việt Nam (ICT)**. Task Scheduler chạy `run`
lúc ~23:55 mỗi tối; tool tự chờ tới nửa đêm rồi burst.

## Pull code về máy mới — làm gì để chạy

### 1. Yêu cầu chung

- **Python 3.10+** (`python --version`).
- **Google Chrome** cài sẵn (caffiliate + saffiliate cần Chrome thật để vượt Turnstile).
- Cài thư viện Python — mỗi tool cần bộ khác nhau:

  ```powershell
  # caffiliate: Selenium (extract creds) + websocket-client (CDP click) + pyautogui (auto-click)
  pip install selenium pyautogui pillow websocket-client
  # saffiliate + hoantien: chỉ cần Playwright cho phần trình duyệt
  pip install playwright
  # tải browser cho Playwright (dùng chung cho saffi + hoantien)
  python -m playwright install chromium
  ```

  Phần bắn API trần (`saffi_checkin.py test`, `hoantien_checkin.py test`, `auto_tool_server.py`)
  chỉ dùng thư viện chuẩn của Python — không cần cài gì thêm.

### 2. Điền credentials (mỗi thư mục một `creds.json`)

`creds.json` **không có trong git** (đã `.gitignore`) — nên sau khi pull về máy mới, phải tạo lại.
Mỗi thư mục có `creds.example.json` mô tả các field cần điền. Cách chung: mở site (đã đăng nhập)
→ DevTools → Network → bấm điểm danh → chuột phải request → **Copy as cURL** → tách các giá trị.

Chi tiết từng field xem README của từng thư mục (bảng trên).

### 3. Cấu hình báo cáo Telegram (tùy chọn nhưng nên có)

Cả 3 tool đọc `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` từ file `.env` cùng thư mục.
Dùng **chung 1 bot + 1 chat** cho cả 3 (phân biệt bằng emoji header). Tạo `.env` ở
mỗi thư mục với 2 dòng:

```
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=987654321
```

Có sẵn `.env` ở một thư mục rồi thì copy sang thư mục khác:

```powershell
Copy-Item caffiliate\.env saffiliate\.env
Copy-Item caffiliate\.env hoantien\.env
```

Không có `.env` → tool vẫn chạy, chỉ in ra console thay vì gửi Telegram.

Header phân biệt trong cùng channel:

- `☕ CAFFI · HH:MM:SS` — caffiliate (bản Oracle server: `☕ CAFFI-Oracle`)
- `🌱 SAFFI · HH:MM:SS` — saffiliate
- `🛒 HOANTIEN · HH:MM:SS` — hoantien

### 4. Đăng nhập trình duyệt lần đầu (site có Turnstile / OAuth)

- **caffiliate**: đăng nhập tay vào profile Chrome `C:\SeleniumChromeProfile` (chạy
  `python auto_tool.py login`) — xem [caffiliate/README.md](caffiliate/README.md).
- **saffiliate**: `python saffi_checkin.py cdp-login`.
- **hoantien**: `python hoantien_checkin.py browser-login`.

### 5. Đặt Task Scheduler (Windows) chạy tự động mỗi tối

Mỗi README có lệnh `schtasks` tạo task chạy lúc 23:55. Ví dụ nhanh (chạy PowerShell
**Run as Administrator**, đổi đường dẫn nếu clone chỗ khác):

```powershell
$py = (Get-Command python).Source
$root = "d:\project\anhnt\caffiliate_tool"
schtasks /Create /TN "Saffi Checkin"    /SC DAILY /ST 23:55 /RL HIGHEST /F `
  /TR "`"$py`" `"$root\saffiliate\saffi_checkin.py`" run"
schtasks /Create /TN "Hoantien Checkin" /SC DAILY /ST 23:55 /RL HIGHEST /F `
  /TR "`"$py`" `"$root\hoantien\hoantien_checkin.py`" run"
```

caffiliate cần **quyền admin** (để sync đồng hồ) và **màn hình không khóa** lúc 00:00
(để mở được cửa sổ Chrome vượt Turnstile) — xem README của nó.

## Git remote

Repo dùng SSH: `git@github.com:anhntcn/tool_affi.git`. Máy mới cần có SSH key đã add vào
GitHub (`ssh -T git@github.com` để kiểm tra). Muốn dùng HTTPS thay vì SSH:

```powershell
git remote set-url origin https://github.com/anhntcn/tool_affi.git
```

## Bảo mật — không commit thứ gì bí mật

`.gitignore` đã loại `creds.json`, `.env`, `*.log`, và các thư mục hồ sơ trình duyệt
(`.chrome-profile/`, `.pw-profile/`, `C:\SeleniumChromeProfile` nằm ngoài repo). **Tuyệt đối
không commit / chia sẻ** các file này — chúng chứa cookie, token, và phiên đăng nhập Google
của bạn.

> ⚠️ Các tool này tự động vượt cơ chế chống bot (Turnstile, HMAC…) mà site cố tình dựng lên.
> Việc dùng có thể vi phạm điều khoản dịch vụ và **rủi ro khóa tài khoản** — cân nhắc trước khi chạy.
