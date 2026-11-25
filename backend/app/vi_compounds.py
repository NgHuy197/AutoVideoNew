"""Common two-syllable Vietnamese words that a caption break must not split.

Vietnamese spaces every syllable, so "báo cáo" looks like two words. This list
is not a dictionary; it covers frequent words in narration and explainers, and
any pair missing from it is simply treated as an ordinary word boundary.
"""
from __future__ import annotations

_WORDS = """
an toàn bài học ban đầu bản thân báo cáo bắt đầu bất kỳ bây giờ bên cạnh bên trong bên ngoài
bệnh viện biết bao bình thường bình luận bóng đá bởi vì buổi sáng buổi tối cá nhân các bạn
cách thức cảm giác cảm thấy cảm ơn can thiệp cạnh tranh cao cấp cấp độ câu chuyện cấu thành
cấu trúc cha mẹ chắc chắn chất lượng chi phí chi tiết chia sẻ chiến lược chính phủ chính sách
chính xác cho nên chủ yếu chú ý chuẩn bị chúng ta chúng tôi chuyên gia chuyên nghiệp chương trình
có lẽ con người cô gái công an công cụ công nghệ công nhân công thức công ty công việc cổ phiếu
cơ bản cơ hội cơ thể cuối cùng cuộc sống cửa hàng dân số dần dần dễ dàng dịch vụ doanh nghiệp
doanh thu dự án dữ liệu dường như đa số đại học đánh giá đặc biệt đầu tiên đầu tư đất nước
đầy đủ để ý điện thoại điều kiện điều tra định nghĩa đối thủ đối với đơn giản đơn vị động lực
đồng nghiệp đồng thời đồng ý đột nhiên gần đây gia đình giá trị giải pháp giải quyết giải thích
giảm giá giám đốc giáo dục giáo viên giới hạn giúp đỡ gợi ý hàng ngày hành động hạnh phúc
hệ thống hiện đại hiện nay hiện tại hiệu quả hình ảnh hình thức hoàn cảnh hoàn hảo hoàn lại
hoàn thành hoàn toàn hoạt động học sinh học tập hỗ trợ hôm nay hôm qua hợp đồng hợp tác hướng dẫn
khả năng khác biệt khách hàng khám phá khẳng định khó khăn khoa học khoản tiền không gian
khu vực kiếm tiền kiểm soát kiểm tra kinh doanh kinh nghiệm kinh tế kỹ năng kỹ thuật kết quả
kết thúc kế hoạch kế toán lãi suất lần đầu làm việc lí do lý do lịch sử liên quan liên tục
lo lắng lợi ích lợi nhuận lựa chọn luôn luôn lương thực mạng xã mặc dù mất mát mẫu mã mỗi ngày
mối quan mục đích mục tiêu nam giới năng lượng nền tảng nghiên cứu nghiêm trọng ngân hàng
ngẫu nhiên người dùng người mới nguồn gốc nguy hiểm nguyên nhân nhân viên nhận ra nhận được
nhanh chóng nhất định nhiệm vụ như vậy những gì nổi tiếng nội dung nông dân nữ giới ổn định
phân tích phát triển phát hiện phần lớn phi kinh phong cách phụ nữ phương pháp phương tiện
quan điểm quan hệ quan trọng quá trình quản lý quảng cáo quốc gia quy định quy trình quyết định
rõ ràng rủi ro sản phẩm sản xuất sau đó sinh viên sức khỏe sự kiện sử dụng suy nghĩ tác động
tài chính tài khoản tài liệu tài sản tại sao tăng trưởng tất cả tầm quan thành công thành phố
thay đổi thế giới thế nào thời gian thời điểm thông qua thông tin thông thường thú vị thua lỗ
thực hiện thực phẩm thực sự thực tế thương hiệu thường xuyên thị trường tiền bạc tiếp tục
tiếp theo tiết kiệm tiêu chuẩn tiêu dùng tin tức tình hình tình huống tình yêu tỉnh táo tổ chức
tổng cộng tổng số tốc độ trách nhiệm trẻ em trợ cấp trung bình trung tâm trước đây trước tiên
trường học tuy nhiên tuyệt vời tư duy tự nhiên ứng dụng ưu điểm vấn đề văn hóa vận chuyển
vì vậy việc làm vô cùng xã hội xác định xây dựng xem xét xu hướng xuất hiện xuất khẩu yêu cầu
ý nghĩa ý tưởng bác sĩ phẫu thuật song sinh sống sót tách rời trực tiếp nguyên tắc chăm sóc
động vật đứa trẻ trẻ con sư tử bệnh nhân y tá cảnh sát thám tử bạn bè bạn gái bạn trai
vợ chồng anh em chị em ông bà con cái hàng xóm thành phố đường phố ngôi nhà căn phòng
máy tính trí tuệ nhân tạo cảm xúc sợ hãi tức giận buồn bã vui vẻ ngạc nhiên
bí mật bí ẩn kỳ lạ đáng sợ nguy cơ cơ quan tế bào não bộ trái tim mạch máu
"""

_SYLLABLES = _WORDS.split()
COMPOUNDS = frozenset(zip(_SYLLABLES[::2], _SYLLABLES[1::2]))
