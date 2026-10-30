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

Script dùng Python 3.14 đã có trên máy để tạo `.venv`, cài bộ dependency đã
khóa phiên bản, sao chép FFmpeg, Whisper cùng DLL, font và ba model vào thư
mục runtime do Video Auto quản lý. Sau đó script tạo/cập nhật SQLite và ghi
manifest hash đầy đủ.
Nếu một asset bắt buộc chưa có, setup dừng với đường dẫn cần bổ sung.

Chạy setup từ PowerShell thường, không từ terminal bên trong ứng dụng Store/MSIX
(Claude, Codex…). Windows chuyển hướng ghi vào `%LOCALAPPDATA%` của các ứng
dụng đó sang thư mục riêng của chúng, nên dữ liệu và model sẽ biến mất nếu gỡ
ứng dụng. Máy này dùng `VIDEOAUTO_DATA_ROOT=C:\Users\PC\VideoAuto` trong
`.runtime\videoauto.env` để tránh việc đó.

## Chạy ứng dụng

```powershell
.\scripts\start.ps1
```

Mở `http://127.0.0.1:8765`. Chọn video local, preset audio/phụ đề và tùy
chọn lồng tiếng. Có thể thêm watch folder trong trang Cài đặt; file phải
giữ nguyên kích thước và thời gian sửa đổi trong 30 giây trước khi được đưa
vào queue.

Ngay trên giao diện, bạn có thể chọn/lưu/cập nhật/xóa preset, chọn trộn hoặc
thay audio, phụ đề burn/soft/SRT, giữ track audio gốc phụ và đặt mức ưu tiên
khi job còn chờ. Mỗi job giữ ảnh chụp bất biến của model, công cụ, giới hạn và
tài nguyên tại lúc nhận vào queue. Bản xuất kèm transcript JSON và processing
report JSON dạng sidecar; xem [hợp đồng API](docs/API.md).

Mở **Tùy chỉnh phụ đề** dưới các lựa chọn upload để chỉnh màu chữ/viền, font,
cỡ chữ, 9 vị trí, khoảng cách mép, viền, bóng, chữ đậm/nghiêng và số dòng.
Có xem trước minh họa cho video ngang/dọc; lưu preset để dùng lại. Cỡ chữ và
khoảng cách tính theo phần trăm khung hình, viền/bóng theo mốc 1080p. Font
cần được cài trên máy hoặc có trong thư mục font runtime; nếu thiếu sẽ dùng
font thay thế. Tùy chỉnh áp dụng cho video thêm sau đó ở chế độ **Ghi vào hình**.
SRT và track bật/tắt trong MP4 do trình phát quyết định kiểu hiển thị.

Trong **Tùy chọn nâng cao**, bật **Lời chào tiếng Hà Lan ở đầu video** để mở đầu bằng
câu chào do giọng Piper `nl_NL-pim-medium` đọc. Video dài thêm vài giây: khung hình
đầu đứng yên trong lúc chào, phụ đề hiện câu chào rồi lùi đúng theo. Giọng này là
tùy chọn, đặt tại `runtime\models\piper-nl\` trong thư mục dữ liệu; setup không tự
tải. Thư mục input/output và lời chào được nhớ trong trình duyệt và dùng chung
giữa tab Hàng đợi và Cài đặt; preset đang chọn sẽ giữ giá trị riêng của nó.

Trong phần upload và trang **Cài đặt**, có thể tạo rồi chọn thư mục con nhiều
cấp cho input và output. Video upload từ web được sao chép vào thư mục input
đã chọn để giữ bản gốc theo nhóm; file kết quả nằm dưới thư mục output đã chọn.
Các lựa chọn này được lưu cùng preset. Trang Cài đặt cũng cho phép bật theo dõi
một thư mục input để video được tự đưa vào hàng đợi và xuất sang đúng nhóm output.

```powershell
.\scripts\status.ps1
.\scripts\stop.ps1
```

Worker chạy thành process con độc lập với API. Queue, lease, checkpoint,
retry và kết quả được lưu trong `VIDEOAUTO_DATA_ROOT`; khởi động lại API không
làm mất tiến độ của worker. Supervisor giữ khóa runtime trong lúc chạy, nên
setup từ chối thay model/công cụ giữa một job. Xem báo cáo kiểm thử để biết các
fault case đã được kiểm tra và các giới hạn còn lại trước khi chạy 24/7.

### Tự động phục hồi và giới hạn nhận việc

Supervisor thăm dò API mỗi 15 giây sau 60 giây khởi động. Ba lần lỗi liên
tiếp sẽ khởi động lại riêng API; thời gian chờ khi crash tăng dần từ 2 đến
256 giây để tránh vòng lặp restart. Queue thread chết hoặc không tiến sang
vòng mới trong 15 phút sẽ khiến supervisor thoát với lỗi để Scheduled Task
khởi động lại. Lỗi quét thư mục, thu hồi worker và bảo trì được ghi riêng,
không ngăn các bước còn lại chạy.

Watch folder chỉ nhận tối đa 4 video mỗi lượt, dừng nhận khi có 100 job chưa
kết thúc hoặc thiếu RAM/dung lượng; tự nhận tiếp khi tài nguyên phục hồi.
File gốc được giữ tại thư mục theo dõi. Job lỗi hết retry vẫn giữ FAILED,
không lặp vô hạn. Chạy bằng `start.ps1` mặc định chống ngủ tự động khi cắm
điện; đặt `VIDEOAUTO_24_7_ENABLED=0` để tắt. Không ngăn tắt máy thủ công.

Artifact đã hoàn tất giữ trust root tuyệt đối của lần xuất. Vì vậy đổi
`output_root` sau khi queue rảnh không làm mất khả năng tải artifact cũ.

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

Task còn có trigger mỗi 5 phút để phục hồi kể cả sau khi đã dùng hết các lần
restart. Muốn dừng lâu dài, gỡ task trước khi chạy `stop.ps1`; nếu chỉ dừng
process, trigger tiếp theo sẽ bật lại. Đăng ký task có thể cần PowerShell
Run as administrator; nếu Windows trả Access is denied thì chưa có bảo đảm
tự chạy lại sau reboot. Xem [vận hành tự động](docs/TU_DONG_HOA.md).

## Kiểm thử

```powershell
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\python.exe -X utf8 -m pytest tests -q
npm --prefix frontend run build
npm --prefix frontend test
```

Các probe thực tế trong `.runtime` dùng fixture và model local để kiểm tra
Whisper→NLLB→Piper, tám chế độ render, offset audio, track mặc định, Unicode,
upload resume, queue fencing và subtitle. Kết quả chi tiết nằm trong
`docs/KIEM_THU_THUC_TE.md`; các điểm review nằm trong `docs/ASTRA_REVIEW.md`.
Chưa có tuyên bố soak 24 giờ hoặc kiểm thử reboot/pre-login trên máy này.
