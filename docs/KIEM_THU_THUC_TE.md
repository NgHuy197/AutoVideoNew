# Bằng chứng kiểm thử thực tế

Ngày kiểm tra: từ 08/09/2026, múi giờ Asia/Saigon. Agent chính thực hiện độc lập với source do Luna triển khai. Đây là báo cáo đang cập nhật trong quá trình sửa lỗi; xem [ASTRA_REVIEW.md](ASTRA_REVIEW.md) để biết kết luận review của Astra.

## Môi trường

- Windows, Ryzen 7 7840HS, CPU inference sáu luồng, không dùng CUDA.
- Python 3.14.3; môi trường QA riêng `.runtime/qa-venv`, dùng các gói AI đã cài trên máy.
- Whisper.cpp 1.9.2 small Q5_1; NLLB-200 distilled 600M; Piper Ban Mai.
- FFmpeg/FFprobe 9.0.1 cùng bộ phát hành; hash archive đã kiểm tra.
- Dữ liệu và output kiểm thử nằm trong `.runtime`, tách khỏi dữ liệu sử dụng thật.

## Những lượt đã chạy

| Lượt kiểm tra | Kết quả quan sát | Bằng chứng local |
| --- | --- | --- |
| Whisper thật với audio English khoảng 11 giây | Nhận dạng ngôn ngữ `en`, khoảng 5.94 giây | `.runtime/fixtures/whisper-reference.json` |
| NLLB và Piper thật, câu English | Sinh bản dịch và WAV tiếng Việt dài 6.385 giây | `.runtime/fixtures/ai-reference.json` |
| NLLB/Piper thật với Chinese, Japanese, Vietnamese | 3/3 pass, tổng 31.205 giây; Vietnamese giữ nguyên nội dung trước TTS. Đây chưa phải E2E ASR cho ba ngôn ngữ này | `.runtime/root-multilingual-smoke.json` |
| Pipeline ứng dụng đầy đủ, video English 11 giây | COMPLETED, 60.9008 giây; MP4 H.264/AAC, video và audio 11.000 giây; full decode thành công | `.runtime/root-e2e-result.json` |
| Upload/Origin/offset/ASS regression vòng hai | 6/6 pass; khối rỗng/thiếu bị từ chối, complete idempotent, tên tiếng Việt giữ nguyên, Origin giả bị chặn, audio giữ khoảng im lặng đầu, đồng hồ ASS đúng | `.runtime/root-regression-round2.xml` |
| Render 8 tổ hợp dubbing bật/tắt × burn/soft/SRT/none | 8/8 pass bằng FFmpeg thật; soft track đúng `mov_text`, `language=vie`; thời lượng 11.000 giây | `.runtime/root-render-matrix.json` |
| Rotation 90°, Unicode, audio bắt đầu muộn, track mặc định thứ hai | 6/6 render burn/soft pass khi truyền đúng thông số; rotation xuất 360×640, offset được giữ gần 1.915 giây | `.runtime/root-media-edges.json` |
| Ghép 300 slot giọng sau sửa | PASS, WAV 450 giây; dòng lệnh không còn tăng theo số đoạn | `.runtime/root-media-edges.json` |
| Chia văn bản dài với tokenizer NLLB thật sau sửa | Chinese/Japanese/English/Chinese không dấu câu đều PASS, tối đa 480 token; giữ nội dung nguồn khi bỏ qua khác biệt khoảng trắng | `.runtime/root-translation-split-probes-round2.json` |
| Fit WAV vào slot 1/2.5/10 giây | Các lượt đo thời lượng pass; chưa dùng kết quả này để khẳng định không mất âm cuối | `.runtime/root-fit-probes.json` |
| Deadline subprocess | Tiến trình ngủ năm giây bị chặn bởi timeout 0.25 giây, trả lỗi sau khoảng 0.304 giây | `.runtime/root-timeout-probe.json` |
| Frontend build | npm install và build pass tại thời điểm kiểm tra | `frontend/package-lock.json`, build output trong môi trường local |
| UI bằng trình duyệt thật | Upload video không audio; trạng thái chuyển QUEUED → SKIPPED_NO_AUDIO | Dữ liệu thử `.runtime/root-browser-data` |
| Hiển thị burn subtitle | Bản đầu bị chữ quá lớn; sau sửa đã xem screenshot: hai dòng đọc được ở đáy hình | `.runtime/root-caption-fixed.png` |
| Parser PowerShell | Các script setup/start/stop/status/install-task/uninstall-task không có lỗi cú pháp tại thời điểm kiểm tra | PowerShell parser chạy trực tiếp trên file |
| Cài đặt môi trường Python riêng | `scripts/setup.ps1 -SkipFrontend` hoàn tất, `.venv` độc lập có torch/Transformers/Piper, `pip check` pass; còn cần kiểm thử inference bằng môi trường này | `.runtime/root-production-freeze.txt` |
| Regression với môi trường cài đặt riêng | 4 unit + 6 API/media + 9 queue/revision của Astra đều PASS, mỗi suite trong một process riêng | `.runtime/root-regression-round3.xml`, `.runtime/astra-queue-round2.xml` |
| Editor qua trình duyệt thật | Bản đang nhập giữ nguyên qua nhiều lần polling; save tạo child QUEUED, parent COMPLETED giữ bản dịch cũ | `.runtime/root-ui-edit-probe.json` |
| Watch folder và kết quả qua UI | Thêm/xóa thư mục thử nghiệm thành công; bấm link kết quả sinh HTTP GET artifact 200 | Phiên Uvicorn QA, dữ liệu `.runtime/root-ui-final-data` |
| E2E bằng `.venv` độc lập | English 11 giây → Whisper/NLLB/Piper/burn MP4, COMPLETED trong 36.948 giây, artifact hợp lệ | `.runtime/root-production-smoke-result.json` |
| Supervisor với worker thật | Kill đúng PID đang listen API, API trở lại với PID mới và worker cũ giữ nguyên; cancel trả CANCELLING rồi CANCELLED sau khi worker dừng; job không audio kế tiếp được xử lý | `.runtime/root-supervisor-smoke-result.json` |
| Video thật dài 66.320 giây, hai chunk ASR | FAILED sau 83.963 giây: voice track 68.320 giây vượt video 2 giây. Whisper ghi cue cuối chunk đầu tới 62 giây, chồng cue chunk sau bắt đầu 60 giây. Validator chặn công bố file; đang sửa và chưa coi là pass | `.runtime/root-production-long-chunks-result.json`, `.runtime/root-long-timeline.json` |
| Editor concurrency sau sửa CAS | Astra xác minh 15 pass / 1 fail: save/save và save/queue đã pass; còn cho phép tạo draft từ parent RUNNING có transcript chưa đầy đủ | `.runtime/astra_queue_regressions.py`, `tests/test_editor_concurrency.py`, `docs/ASTRA_REVIEW.md` |
| Setup và migration cập nhật | Setup `-SkipFrontend` thành công với Alembic; lưu DATA_ROOT/FONT_DIR theo đường dẫn physical sau chuyển hướng LocalAppData. Chưa xác minh full managed model/runtime import | `.runtime/videoauto.env`, `scripts/setup.ps1` |
| Đổi generation khi lưu checkpoint | Astra bổ sung 5 regression: rollback ghi ASR/config/translation/TTS từ owner cũ và khóa SQLite trong khi chuyển owner, tất cả pass. Chưa suy ra an toàn file khi chết process | `.runtime/astra_queue_regressions.py`, `docs/ASTRA_REVIEW.md` |
| Kill/restart supervisor thật | 4/4 bước pass: API restart giữ worker, supervisor restart giữ tối đa một job active, cancel dừng worker, job tiếp theo được xử lý. Chưa khẳng định cleanup mọi process con hoặc tự khởi động cùng Windows | `.runtime/root-supervisor-restart-result.json` |
| Watcher và chọn track mặc định sau sửa legacy | 7/7 pass: đợi file ổn định, dedup nội dung, preset mới tạo job mới, ba cách chọn track đều lấy audio có lời ở track mặc định thứ hai. Dùng FFmpeg thật, thay ASR bằng bộ đo waveform | `.runtime/root-watch-track-round2.json` |
| Tên file có dấu nháy và video VFR | 3/3 pass: `O'Brien.mp4` burn được phụ đề; VFR burn/soft giữ 213 frame và 11 giây | `.runtime/root-render-extra-probes-round2.json` |
| Phục hồi video 66 giây sau sửa timeline | PASS trong 42.312 giây, MP4 hợp lệ; không gọi ASR, dịch lại đúng một đoạn nguồn đã đổi và sinh giọng lại một câu, hai chunk còn đủ và không chồng mốc thời gian | `.runtime/root-long-resume-result.json` |
| Xuất child thực tế sau sửa bản dịch | PASS trong 9.011 giây: câu hiển thị/câu đọc đã sửa được giữ, parent không thay đổi, không gọi Whisper/NLLB, Piper chỉ sinh một câu mới và dùng cache các câu còn lại | `.runtime/root-long-edit-result.json` |
| Full decode với frame hỏng cuối video | Đã tái hiện FFmpeg báo lỗi decode nhưng exit 0 khiến validator cũ chấp nhận. Luna thêm `-xerror -err_detect explode`, probe sau sửa từ chối file hỏng | `.runtime/root_corruption_probe.py`, `.runtime/root-corruption-probe.json` |
| Bộ runtime managed | PASS: 65 file hash khớp manifest, 11 file model khớp baseline trước khi sao chép; FFmpeg/FFprobe/Whisper bản managed đều khởi chạy exit 0. Import/setup lặp lại idempotent theo kiểm thử Luna | `.runtime/root-managed-audit.json`, `tests/test_packaging.py` |
| E2E bằng toàn bộ runtime managed | English 11 giây → ASR, NLLB, Piper, burn subtitle và MP4 hợp lệ; COMPLETED trong 41.475 giây | `.runtime/root-production-managed-runtime-result.json` |
| Piper/cache/fit bằng runtime managed | Piper thật sinh WAV 1.939 giây trong 7.293 giây; cache hit 0.215 giây và không load model. Fit vào slot 300/1000/5000 ms đều đúng thời lượng | `.runtime/root-tts-managed-probe.json` |
| UI upload đồng thời và reload giữa chừng | Hai video khoảng 25 MiB cùng tải; reload rồi chọn lại dùng đúng hai upload ID, không gửi lại chunk 0/1 đã xác nhận, SHA-256 toàn bộ file khớp. Cả hai cuối cùng QUEUED sau retry một lỗi backend | `.runtime/root-upload-ui-result.json`, `.runtime/root-upload-ui-requests.jsonl` |

## Lỗi được phát hiện và chuyển lại Luna

- Soft subtitle từng lỗi do đặt input SRT sau output option `-map`. Đã sửa và matrix tám tổ hợp pass.
- Caption từng bị cắt do kích thước và số dòng. Đã sửa cách chia cue và kiểm tra hình thực.
- Ghép 300 slot giọng, tương đương 7.5 phút trong fixture, từng gây WinError 206 do argv vượt giới hạn Windows. Đã đổi sang concat manifest, chạy lại PASS.
- Tách câu Chinese/Japanese không khoảng trắng từng tạo đơn vị 1,502/1,002 token, vượt giới hạn 480. Đã sửa và chạy lại bốn trường hợp bằng tokenizer thật, PASS.
- Lỗi voice track video 66 giây vượt hai giây đã sửa và E2E phục hồi/edited child pass. Review tiếp phát hiện dedup có thể làm mất từ lặp hợp lệ khi không có overlap; đang sửa, không suy từ lượt E2E này ra đúng mọi ranh giới ngôn ngữ.
- Dedup chỉ theo text đã được đổi sang yêu cầu overlap thời gian; Astra xác nhận 27/27 trường hợp của harness hiện tại pass, gồm từ lặp hợp lệ và token có hyphen.
- Hai upload khác tên nhưng cùng nội dung tranh chấp file source `.partial`, một lượt bị WinError 32. Retry từ UI phục hồi mà không gửi lại chunk. Queue còn dùng tên Source đầu tiên cho cả hai upload; đã chuyển hai lỗi này cho Luna, chưa đánh dấu đã sửa.

## Điều chưa được coi là đã nghiệm thu

- Matrix media chỉ kiểm tra renderer trực tiếp, không thay cho E2E mọi tổ hợp trong queue.
- Chưa có đủ bằng chứng phục hồi sau crash ở mọi stage và ở ranh giới công bố file/commit DB; không suy ra điều này từ unit test.
- Chưa chứng minh cài đặt production, model/runtime đã sao chép ổn định và task boot trước đăng nhập hoạt động trên máy này.
- Migration database cũ và đường dẫn physical đã được kiểm tra riêng; vẫn cần xác minh lại với bản đóng gói cuối và tác vụ chạy ngoài Codex.
- Chưa chạy soak liên tục 24 giờ hoặc reboot để nghiệm thu; không có cam kết tốc độ realtime hay độ chính xác dịch tuyệt đối.
- Ghi chú này được lập trước cập nhật ngày 12/09 và trước quyết định hoãn review Astra; xem phần cập nhật bên dưới để biết trạng thái bàn giao hiện tại.

File kết quả E2E để xem/nghe: `.runtime/root-e2e-output/english-short/english-short.vi.053655b9.mp4`.

## Cập nhật nghiệm thu 12/09/2026

Các kết quả dưới đây thay thế các ghi chú cũ nói rằng managed runtime, import
lock hoặc lifecycle sau thay đổi chưa được kiểm tra.

| Lượt kiểm tra | Kết quả quan sát | Bằng chứng local |
| --- | --- | --- |
| Setup idempotent trên runtime managed | `scripts/setup.ps1 -SkipFrontend` hoàn tất; migration lên `0002_product_features`, manifest được ghi lại và `pip check` pass | output setup ngày 12/09, `.runtime/videoauto.env` |
| Audit runtime managed sau setup | 65 file hash khớp manifest v3, 11 file baseline model khớp; FFmpeg, FFprobe và Whisper managed đều exit 0 | `.runtime/root-managed-audit.json` |
| E2E runtime managed sau setup | English 11 giây → Whisper → NLLB → Piper → burn subtitle; `COMPLETED` trong 49.236 giây; MP4, transcript JSON và report JSON đều valid | `.runtime/root-production-final-managed-result.json`, `.runtime/root-production-final-managed-output/` |
| Importer concurrent/junction/active runtime | 8/8 pass: hai import đồng thời không xóa staging đang dùng; source junction và destination junction bị từ chối; importer từ chối byte-range lock do process khác giữ và lock file không tăng kích thước qua setup lặp lại | `tests/test_packaging.py`, `.runtime/astra_packaging_current.py` |
| Supervisor sau runtime lock | 4/4 pass: restart API giữ worker, restart supervisor không vượt một job active, cancel dừng worker, queue tiến lên job kế tiếp | `.runtime/root-supervisor-restart-result.json` |
| Lifecycle API và supervisor | 2/2 pass: lifespan gọi `start` rồi `stop` khi hàng đợi nhúng bật, và không gọi cả hai khi được tắt bằng biến môi trường | `tests/test_api_lifecycle.py` |
| UI/API preset và priority | UI localhost hiển thị preset, track gốc phụ và priority; POST/PATCH/DELETE preset cùng PATCH priority pass trên server data cô lập | `.runtime/root_ui_api_probe.py` |
| Regression cuối | Python 38/38 pass; `compileall`, `pip check`, frontend build và `npm audit --omit=dev` pass | `.runtime/final-release-suite-2/`, output kiểm tra ngày 12/09 |

Runtime ownership hiện được supervisor giữ bằng `.runtime-active.lock`. Setup
kiểm tra lock này trước khi hoán đổi model/công cụ, vì vậy không cập nhật một
runtime đang được worker sử dụng.

Các giới hạn còn lại vẫn có chủ ý: chưa đăng ký Scheduled Task trên máy người
dùng, chưa kiểm thử reboot/pre-login, và chưa chạy soak liên tục 24 giờ. Các
việc đó cần chạy trên điều kiện vận hành thật; kết quả unit, lifecycle và E2E
ở trên không được dùng để thay thế chúng.

Review Astra được hoãn theo yêu cầu hiện tại; `ASTRA_REVIEW.md` được giữ làm
lịch sử đánh giá và checklist tham khảo, không là điều kiện chặn bàn giao hiện tại.
