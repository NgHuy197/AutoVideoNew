# Kiểm tra upload có thể tiếp tục

1. Mở UI local, chọn hai video cùng lúc. Kiểm tra hai dòng upload xuất hiện và tối đa hai request `PUT /uploads/{id}/chunks/{index}` đang chạy song song. Phần trăm chỉ tăng sau khi response trả về với `received` chứa index khối.
2. Trong DevTools Network, chặn mạng sau khi một vài khối đã trả về. UI phải giữ dòng `Tải lên bị gián đoạn`, nút `Thử lại` và thông báo chọn lại đúng tệp.
3. Tắt/reload trình duyệt. Đặt lại đúng preset đã hiển thị trong vùng `Phiên upload dở`, chọn lại đúng video. UI phải gọi `GET /uploads/{id}` và chỉ gửi các khối chưa có trong `received`; không tạo POST upload mới.
4. Chọn cùng video sau khi đổi audio hoặc phụ đề. UI phải thông báo preset đã đổi và tạo phiên upload mới. Không được tự dùng phiên cũ; phiên cũ vẫn hiện trong vùng phiên dở để người dùng đặt lại preset nếu muốn tiếp tục.
5. Thay video bằng nội dung khác (đổi kích thước, tên, thời gian sửa đổi hoặc dữ liệu đầu/cuối) rồi chọn lại. UI phải tạo phiên mới hoặc báo nội dung đã thay đổi, không gửi tiếp vào phiên cũ.
6. Cho một chunk trả lỗi HTTP, bấm `Thử lại`, rồi xác nhận các chunk đã được máy chủ xác nhận vẫn bị bỏ qua và upload hoàn tất idempotent khi reload lại trang.
7. Trong lúc upload đang chạy, chuyển sang `Cài đặt`. Upload manager phải còn hiện số video đang tải và nút `Mở hàng đợi`; mở lại hàng đợi rồi bấm `Hủy tải lên` để xác nhận request bị abort và không còn công việc không thể điều khiển bị ẩn.
8. Bấm `Hủy tải lên` đúng lúc dòng hiển thị `Đang hoàn tất upload trên máy chủ…`. UI phải hiển thị `Đang xác minh máy chủ…`; nếu GET thấy `COMPLETED`, dòng chuyển thành đã hoàn tất và phiên được dọn; nếu GET thấy `UPLOADING`, dòng chuyển thành đã hủy và phiên vẫn được lưu. Khi trạng thái còn `COMPLETING`, UI giữ phiên và báo cần thử lại, không tuyên bố đã hủy dứt khoát.

Các bước này phù hợp với API `POST /uploads`, `GET /uploads/{id}`, `PUT /uploads/{id}/chunks/{index}` và `POST /uploads/{id}/complete`. Nhận diện tệp dùng tên, kích thước, thời gian sửa đổi và SHA-256 toàn bộ nội dung. SHA-256 được cập nhật theo lát 8 MiB, nên không cần nạp toàn bộ tệp 2 GiB vào RAM; UI phải hiển thị tiến độ băm trước khi gửi khối đầu tiên.
