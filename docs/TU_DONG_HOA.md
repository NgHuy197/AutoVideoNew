# Vận hành tự động

Phạm vi hiện tại: nhận video local → nhận dạng → dịch → lồng tiếng → xuất
file. Theo yêu cầu mới, chưa triển khai đăng bài hay kiểm duyệt pháp lý.

## Bắt đầu

1. Thêm thư mục chứa video trong Cài đặt → Thư mục theo dõi. Đặt thư mục
   ngoài vùng dữ liệu quản lý và thư mục output để tránh tự nhập lại kết quả.
2. Chạy `scripts/start.ps1`; video ổn định đủ 30 giây sẽ tự vào queue.
3. Chạy `scripts/install-task.ps1` bằng PowerShell có quyền đăng ký task để
   giữ dịch vụ hoạt động sau reboot và tự phục hồi supervisor bị dừng.
4. Xem trạng thái bằng `scripts/status.ps1`, lỗi tại
   `VIDEOAUTO_DATA_ROOT/logs/supervisor.jsonl` và log từng worker.

## Cơ chế mới

- API treo: probe loopback có timeout 2 giây, không dùng proxy; đợi khởi động
  60 giây, kiểm tra mỗi 15 giây, restart sau 3 lỗi liên tiếp.
- API crash liên tục: backoff 2–256 giây; reset sau 5 phút hoạt động tốt.
- Queue thread chết hoặc không lặp trong 15 phút: supervisor thoát lỗi;
  Scheduled Task phục hồi. Tác vụ dài bất thường có thể bị ngắt bởi ngưỡng này.
- Lỗi một công đoạn giám sát không ngăn chạy các công đoạn khác. Bảo trì
  ném exception sẽ thử lại sau 60 giây. Bộ nhớ chống log lặp giới hạn 256 mục.
- Watch folder: tối đa 4 file/lượt và 100 job đang hoạt động, kiểm tra RAM,
  dung lượng dự trữ cộng kích thước file trước khi sao chép, bỏ qua symlink
  và file quá lớn. Đây là giới hạn nhập từ watch folder, không giới hạn
  upload thủ công qua API. File chưa nhận vẫn ở nguyên để lần sau tiếp tục.
- Yêu cầu chống ngủ Windows được giải phóng trên đúng thread tạo nó.
- Scheduled Task khởi động khi boot và kiểm tra khởi chạy mỗi 5 phút;
  IgnoreNew tránh chạy trùng. Task được phép tiếp tục khi chuyển sang pin,
  nhưng chống ngủ chỉ áp dụng khi dùng điện AC.

## Giới hạn thực tế

Không thể vận hành vô hạn trên ổ đĩa hữu hạn: file gốc và video xuất không
bị tự xóa. Khi đạt mức dự trữ, hệ thống chờ tài nguyên. Cần kế hoạch lưu trữ
nếu muốn chạy dài hạn. Job lỗi vĩnh viễn giữ FAILED sau retry có giới hạn,
các job khác tiếp tục chạy. Hiện chưa tự lấy video mới từ Internet.

Bản cập nhật bổ sung migration `0003_watch_preset_hash` và nâng cấp tương
thích khi khởi động: database cũ thiếu `watch_seen.preset_hash` trước đây
làm watch folder lỗi ở mỗi lượt quét. Migration chỉ thêm cột, giữ lịch sử.

## Kiểm tra trên máy hiện tại

- Toàn bộ 48 kiểm thử tự động đạt. Dùng `pytest --basetemp` với một thư mục
  mới trong `.runtime` vì thư mục TEMP mặc định của môi trường bị chặn.
  Dừng dịch vụ trước khi chạy toàn bộ suite: test đóng gói chủ động từ chối
  thay runtime khi phát hiện supervisor đang chạy.
- Đã sao lưu database trước khi nâng cấp và sửa schema cũ thành công.
- Đã tạo và bật watch folder `C:\Users\PC\Videos\Video Auto Inbox`.
- Đã dừng đột ngột process API và xác nhận supervisor tự tạo API mới.
- Video fixture `english-short.mp4` được đặt tên `autonomy-check.mp4` trong
  inbox; hệ thống tự nhận, ASR, dịch, TTS và xuất MP4/transcript/report với
  trạng thái COMPLETED, không thao tác xử lý job thủ công.
- Đăng ký Scheduled Task bị Windows từ chối: `Access is denied`.
  Cần chạy script cài task trong PowerShell có quyền phù hợp để bật
  tự chạy sau reboot. Chạy nền trong phiên hiện tại không thay thế bước này.

Chưa có kiểm thử soak 24 giờ, reboot/pre-login hoặc mất điện thực tế cho
bản thay đổi này. Các kiểm thử tự động mô phỏng lỗi probe, lỗi bước giám sát,
queue đầy rồi hồi phục, thiếu tài nguyên và watchdog queue.

Muốn dừng lâu dài: gỡ Scheduled Task bằng `scripts/uninstall-task.ps1`,
sau đó chạy `scripts/stop.ps1`. Chỉ dừng process sẽ được trigger bật lại.

## Hướng tiếp theo

Sau khi xác định nơi lưu trữ và thời hạn giữ file, có thể thêm chuyển kho
lưu trữ tự động trước khi ổ đầy; gửi thông báo chỉ khi job hết retry hoặc
dịch vụ phục hồi thất bại; chạy soak với tập video đại diện. Không tự xóa
nguồn hay kết quả khi chưa có quy tắc lưu trữ rõ ràng.
