# Hatismart cho Home Assistant

Cầu nối giữa app **Hatismart Pro** và các thiết bị vệ tinh trong mạng nội bộ (hiện tại: Frigate camera AI).
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

## Quyền
- Thành viên: xem cấu hình, ảnh, sự kiện Frigate.
- Quản trị viên: sửa cấu hình Frigate, khởi động lại Frigate, đổi địa chỉ Frigate.

Yêu cầu: Home Assistant ≥ 2024.5, Frigate ≥ 0.14.
