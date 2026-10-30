#!/bin/sh
# Cài camera Hatismart cho máy khách: Home Assistant chạy Docker (Portainer), không add-on.
# Một lệnh, chạy trên máy chủ (root hoặc tài khoản trong nhóm docker):
#
#   wget -qO- https://raw.githubusercontent.com/Hatismart/hatismart-ha/main/cai-dat.sh | sh
#
# Việc làm (chạy lại nhiều lần được — lần sau là cập nhật):
#   1. go2rtc: chưa có ở cổng 1984 thì tạo container `go2rtc` (mạng host, cấu hình ở /opt/go2rtc,
#      tài khoản API ngẫu nhiên để máy khác trong mạng không vào tự do). Đã có thì giữ nguyên.
#   2. Bộ cầu Hatismart (custom_components/hatismart) — bản cũ sao lưu ở /config/hatismart-backup.
#   3. configuration.yaml: thêm `go2rtc: url: http://127.0.0.1:1984` để HA dùng chung go2rtc (sao lưu trước).
#   4. Khởi động lại HA, chờ HA lên. Có HA_TOKEN thì tự thêm tích hợp Hatismart và kiểm tra.
#
# Tuỳ chọn (đặt trước `sh`, VD `... | HA_TOKEN=eyJ... sh`):
#   HA_CONTAINER=ten      tên container HA (khi máy có nhiều HA)
#   HA_TOKEN=...          token dài hạn của tài khoản quản trị → tự thêm tích hợp + kiểm tra
#   HATISMART_REF=main    nhánh/thẻ của bộ cầu (bản thử: go2rtc-bridge)
#   G2_IMAGE=alexxit/go2rtc:1.9.12   G2_DIR=/opt/go2rtc   G2_SHARE=0 (không sửa configuration.yaml)
#   DRY_RUN=1             chỉ kiểm tra và in việc sẽ làm, không thay đổi gì
set -e

REPO="Hatismart/hatismart-ha"
REF="${HATISMART_REF:-main}"
G2_IMAGE="${G2_IMAGE:-alexxit/go2rtc:1.9.12}"
G2_DIR="${G2_DIR:-/opt/go2rtc}"
G2_SHARE="${G2_SHARE:-1}"
DRY="${DRY_RUN:-0}"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mLỗi:\033[0m %s\n' "$*" >&2; exit 1; }
run()  { if [ "$DRY" = 1 ]; then printf '    (thử) %s\n' "$*"; else "$@"; fi; }

# Tải một URL ra stdout (máy có curl hoặc wget)
fetch() {
  if command -v curl >/dev/null 2>&1; then curl -fsSL -m "${2:-30}" "$1"
  elif command -v wget >/dev/null 2>&1; then wget -qO- -T "${2:-30}" "$1"
  else return 1; fi
}
# Mã HTTP của một URL (000 = không kết nối được)
http_code() {
  if command -v curl >/dev/null 2>&1; then curl -s -o /dev/null -m 3 -w '%{http_code}' "$1" 2>/dev/null || true
  else wget -q -O /dev/null -T 3 "$1" 2>/dev/null && echo 200 || echo 000; fi
}

[ "$DRY" = 1 ] && say "CHẠY THỬ: chỉ kiểm tra, không thay đổi gì"
command -v docker >/dev/null 2>&1 || die "Máy chưa có docker."
docker info >/dev/null 2>&1 || die "Không gọi được docker. Chạy bằng root, hoặc tài khoản trong nhóm docker."

# ---------- Container Home Assistant ----------
say "Tìm Home Assistant"
if [ -n "$HA_CONTAINER" ]; then
  HA="$HA_CONTAINER"
  docker inspect "$HA" >/dev/null 2>&1 || die "Không có container '$HA'."
else
  LIST=$(docker ps --format '{{.Names}} {{.Image}}' | awk '$2 ~ /home-assistant\/home-assistant|homeassistant\/home-assistant|-homeassistant:/ {print $1}')
  N=$(printf '%s\n' "$LIST" | grep -c . || true)
  [ "$N" -eq 0 ] && die "Không thấy container Home Assistant đang chạy. Đặt HA_CONTAINER=ten."
  if [ "$N" -gt 1 ]; then
    printf '    Máy có %s container HA:\n' "$N"; printf '      %s\n' $LIST
    die "Chọn một: đặt HA_CONTAINER=ten trước sh."
  fi
  HA="$LIST"
fi
NET=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$HA")
HAVER=$(docker exec "$HA" sh -c 'cat /config/.HA_VERSION 2>/dev/null' || true)
docker exec "$HA" test -f /config/configuration.yaml || die "Container '$HA' không có /config/configuration.yaml."
ok "Container: $HA (mạng $NET${HAVER:+, HA $HAVER})"
if [ "$NET" != host ]; then
  warn "HA không chạy mạng host: bộ cầu sẽ không tự thấy go2rtc ở 127.0.0.1."
  warn "Cài xong, mở app → Camera → + → Kết nối go2rtc, nhập IP máy này:1984 và tài khoản in ở cuối."
fi
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')

# ---------- go2rtc ----------
say "go2rtc"
G2_USER=""; G2_PASS=""
if [ "$(http_code http://127.0.0.1:1984/api)" != 000 ]; then
  V=$(fetch http://127.0.0.1:1984/api 3 2>/dev/null | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')
  ok "Đã có go2rtc ở cổng 1984${V:+ (bản $V)} → giữ nguyên"
  C=$(docker ps --format '{{.Names}} {{.Image}}' | awk '$2 ~ /go2rtc/ {print $1}' | head -1)
  [ -n "$C" ] && G2_YAML_SRC=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/config"}}{{.Source}}{{end}}{{end}}' "$C")
  [ -n "$G2_YAML_SRC" ] && ok "Cấu hình: $G2_YAML_SRC/go2rtc.yaml (thư mục này phải ghi được để app thêm camera)"
else
  docker inspect go2rtc >/dev/null 2>&1 && die "Có container 'go2rtc' nhưng cổng 1984 không trả lời. Xem: docker logs go2rtc"
  G2_USER=hatismart
  G2_PASS=$(tr -dc 'A-Za-z0-9' </dev/urandom 2>/dev/null | head -c 16 || true)
  [ -n "$G2_PASS" ] || G2_PASS=$(date +%s | sha256sum | head -c 16)
  say "Tạo container go2rtc ($G2_IMAGE, cấu hình $G2_DIR)"
  # Viết go2rtc.yaml qua chính image go2rtc → không cần quyền root trên máy
  if [ "$DRY" = 1 ]; then
    printf '    (thử) viết %s/go2rtc.yaml nếu chưa có (api: tài khoản hatismart, mật khẩu ngẫu nhiên)\n' "$G2_DIR"
  elif docker run --rm -v "$G2_DIR:/config" --entrypoint sh "$G2_IMAGE" -c 'test -f /config/go2rtc.yaml' 2>/dev/null; then
    ok "Giữ go2rtc.yaml có sẵn trong $G2_DIR"; G2_USER=""; G2_PASS=""
  else
    printf 'api:\n  listen: ":1984"\n  username: %s\n  password: %s\nstreams: {}\n' "$G2_USER" "$G2_PASS" |
      docker run --rm -i -v "$G2_DIR:/config" --entrypoint sh "$G2_IMAGE" -c 'cat > /config/go2rtc.yaml'
    ok "Đã viết $G2_DIR/go2rtc.yaml"
  fi
  run docker run -d --name go2rtc --network host --restart unless-stopped -v "$G2_DIR:/config" "$G2_IMAGE" >/dev/null
  if [ "$DRY" != 1 ]; then
    i=0; while [ "$(http_code http://127.0.0.1:1984/api)" = 000 ] && [ $i -lt 15 ]; do sleep 1; i=$((i+1)); done
    [ "$(http_code http://127.0.0.1:1984/api)" != 000 ] || die "go2rtc không chạy. Xem: docker logs go2rtc"
    ok "go2rtc đang chạy"
  fi
fi

# ---------- Bộ cầu Hatismart ----------
say "Bộ cầu Hatismart ($REPO @ $REF)"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
fetch "https://codeload.github.com/$REPO/tar.gz/$REF" 60 | tar -xz -C "$TMP" || die "Không tải được bộ cầu (mạng, hoặc sai HATISMART_REF)."
SRC=$(find "$TMP" -maxdepth 3 -type d -path '*/custom_components/hatismart' | head -1)
[ -n "$SRC" ] || die "Gói tải về không có custom_components/hatismart."
NEWV=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$SRC/const.py")
OLDV=$(docker exec "$HA" sh -c "sed -n 's/^VERSION = \"\(.*\)\"/\1/p' /config/custom_components/hatismart/const.py 2>/dev/null" || true)
ok "Bản mới $NEWV${OLDV:+ (đang có $OLDV)}"
if [ "$DRY" = 1 ]; then
  printf '    (thử) chép vào %s:/config/custom_components/hatismart, sao lưu bản cũ ở /config/hatismart-backup\n' "$HA"
else
  find "$SRC" -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
  tar -C "$(dirname "$SRC")" -cf - hatismart | docker exec -i "$HA" sh -c '
    set -e; mkdir -p /config/custom_components
    if [ -d /config/custom_components/hatismart ]; then
      rm -rf /config/hatismart-backup && cp -r /config/custom_components/hatismart /config/hatismart-backup
    fi
    mkdir -p /config/.hatismart-new && tar -xf - -C /config/.hatismart-new
    rm -rf /config/custom_components/hatismart && mv /config/.hatismart-new/hatismart /config/custom_components/hatismart
    rmdir /config/.hatismart-new'
  ok "Đã cài bộ cầu $NEWV${OLDV:+ (bản $OLDV sao lưu ở /config/hatismart-backup)}"
fi

# ---------- HA dùng chung go2rtc ----------
if [ "$G2_SHARE" = 1 ] && [ "$NET" = host ]; then
  say "configuration.yaml: HA dùng chung go2rtc"
  if docker exec "$HA" grep -q '^go2rtc:' /config/configuration.yaml; then
    ok "Đã có mục go2rtc: → giữ nguyên"
  elif [ "$DRY" = 1 ]; then
    printf '    (thử) thêm "go2rtc: url: http://127.0.0.1:1984" (sao lưu configuration.yaml.bak-hatismart)\n'
  else
    docker exec "$HA" sh -c 'cp /config/configuration.yaml /config/configuration.yaml.bak-hatismart &&
      printf "\n# Hatismart: dùng chung go2rtc chạy cạnh HA\ngo2rtc:\n  url: http://127.0.0.1:1984\n" >> /config/configuration.yaml'
    ok "Đã thêm (bản cũ: configuration.yaml.bak-hatismart)"
  fi
fi

# ---------- Khởi động lại HA ----------
if [ "$DRY" = 1 ]; then
  say "(thử) docker restart $HA"
  say "Chạy thử xong — không có gì bị thay đổi."
  exit 0
fi
say "Khởi động lại Home Assistant"
docker restart "$HA" >/dev/null
HA_URL="http://127.0.0.1:8123"; [ "$NET" = host ] || HA_URL="http://${HOST_IP:-127.0.0.1}:8123"
i=0; while [ "$(http_code "$HA_URL/api/")" = 000 ] && [ $i -lt 90 ]; do sleep 2; i=$((i+1)); done
[ "$(http_code "$HA_URL/api/")" != 000 ] && ok "HA đã lên" || warn "HA chưa trả lời sau 3 phút. Xem: docker logs $HA"

# ---------- Tích hợp Hatismart ----------
if [ -n "$HA_TOKEN" ] && command -v curl >/dev/null 2>&1; then
  say "Tích hợp Hatismart"
  AUTH="Authorization: Bearer $HA_TOKEN"
  sleep 5
  if [ "$(curl -s -o /dev/null -w '%{http_code}' -H "$AUTH" "$HA_URL/api/hatismart/info")" != 200 ]; then
    FID=$(curl -s -H "$AUTH" -H 'Content-Type: application/json' -d '{"handler":"hatismart"}' "$HA_URL/api/config/config_entries/flow" |
          sed -n 's/.*"flow_id": *"\([^"]*\)".*/\1/p')
    [ -n "$FID" ] && curl -s -o /dev/null -H "$AUTH" -H 'Content-Type: application/json' -d '{}' "$HA_URL/api/config/config_entries/flow/$FID"
    sleep 3
  fi
  INFO=$(curl -s -H "$AUTH" "$HA_URL/api/hatismart/info")
  case "$INFO" in
    *'"go2rtc": true'*|*'"go2rtc":true'*) ok "Bộ cầu chạy, đã nhận go2rtc: $INFO" ;;
    *version*) warn "Bộ cầu chạy nhưng chưa nhận go2rtc: $INFO" ;;
    *) warn "Chưa gọi được /api/hatismart/info (token sai, hoặc HA chưa nạp xong): $INFO" ;;
  esac
else
  say "Việc còn lại trong HA"
  printf '    Settings → Devices & services → Add integration → Hatismart → Submit (chỉ lần đầu)\n'
fi

say "Xong"
if [ -n "$G2_PASS" ]; then
  printf '    go2rtc: http://%s:1984  tài khoản %s / %s (đã lưu trong %s/go2rtc.yaml)\n' "${HOST_IP:-<ip>}" "$G2_USER" "$G2_PASS" "$G2_DIR"
fi
printf '    App Hatismart Pro (tài khoản quản trị) → tab Camera → + → chạm camera tìm thấy → nhập mật khẩu camera.\n'
printf '    Nên để luồng phụ của camera dùng H.264.\n'
