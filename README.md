# Hatismart cho Home Assistant

Cầu nối giữa app **Hatismart Pro** và các thiết bị vệ tinh trong mạng nội bộ: Frigate (camera AI) và go2rtc (camera thường).
API nằm dưới `/api/hatismart/...`, dùng xác thực sẵn có của Home Assistant — không mở thêm cổng.

## Cài đặt

### Cách 1 — HACS (khuyên dùng)
1. HACS → ⋮ → **Custom repositories** → `https://github.com/Hatismart/hatismart-ha`, loại **Integration**.
2. Tìm **Hatismart** → **Download** → khởi động lại Home Assistant.
3. Settings → Devices & services → **Add integration** → **Hatismart** → Submit.

### Cách 2 — Một lệnh (Docker)
```bash
docker exec homeassistant sh -c "wget -qO- https://raw.githubusercontent.com/Hatismart/hatismart-ha/main/install.sh | sh"
docker restart homeassistant
```
Sau đó thêm tích hợp **Hatismart** như bước 3 ở trên.

## Dùng với app
App Hatismart Pro → Thêm → **Camera AI** → **Kết nối Frigate** (tự tìm trong LAN hoặc nhập IP). Chỉ quản trị viên.

## go2rtc (camera thường, không AI)
Cài go2rtc độc lập cạnh Home Assistant (Portainer):
```yaml
go2rtc:
  image: alexxit/go2rtc:1.9.12
  network_mode: host
  restart: unless-stopped
  volumes:
    - /opt/go2rtc:/config      # go2rtc.yaml phải ghi được: app thêm/sửa camera vào đây
```
- Tích hợp tự nhận go2rtc ở `127.0.0.1:1984` khi khởi động. Máy khác hoặc có đặt tài khoản: App → Thêm thiết bị → Camera → **Kết nối go2rtc**.
- Nên đặt `api: username/password` trong `go2rtc.yaml` để máy khác trong mạng không vào tự do (tích hợp giữ tài khoản này).
- Muốn Home Assistant dùng chung go2rtc này (khỏi chạy go2rtc kèm sẵn): thêm vào `configuration.yaml`
  ```yaml
  go2rtc:
    url: http://127.0.0.1:1984
  ```
- API: `GET /api/hatismart/go2rtc/streams`, `GET …/frame.jpeg?src=`, WebSocket `…/ws?src=` (đường ký `auth/sign_path`),
  `PUT/DELETE …/streams` (quản trị). Chỉ nhận tên luồng đã có và nguồn camera (rtsp, rtsps, rtmp, http(s), onvif) —
  go2rtc tự tạo luồng từ mọi `src`, kể cả `exec:` chạy lệnh trên máy.

## Quyền
- Thành viên: xem cấu hình, ảnh, sự kiện Frigate; xem camera go2rtc (nguồn đã che mật khẩu).
- Quản trị viên: sửa cấu hình Frigate, khởi động lại Frigate, đổi địa chỉ Frigate; thêm/sửa/xoá luồng go2rtc, đổi địa chỉ go2rtc.

Yêu cầu: Home Assistant ≥ 2024.5, Frigate ≥ 0.14.
