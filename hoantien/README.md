# Hoantien — điểm danh tự động hoantienshopee.me

Tool gọi thẳng API check-in (`POST https://api.hoantienshopee.me/checkin`), không Selenium.
Reset điểm danh: **00:00 giờ Việt Nam (ICT)**. Thưởng cố định 500đ/ngày, mốc 7 ngày +5000đ
(không có early-bird bonus — burst chủ yếu để đua rank leaderboard và không bao giờ trượt).

## Điểm đặc thù: access token hết hạn 15 phút + **1 tài khoản = 1 phiên**

Khác với saffi/caffi, access token ở đây là **JWT sống ~15 phút**. Tool tự lo việc này:

- Đọc `exp` trong JWT, sắp hết hạn thì gọi `POST /auth/refresh` bằng `shop_refresh_token`.
- Server **xoay refresh token** mỗi lần refresh (trả token mới qua `Set-Cookie`) → tool tự
  lưu lại vào `creds.json`. Vì vậy **không sửa creds.json thủ công khi tool đang chạy**.
- Khi chạy `run`, tool refresh ngay trước 00:00 để chắc chắn còn hạn suốt lúc burst.

### ⚠️ Vì sao cứ bị `401 "Phiên làm mới không hợp lệ"`

JWT có claim **`"ver"`** (số phiên bản) gắn với tài khoản. **Server chỉ chấp nhận đúng `ver`
mới nhất, và mỗi lần có ai ĐĂNG NHẬP thì `ver` tăng lên → giết mọi token của `ver` cũ.** Đây là
cơ chế **1 tài khoản = 1 phiên** (không phải chỉ "reuse detection"). Hệ quả:

> Bạn vào web đăng nhập → `ver` nhảy lên → **phiên của tool chết ngay lập tức**. Ai đăng nhập
> SAU thì thắng; phiên còn lại chết. **Không thể** vừa chạy tool vừa dùng web song song lâu dài.
> Mẹo "cửa sổ ẩn danh" KHÔNG cứu được — vì bản chất là server chỉ cho 1 phiên sống.

### ✅ Giải pháp: tool tự đăng nhập lại bằng trình duyệt (Playwright)

Site đăng nhập bằng **Google/Shopee OAuth** (không có API email+mật khẩu) nên không thể tự "gõ
mật khẩu". Thay vào đó tool giữ sẵn một **hồ sơ trình duyệt đã đăng nhập Google** (`.pw-profile/`)
và tự mở lại mỗi khi cần token — xem [browser_login.py](browser_login.py).

**Cài 1 lần:**
```powershell
pip install playwright
python -m playwright install chromium
python hoantien_checkin.py browser-login   # mở cửa sổ → đăng nhập Google 1 lần → tự lưu
```

Sau đó tool **tự lành**: mỗi lần refresh chết (do bạn vừa vào web), `ensure_token` tự động chạy
trình duyệt headless bằng hồ sơ đã lưu → đăng nhập lại → lấy token mới → chạy tiếp. Bạn **không
phải copy cURL tay nữa**.

- Ban ngày cứ dùng web thoải mái; tool chỉ "giành" phiên đúng lúc nó chạy (điểm danh ~00:00 lúc
  bạn ngủ; nhiệm vụ 1 lần trong ngày). Bạn vào web sau đó thì lại giành lại — tool tự lấy lại ở
  lần chạy kế tiếp.
- Nếu thỉnh thoảng Google bắt xác minh lại (captcha/2FA), headless sẽ báo Telegram nhắc chạy
  `python hoantien_checkin.py browser-login` mở tay 1 lần rồi lại tự chạy như cũ.
- `.pw-profile/` chứa phiên Google → đã `.gitignore`, **tuyệt đối không commit / chia sẻ**.

Cách cũ (copy cURL tay) vẫn dùng được để mồi token lần đầu, nhưng sẽ chết mỗi khi bạn vào web —
`browser-login` là cách bền.

## Cài đặt

1. `creds.json` — copy từ `creds.example.json` rồi điền:
   - `access_token` — JWT sau `Bearer ` trong header `authorization`
   - `refresh_token` — giá trị cookie `shop_refresh_token`
   - `user_agent` — header `user-agent`
   - `refresh_url` — để trống (mặc định `https://api.hoantienshopee.me/auth/refresh`)

   Lấy nhanh: mở hoantienshopee.me (đã đăng nhập) → DevTools → Network → request `checkin`
   hoặc `status` → Copy as cURL, tách `authorization` và cookie `shop_refresh_token`.

2. `.env` (tùy chọn) — dùng chung channel Telegram với caffi/saffi:
   ```powershell
   Copy-Item saffiliate\.env hoantien\.env
   ```

## Chạy

```
python hoantien_checkin.py test       # điểm danh 1 phát ngay — kiểm tra creds
python hoantien_checkin.py now        # burst ngay lập tức
python hoantien_checkin.py run        # chờ tới 00:00 ICT rồi burst — dùng cho Task Scheduler
python hoantien_checkin.py status     # xem tình trạng + leaderboard (không điểm danh)
python hoantien_checkin.py refresh    # refresh access token thủ công rồi lưu creds.json
python hoantien_checkin.py browser-login    # LẦN ĐẦU: mở cửa sổ, đăng nhập Google, lưu hồ sơ
python hoantien_checkin.py browser-refresh  # thử tự lấy token headless từ hồ sơ đã lưu
python hoantien_checkin.py tasks      # claim nhiệm vụ hàng ngày (đăng nhập +100đ…) NGAY
python hoantien_checkin.py tasks-run  # ngủ ngẫu nhiên (≤5h) rồi claim — dùng cho Task Scheduler
```

## Nhiệm vụ hàng ngày (`tasks`)

Site có nhiệm vụ "Đăng nhập mỗi ngày" (+100đ) và các nhiệm vụ khác (`GET /tasks`, claim bằng
`POST /tasks/{id}/claim`). Tool tự lấy danh sách, tìm nhiệm vụ hàng ngày tự-claim được (lọc theo
`ruleType=login_day` / `category=hang_ngay`, bỏ cái `da_nhan` đã nhận và `dang_lam` cần thao tác
thật) rồi claim. Không cần đúng nửa đêm — chạy bất kỳ lúc nào trong ngày.

Task Scheduler chạy `tasks-run` lúc **08:00**, script tự **ngủ ngẫu nhiên tới 5 giờ** rồi mới
claim → mỗi ngày chạy một giờ khác nhau (08:00–13:00) để tránh bị phát hiện. Chỉnh khoảng ngẫu
nhiên bằng `TASKS_RANDOM_DELAY_MAX_S`.

## Cơ chế burst (giống saffi)

Burst dày lúc 00:00 (giành #1 nếu cửa mở đúng mốc), rồi **poll nhẹ 4s/lần tối đa 15'**
(`POLL_*`) tới khi nhận `checkinDate` **đúng ngày hôm nay**. Nếu token/refresh chết (401) thì
DỪNG SỚM báo lỗi thay vì hammer. Nếu ngày cũ chưa điểm danh, request đầu vớt ngày cũ nhưng vẫn
poll tiếp cho hôm nay. Hằng số ở đầu [hoantien_checkin.py](hoantien_checkin.py).

## Task Scheduler (Windows)

Chạy `run` mỗi tối lúc **23:55**:

```powershell
$py = (Get-Command python).Source
schtasks /Create /TN "Hoantien Checkin" /SC DAILY /ST 23:55 `
  /TR "`"$py`" `"d:\project\anhnt\caffiliate_tool\hoantien\hoantien_checkin.py`" run" /RL HIGHEST /F
```

Kiểm tra chạy tay: `schtasks /Run /TN "Hoantien Checkin"` (ban ngày sẽ điểm danh luôn nếu chưa).

## Thông báo Telegram

Header phân biệt: `🛒 HOANTIEN · HH:MM:SS`. Tin thành công dạng:

```
🛒 HOANTIEN · 00:00:01
✅ Điểm danh thành công
🔥 streak 2 · +500đ · ví 1.000đ
🏅 BXH #7/10 · streak 2
⚙️ burst #3/50 · fire 00:00:00.187 · 142ms
```

`creds.json` và `.env` đã được `.gitignore` — không bị commit.
