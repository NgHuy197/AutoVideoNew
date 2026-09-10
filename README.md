# Video Auto

Video Auto là ứng dụng Windows chạy local để nhận dạng tiếng nói bằng
Whisper.cpp, dịch sang tiếng Việt bằng NLLB, tạo giọng đọc bằng Piper Banmai
và xuất video có audio/phụ đề. Dữ liệu video, mô hình và kết quả đều ở máy
cục bộ; ứng dụng không gọi dịch vụ cloud hoặc tự tải model.

## Cài đặt

Mở PowerShell tại thư mục dự án và chạy:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup.ps1
```

Script tạo `.venv`, cài đúng phiên bản Python đã kiểm thử, kiểm tra FFmpeg,
FFprobe, Whisper và ba model local, tạo/cập nhật SQLite rồi ghi manifest hash.
Nếu một asset bắt buộc chưa có, setup dừng với đường dẫn cần bổ sung.

## Chạy ứng dụng

```powershell
.\scripts\start.ps1
```

Mở `http://127.0.0.1:8765`. Chọn video local, preset audio/phụ đề và tùy
chọn lồng tiếng. Có thể thêm watch folder trong trang Cài đặt; file phải
giữ nguyên kích thước và thời gian sửa đổi trong 30 giây trước khi được đưa
vào queue.

```powershell
.\scripts\status.ps1
.\scripts\stop.ps1
```

Worker chạy thành process con độc lập với API. Queue, lease, checkpoint,
retry và kết quả được lưu trong `VIDEOAUTO_DATA_ROOT`; nếu API khởi động lại,
worker còn lease vẫn được supervisor quản lý hoặc thu hồi an toàn.

## Khởi động cùng Windows

Sau khi đã kiểm tra ứng dụng thủ công, đăng ký Scheduled Task bằng PowerShell
quyền phù hợp:

```powershell
.\scripts\install-task.ps1
```

Task dùng tài khoản hiện tại, chạy ẩn trước khi đăng nhập, tự khởi động lại
và bỏ qua instance thứ hai. Gỡ task bằng:

```powershell
.\scripts\uninstall-task.ps1
```

Lệnh này chỉ gỡ Scheduled Task, giữ nguyên dữ liệu, model và output.

## Kiểm thử

```powershell
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\python.exe -X utf8 -m pytest tests -q
npm --prefix frontend run build
```

Các probe thực tế trong `.runtime` dùng fixture và model local để kiểm tra
Whisper→NLLB→Piper, tám chế độ render, offset audio, track mặc định, Unicode,
upload resume, queue fencing và subtitle. Chưa có tuyên bố soak 24 giờ hoặc
kiểm thử reboot/pre-login trên máy đích.
"# AutoVideoNew" 
