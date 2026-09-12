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

## Job

`POST /jobs` nhận `source_id`, `audio_mode` (`replace` hoặc `mix`),
`subtitle_mode` (`burn`, `soft`, `srt`, `none`), `include_dubbing`,
`include_original_track`, `source_language`, `audio_index` và `priority`.
`GET /jobs` và `GET /jobs/{id}` trả `filename`, `original_name`,
`display_name`, `status`, `mode` và `settings_snapshot`. Tên này thuộc về job,
vì nhiều upload khác tên có thể cùng trỏ đến một source blob content-addressed.

`PATCH` hoặc `POST /jobs/{id}/priority` nhận `{ "priority": 10 }`. Chỉ job
đang chờ, tạm dừng hoặc chờ tài nguyên mới đổi ưu tiên được; job đang chạy
trả `409`.

## Artifact

`GET /jobs/{id}/artifacts` liệt kê artifact đã xác thực. Các loại sidecar là
`transcript` (JSON machine-readable, gồm các mốc và văn bản từng segment) và
`report` (JSON kiểm tra xử lý), cùng `video` và `srt` khi mode tạo ra chúng.
`GET /artifacts/{artifact_id}` hỗ trợ HTTP Range. Mỗi artifact lưu trust root
của lần xuất nên vẫn tải được sau khi `output_root` thay đổi lúc queue rảnh.

## Settings

`GET/PATCH /settings` đọc và ghi giới hạn cục bộ. Job mới chụp toàn bộ đường
dẫn model/tool, tài nguyên, giới hạn, retry và retention trong
`settings_snapshot`; các job đã nhận không bị thay đổi bởi lần chỉnh sau.
Các thay đổi managed path (`data_root`, `output_root`) bị chặn khi còn bất kỳ
job active nào, gồm `QUEUED`, `RUNNING`, `PAUSING`, `PAUSED`, `CANCELLING`,
`RETRY_WAIT` và `WAITING_RESOURCES`.
