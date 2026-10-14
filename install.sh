#!/bin/sh
# Cài/cập nhật tích hợp Hatismart. Chạy trong thư mục cấu hình HA hoặc truyền đường dẫn:
#   sh install.sh /duong/dan/config
set -e
REPO="${HATISMART_REPO:-Hatismart/hatismart-ha}"
CFG="${1:-/config}"
TMP="$(mktemp -d)"
echo "Tải Hatismart từ github.com/$REPO …"
URL="https://api.github.com/repos/$REPO/zipball/main"
if [ -n "$GITHUB_TOKEN" ]; then
  # repo private: dùng token chỉ-đọc (fine-grained, quyền Contents: Read)
  curl -fsSL -H "Authorization: Bearer $GITHUB_TOKEN" -o "$TMP/h.zip" "$URL" || wget -qO "$TMP/h.zip" --header="Authorization: Bearer $GITHUB_TOKEN" "$URL"
else
  curl -fsSL -o "$TMP/h.zip" "$URL" || wget -qO "$TMP/h.zip" "$URL"
fi
unzip -q "$TMP/h.zip" -d "$TMP"
mkdir -p "$CFG/custom_components"
rm -rf "$CFG/custom_components/hatismart"
cp -r "$TMP"/*/custom_components/hatismart "$CFG/custom_components/"
rm -rf "$TMP"
echo "Xong. Khởi động lại Home Assistant rồi thêm tích hợp Hatismart."
