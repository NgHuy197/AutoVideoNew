# Kiểm thử tùy chỉnh phụ đề — 06/10/2026

Đã kiểm tra lưu/nạp preset, ảnh chụp cấu hình của job, truyền kiểu vào render,
đổi kiểu khi retry upload, giá trị không hợp lệ và kiểu mặc định của job cũ.

- Python: 94 tests qua với `pytest tests --basetemp .pytest-basetemp-subtitle-all-1`.
- Frontend: `npm --prefix frontend run build` thành công; `npm --prefix frontend test`
  có 3 tests qua cho định danh upload và kiểm tra kiểu lưu trong phiên upload.
  Bộ test frontend dùng Node hỗ trợ `node:module.stripTypeScriptTypes` (đã chạy
  trên Node 25.8.0).
- FFmpeg thực tế: 6 bản xuất, khung 640×360 và 360×640, vị trí trên/giữa/dưới,
  Arial, chữ đỏ, đậm/nghiêng, viền. Kiểm tra pixel màu và tọa độ chữ sau encode,
  đọc processing report và giải mã toàn bộ video đều qua.
- Trình duyệt: đổi font/vị trí/cỡ chữ/mã màu, chữ đậm, xem trước video dọc,
  lưu preset và nạp lại sau reload đều qua. Chọn SRT khóa các trường kiểu và
  hiển thị giải thích về trình phát. Bảng mở không che tiêu đề trang.

Probe và kết quả cục bộ nằm trong `.runtime/subtitle_style_probe.py`,
`.runtime/subtitle-style-probe/results.json`; ảnh giao diện là
`.runtime/subtitle-style-ui.jpg`. Các file `.runtime` không đưa vào Git.
Không dùng model ASR/TTS trong probe render này; nguồn là video đen có audio
im lặng, phụ đề thử có dấu tiếng Việt.
