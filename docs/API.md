# Hợp đồng API cục bộ

API có prefix `/api/v1` và chạy trên loopback. Các request thay đổi dữ liệu
phải gửi cookie `videoauto_csrf` cùng header `X-CSRF-Token`.

## Preset

`GET /presets` liệt kê preset. `POST /presets` nhận:

```json
{
  "name": "Vietnamese dub",
  "description": "Tuỳ chọn mặc định",
  "config": {
    "audio_mode": "replace",
    "subtitle_mode": "burn",
    "include_dubbing": true,
    "include_original_track": false,
    "audio_index": null,
    "source_language": null,
    "priority": 0
  }
}
```

`GET`, `PATCH` và `DELETE /presets/{id}` quản lý một preset. Cấu hình được
chuẩn hoá khi ghi; tên trùng trả về `409`.

Preset, payload upload và `POST /jobs` hỗ trợ `subtitle_style` (object tùy chọn):

```json
{
  "font_family": "Noto Sans",
  "font_size": 5.5,
  "color": "#FFFFFF",
  "outline_color": "#000000",
  "outline_width": 3,
  "shadow": 1,
  "alignment": 2,
  "margin_v": 4.5,
  "margin_h": 2.083333,
  "bold": false,
  "italic": false,
  "max_chars": 42,
  "max_lines": 2
}
```

Các trường bị bỏ qua trong object được điền mặc định. `font_size` là phần
trăm chiều cao (1–15), `margin_v`/`margin_h` là phần trăm chiều cao/chiều rộng
(0–30); `outline_width` và `shadow` dùng pixel tại mốc 1080p (0–8), tự co giãn
theo chiều cao. `alignment` theo bàn phím số: 1–3 dưới, 4–6 giữa, 7–9 trên,
từ trái sang phải. `max_chars` 10–100, `max_lines` 1–4. Màu dùng `#RRGGBB`;
font dài tối đa 80 ký tự, không chứa dấu phẩy hoặc cú pháp ASS. Giá trị
không hợp lệ trả `422`. Kiểu áp dụng ở mode `burn` và được ghi vào report;
mode `soft`/`srt` không giữ kiểu này. Job/preset cũ không có object giữ kiểu
render cũ. Khi tạo job với `preset_id`, kiểu được lấy từ preset trừ khi
payload gửi `subtitle_style` để thay thế (gửi `null` để dùng kiểu cũ).

## Job

`POST /jobs` nhận `source_id`, `audio_mode` (`replace` hoặc `mix`),
`subtitle_mode` (`burn`, `soft`, `srt`, `none`), `include_dubbing`,
`include_original_track`, `source_language`, `audio_index` và `priority`.
`GET /jobs` và `GET /jobs/{id}` trả `filename`, `original_name`,
`display_name`, `status`, `mode` và `settings_snapshot`. Tên này thuộc về job,
vì nhiều upload khác tên có thể cùng trỏ đến một source blob content-addressed.

`GET /jobs/page?limit=10&offset=0` trả `{ items, total, limit, offset, counts }`
để hiển thị hàng đợi theo trang. `DELETE /jobs/{id}` gỡ job chưa chạy hoặc đã
kết thúc khỏi hàng đợi; job đang chạy trả `409` và cần được hủy trước. Thao tác
này xóa dữ liệu hàng đợi liên quan nhưng giữ file nguồn và kết quả đã xuất.

`PATCH` hoặc `POST /jobs/{id}/priority` nhận `{ "priority": 10 }`. Chỉ job
đang chờ, tạm dừng hoặc chờ tài nguyên mới đổi ưu tiên được; job đang chạy
trả `409`.

## Artifact

`GET /jobs/{id}/artifacts` liệt kê artifact đã xác thực. Các loại sidecar là
`transcript` (JSON machine-readable, gồm các mốc và văn bản từng segment) và
`report` (JSON kiểm tra xử lý), cùng `video` và `srt` khi mode tạo ra chúng.
`GET /artifacts/{artifact_id}` hỗ trợ HTTP Range. Mỗi artifact lưu trust root
của lần xuất nên vẫn tải được sau khi `output_root` thay đổi lúc queue rảnh.

## Thư mục input / output

`GET /storage-folders?scope=input|output` trả thư mục gốc và danh sách thư mục
con nhiều cấp. `POST /storage-folders` nhận `{ "scope": "input", "parent":
"Khach-hang", "name": "Du-an-A" }`. Tên và đường dẫn phải là đường dẫn tương
đối an toàn; API chặn `..`, đường dẫn tuyệt đối, ký tự cấm và tên thiết bị
Windows. `input_folder` và `output_folder` trong preset là đường dẫn tương đối
theo hai root này. Upload web lưu một bản gốc có kiểm tra SHA-256 vào input;
pipeline xuất artifact vào output. Gửi `input_folder` cho `POST /watch-folders`
để theo dõi một thư mục input do ứng dụng quản lý.

## Settings

`GET/PATCH /settings` đọc và ghi giới hạn cục bộ. Job mới chụp toàn bộ đường
dẫn model/tool, tài nguyên, giới hạn, retry và retention trong
`settings_snapshot`; các job đã nhận không bị thay đổi bởi lần chỉnh sau.
Các thay đổi managed path (`data_root`, `output_root`) bị chặn khi còn bất kỳ
job active nào, gồm `QUEUED`, `RUNNING`, `PAUSING`, `PAUSED`, `CANCELLING`,
`RETRY_WAIT` và `WAITING_RESOURCES`.
