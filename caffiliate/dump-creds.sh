#!/usr/bin/env bash
# Refresh creds (cookies + csrf_token) khi Cloudflare Worker báo lỗi auth.
# Sau khi chạy script này, copy giá trị từ creds.json vào Cloudflare Worker secrets:
#   - COOKIES     (field "cookies")
#   - CSRF_TOKEN  (field "csrf_token")
# (USER_ID, XENG_SECRET, USER_AGENT thường ổn định, ít khi đổi)

set -e
cd "$(dirname "$0")"

echo "[1/2] Extract creds qua Selenium..."
python auto_tool.py dump-creds

echo ""
echo "[2/2] Nội dung creds.json:"
cat creds.json
echo ""
echo ""
echo "================================================================"
echo "Vào Cloudflare Dashboard → Worker caffi → Settings → Variables"
echo "Update 2 secrets:"
echo "  COOKIES     <-- copy giá trị 'cookies' ở trên"
echo "  CSRF_TOKEN  <-- copy giá trị 'csrf_token' ở trên"
echo "Save và Deploy. Xong."
echo "================================================================"
