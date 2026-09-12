# Kế hoạch triển khai Video Auto cá nhân

Mục tiêu: đưa video từ máy vào hàng đợi, dịch lời thoại sang tiếng Việt bằng các mô hình đã cài, tạo giọng Ban Mai, xuất video và phụ đề về máy. Sau khi cấu hình, mỗi video hợp lệ được xử lý tự động; lỗi của một video không chặn các video tiếp theo.

Tài liệu này diễn giải hợp đồng kỹ thuật trong [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md). Đây là yêu cầu và tiêu chí nghiệm thu, **không phải tuyên bố mọi hạng mục đã hoàn thành**. Bằng chứng đã chạy nằm trong [KIEM_THU_THUC_TE.md](KIEM_THU_THUC_TE.md); kết luận review độc lập nằm trong [ASTRA_REVIEW.md](ASTRA_REVIEW.md).

## 1. Luồng người dùng

1. Mở giao diện tiếng Việt tại `http://127.0.0.1:8765`.
2. Chọn video hoặc đặt video vào thư mục theo dõi đã cấu hình.
3. Chọn đầu ra: lồng tiếng; phụ đề; hoặc cả hai. Mặc định lồng tiếng và đốt phụ đề vào hình.
4. Chọn cách ghép giọng: thay audio gốc hoặc trộn với toàn bộ audio gốc ở mức khoảng -18 dB. Trộn audio không tách riêng lời thoại khỏi nhạc nền.
5. Chọn phụ đề: đốt vào hình, track phụ đề bật/tắt trong MP4, hoặc SRT rời. Chọn ngôn ngữ nguồn tự động hoặc chỉ định khi nhận dạng sai.
6. Theo dõi trạng thái và tiến độ từng bước. Có thể đóng trình duyệt; hàng đợi tiếp tục chạy.
7. Mở video kết quả, tải phụ đề, xem transcript và báo cáo xử lý.
8. Nếu cần sửa bản dịch, chỉnh nội dung rồi tạo một phiên bản xuất mới. Kết quả cũ vẫn còn; tái sử dụng nhận dạng và những câu TTS không đổi.

Không yêu cầu duyệt bản dịch trước mỗi video. Cảnh báo tốc độ nói cao được ghi nhận nhưng không khiến hàng đợi chờ người vận hành.

## 2. Công nghệ và cấu hình ban đầu

| Thành phần | Lựa chọn | Lý do và ràng buộc |
| --- | --- | --- |
| Giao diện | React, TypeScript, Vite | Giao diện web local; build được phục vụ bởi FastAPI |
| HTTP API | FastAPI, Python 3.14 | Giao tiếp với UI, upload và truy xuất kết quả |
| Hàng đợi | SQLite, SQLAlchemy, supervisor riêng | Lưu bền trạng thái; không phụ thuộc vòng đời request |
| Nhận dạng | Whisper.cpp small Q5_1 | Dùng model local hiện có; giữ nguyên ngôn ngữ nguồn |
| Dịch | NLLB-200 distilled 600M | Đích `vie_Latn`, CPU, chỉ nạp model từ đĩa |
| Tạo giọng | Piper, giọng Ban Mai | Model ONNX và JSON tương ứng, một giọng tiếng Việt |
| Media | FFmpeg và FFprobe cùng phiên bản | Probe, trích audio, căn thời gian, phụ đề, encode và kiểm tra |
| Vận hành Windows | Supervisor, Job Objects, Task Scheduler | Dừng cây tiến trình, khởi động lại và chạy trước đăng nhập |

Máy đã xác định dùng Ryzen 7 7840HS và GPU AMD tích hợp. Mặc định chạy CPU, sáu luồng tính toán, một video nặng tại một thời điểm. Không giả định có CUDA.

Giới hạn ban đầu: 2 GiB/video, 30 phút/video; tối ưu trước cho video dưới 15 phút. Chỉ nhận file local. Không dùng dịch vụ cloud, tải video trên mạng, nhân bản giọng, tách người nói hoặc đồng bộ khẩu hình.

## 3. Phân chia tiến trình

```text
Trình duyệt ── HTTP / SSE ── FastAPI
                               │
                             SQLite
                               │
                      Supervisor độc lập
                               │
                   Một tiến trình xử lý video
                               │
             FFmpeg / Whisper / NLLB / Piper
```

API chỉ tiếp nhận và điều khiển công việc. Supervisor đọc hàng đợi, kiểm tra tài nguyên, nhận quyền xử lý job và giám sát tiến trình con. Mỗi lần nhận job có mã lease riêng; tiến trình cũ không được ghi trạng thái hoặc công bố kết quả sau khi quyền xử lý hết hiệu lực.

Đóng UI hoặc khởi động lại API không được dừng inference. Khi supervisor khởi động lại, nó đối chiếu tiến trình đang sống và checkpoint trước khi nhận lại job. Không cho hai tiến trình cùng xuất một phiên bản.

## 4. Các giai đoạn triển khai

### Giai đoạn A — Kiểm kê và đóng gói môi trường

- Xác nhận Python, DLL của Whisper, model NLLB đầy đủ, cặp Piper ONNX/JSON và FFmpeg/FFprobe chạy được.
- Tạo môi trường Python riêng; khóa phiên bản thư viện đã kiểm thử. Kiểm tra mã thoát của mọi lệnh cài đặt.
- Sao chép model và công cụ vào kho quản lý ổn định, giữ nguyên bản gốc. Lưu manifest phiên bản và SHA-256.
- Cấu hình UTF-8, đường dẫn tuyệt đối, biến offline và tắt telemetry. Không tải model khi xử lý video.
- Cung cấp setup, start, stop, status và chẩn đoán thiếu model/runtime.

Nghiệm thu: chạy nhận dạng, dịch và tạo WAV thật từ bộ model đã cài; khởi động được từ thư mục làm việc khác; lỗi thiếu DLL hoặc model được báo rõ.

### Giai đoạn B — Dữ liệu, upload và API local

- Xây dựng Source, Job, StageRun, Segment, Artifact, WatchFolder, Event và cấu hình lưu bền.
- SQLite trên đĩa local, WAL, synchronous FULL, transaction ngắn; có đường nâng cấp schema và backup bằng SQLite Backup API.
- Upload từng khối 8 MiB, tối đa hai file cùng lúc. Kiểm tra kích thước, chỉ số, checksum và trạng thái từng khối; hỗ trợ gửi lại sau mất kết nối.
- Hoàn tất upload phải idempotent: gửi lại request không tạo job trùng. Chỉ đưa vào queue sau khi snapshot và hash nguồn hợp lệ.
- API chỉ bind loopback; kiểm tra Host/Origin chính xác, session và CSRF. Truy cập kết quả bằng artifact ID, hỗ trợ HTTP Range để tua video.

Nghiệm thu: upload bị ngắt rồi tiếp tục; tên tiếng Việt; khối rỗng, thiếu byte và ghi sau khi complete bị từ chối; web từ origin khác không điều khiển được app.

### Giai đoạn C — Probe và nhận dạng giữ nguyên thời gian

- Probe video/audio, thời lượng, track mặc định, rotation, VFR và thời điểm bắt đầu.
- Chọn audio người dùng yêu cầu, nếu không thì track mặc định rồi track đầu tiên. Phân biệt video không audio với audio không có lời nói.
- Trích PCM16 mono 16 kHz, giữ khoảng im lặng và độ lệch audio trên timeline video.
- Chia ASR khoảng 60 giây gần điểm im lặng; khi phải chồng lấn, loại bỏ câu trùng theo thời gian và nội dung.
- Whisper transcribe ngôn ngữ gốc, không dịch qua tiếng Anh. Lưu từng chunk, mã ngôn ngữ và mốc mili giây.
- Từ chối HDR trong phiên bản đầu thay vì xuất màu sai một cách im lặng.

Nghiệm thu: English/Chinese/Japanese/Vietnamese, video dọc, rotation, VFR, audio bắt đầu muộn, nhiều track, video im lặng; không trùng hoặc mất câu tại ranh giới chunk.

### Giai đoạn D — Dịch NLLB và sửa bản dịch

- Ánh xạ rõ mã ngôn ngữ Whisper sang NLLB. Tiếng Việt bỏ qua dịch; ngôn ngữ không hỗ trợ có lỗi cụ thể.
- Ghép câu thành đơn vị dịch có độ dài hợp lý; tách trước giới hạn token. Không dùng truncation làm mất phần cuối nguồn.
- Dịch batch bốn câu, CPU fp32, beam bốn; kiểm tra kết quả rỗng, lặp hoặc đạt giới hạn sinh.
- Lưu từng đơn vị dịch để retry không cần chạy lại cả video.
- Editor dùng revision kỳ vọng. Hai tab sửa đồng thời phải phát hiện xung đột; tạo revision mới từ snapshot, không sửa lịch sử đã xuất.

Nghiệm thu: câu dài có và không có khoảng trắng, dấu tiếng Việt, kết quả rỗng, ngôn ngữ không hỗ trợ, sửa đồng thời; revision mới giữ lại phần ASR và TTS có thể tái sử dụng.

### Giai đoạn E — Piper và căn giọng theo video

- Tạo WAV theo từng đoạn; cache theo toàn bộ nội dung đọc và hash model/config. Cache chỉ có hiệu lực khi file hoàn chỉnh đã kiểm tra.
- Chuẩn hóa các slot chồng lấn, bảo đảm thời lượng dương và nằm trong video.
- Nếu giọng ngắn hơn slot, chèn im lặng. Nếu dài hơn, tăng tốc toàn bộ giọng bằng chuỗi atempo; không cắt bỏ cuối câu hoặc kéo dài video.
- Đo lại sau tăng tốc. Cảnh báo vượt 1.5x và 2x để người dùng biết chất lượng nghe có thể giảm.
- Ghép timeline bằng cách có bộ nhớ và dòng lệnh giới hạn; không tạo argv vượt giới hạn Windows khi có hàng trăm câu.

Nghiệm thu: câu ngắn/dài, tăng tốc trên 2x, đoạn sát nhau, hàng trăm slot, cache bị hỏng, thay config giọng; thời lượng và nội dung giọng đều được giữ.

### Giai đoạn F — Subtitle, render và xuất nguyên tử

- Sinh SRT UTF-8 và ASS với font local Noto Sans; tối đa hai dòng mỗi cue, chia thời gian theo độ dài nội dung. Đây không phải căn từ chính xác.
- Render MP4 H.264 yuv420p CRF20 veryfast, AAC48k192k, faststart. Copy video tương thích khi không burn; không ép mọi video thành 30fps.
- Giữ geometry hiển thị và timeline. Trộn audio có limiter; track audio gốc phụ là tùy chọn riêng.
- Ghi `.partial` cùng filesystem đầu ra. Probe stream, kiểm tra phụ đề và giải mã toàn bộ trước khi rename rồi công bố artifact.
- Sai lệch thời lượng tối đa là giá trị lớn hơn giữa 100ms và hai frame. Xuất kèm transcript và báo cáo cảnh báo/thời gian.

Nghiệm thu: mọi tổ hợp đầu ra, Unicode trong đường dẫn, rotation, audio offset; lỗi render không sinh artifact hợp lệ; khôi phục được khi dừng giữa rename và ghi database.

### Giai đoạn G — Hàng đợi chạy không giám sát

- Trạng thái: QUEUED, RUNNING, RETRY_WAIT, PAUSED, WAITING_RESOURCES, FAILED, CANCELLED, COMPLETED, COMPLETED_WITH_WARNINGS và các trạng thái bỏ qua rõ lý do.
- FIFO trong cùng mức ưu tiên. Retry hai lần với độ trễ 30 và 120 giây; không để một video lỗi chặn queue.
- Heartbeat 10 giây, lease 60 giây; heartbeat không thay thế kiểm tra tiến độ thực.
- Deadline riêng: Whisper 20 phút/chunk, NLLB 5 phút/batch, Piper 2 phút/đơn vị; FFmpeg không tiến triển 5 phút phải được xử lý.
- Pause tại ranh giới đơn vị; cancel dừng cả cây tiến trình. Lưu checkpoint đã hoàn chỉnh để tiếp tục.
- Chờ khi RAM trống dưới 4 GiB; dự trữ 20 GiB đĩa cộng ước lượng xử lý, kiểm tra cả lúc upload/render.
- Watcher quét mỗi 10 giây, ổn định ít nhất 30 giây; loại file tạm, junction và thư mục app/output. Copy snapshot có kiểm tra trước/sau; dedup theo nội dung, preset và phiên bản pipeline.
- Backup DB hằng ngày giữ bảy bản. Dọn cache thành công sau bảy ngày, thất bại sau 14 ngày, upload dở sau 24 giờ, log sau 30 ngày và tối đa 200MB; không xóa file nguồn ngoài app hay kết quả cuối để lấy chỗ trống.

Nghiệm thu: dừng tiến trình ở từng stage, mất lease, khởi động lại API/supervisor, thiếu RAM/đĩa, file đang ghi trong thư mục watch, queue tiếp tục sau lỗi; không có job hoặc subprocess mồ côi.

### Giai đoạn H — Cài đặt Windows và bàn giao

- Task Scheduler chạy supervisor ẩn lúc boot, tài khoản người dùng với S4U, đường dẫn tuyệt đối, không giới hạn thời gian chạy, tự restart và chỉ một instance.
- Chế độ 24/7 ngăn idle sleep khi dùng nguồn AC theo cấu hình; không cam kết tiếp tục khi mất điện, máy hỏng hoặc chính sách đóng nắp buộc sleep.
- S4U không phụ thuộc network share hoặc file EFS. Script gỡ task không xóa model, dữ liệu hoặc kết quả.
- Chạy smoke bằng model thật; chạy thử kéo dài 24 giờ, có lỗi chủ động và reboot trước đăng nhập khi điều kiện máy cho phép.
- Bàn giao hướng dẫn tiếng Việt, dependency locks, báo cáo QA, review Astra và giới hạn còn lại.

Nghiệm thu 24/7 chỉ được đánh dấu đạt sau khi đã chạy thực tế. Unit test và một video thành công không đủ thay thế bài thử này.

## 5. Thứ tự ưu tiên và phân công

Luna Extra High thực hiện source, test, script và hướng dẫn vận hành. Agent chính kiểm kê môi trường, chuẩn bị fixture và kiểm thử thực độc lập. Astra review candidate, tập trung lỗi gây mất dữ liệu, mất câu, queue kẹt, chạy trùng và xuất sai. Luna sửa phát hiện, sau đó Astra kiểm tra lại.

Ưu tiên sửa: tính đúng đắn audio/subtitle → upload và revision → phục hồi/giới hạn tài nguyên → setup/startup → polish UI. Không dùng giao diện đẹp hoặc test mock thành công để kết luận pipeline thật đã hoàn tất.
