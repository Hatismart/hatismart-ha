#!/bin/sh
# Cài/cập nhật tích hợp Hatismart. Chạy trong thư mục cấu hình HA hoặc truyền đường dẫn:
#   sh install.sh /duong/dan/config
set -e
REPO="${HATISMART_REPO:-Hatismart/hatismart-ha}"
CFG="${1:-/config}"
TMP="$(mktemp -d)"
echo "Tải Hatismart từ github.com/$REPO …"
wget -qO "$TMP/h.zip" "https://github.com/$REPO/archive/refs/heads/main.zip" || curl -fsSL -o "$TMP/h.zip" "https://github.com/$REPO/archive/refs/heads/main.zip"
unzip -q "$TMP/h.zip" -d "$TMP"
mkdir -p "$CFG/custom_components"
rm -rf "$CFG/custom_components/hatismart"
cp -r "$TMP"/*/custom_components/hatismart "$CFG/custom_components/"
rm -rf "$TMP"
echo "Xong. Khởi động lại Home Assistant rồi thêm tích hợp Hatismart."
