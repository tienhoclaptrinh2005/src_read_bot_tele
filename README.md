# Telegram Order Backup – MTProto

Tool đăng nhập tài khoản bot qua MTProto, tương tự tính năng **Login as Bot
Account** của một Telegram client. Tool không dùng `getUpdates`, không đặt hoặc
xóa webhook, không gửi thông báo và không có hàm gửi/sửa/xóa/forward tin nhắn.

Khi thấy chính bot gửi thông báo giao hàng thành công, tool sẽ:

1. Đọc `order_id`, tên sản phẩm và tài khoản đã giao.
2. Chống ghi trùng bằng `order_id` trong SQLite.
3. Tạo `data/<Tên sản phẩm>.txt` nếu chưa có.
4. Mở file ở append mode `"a"` và thêm mỗi tài khoản thành một dòng.
5. Append mỗi tài khoản thành một dòng riêng trong `purchase_history.txt`.

## Định dạng đã hỗ trợ

Định dạng thực tế của bot:

```text
Đã nhận thanh toán cho đơn hàng ORDERRRZVTMPUIU (🚚 Express VPN 3 Ngày BHF). Dưới đây là tài khoản của bạn:
expressos72709x7@catshopvip.site|Admin123@
```

Kết quả:

```text
data/Express VPN 3 Ngày BHF.txt
expressos72709x7@catshopvip.site|Admin123@
```

Tool cũng tiếp tục hỗ trợ thông báo có nhãn:

```text
✅ Mua hàng thành công
Mã đơn: ORD001
User ID: 123456
Username: @userA
Sản phẩm: Canva Pro 1 Tháng
Tài khoản: account01@gmail.com|pass01
```

Đơn thanh toán bằng số dư ví được xử lý giống hệt đơn thanh toán trực tiếp. Các
cụm như `Thanh toán qua ví thành công`, `Thanh toán bằng ví thành công`,
`Đã thanh toán bằng ví` đều được nhận diện. Quan trọng nhất là thông báo đã giao
phải có mã đơn, tên sản phẩm và phần tài khoản; thông báo ví thất bại/pending/hủy
sẽ bị bỏ qua.

Một đơn có thể giao 1, 2, 3, 4, 5 hoặc nhiều tài khoản. Tool ghi nhớ
`Sản phẩm` và `Số lượng` trong tin **Xác nhận đơn hàng**, sau đó chỉ
lưu khi đã tách đủ đúng số lượng. Dữ liệu dài được hỗ trợ theo cả ba
trường hợp:

- Mỗi tài khoản đã nằm trên một dòng.
- Nhiều tài khoản bị nối liền trong cùng một code block.
- Telegram chia phần tiêu đề giao hàng và dữ liệu thành nhiều tin nhắn.

Khi chưa nhận đủ, console hiện `⏳ [WAIT ] ... 2/4 tài khoản` và chưa
ghi file. Khi đủ 4/4, file sản phẩm được append chính xác bốn dòng.
Toàn bộ các dòng vẫn thuộc cùng một `order_id`, nên Telegram gửi
lại cùng đơn cũng không làm ghi trùng.

Ví dụ đơn giao ba tài khoản:

```text
Đã nhận thanh toán cho đơn hàng ORD003 (Canva Pro 1 Tháng). Dưới đây là tài khoản của bạn:
canva01@example.com|pass01
canva02@example.com|pass02
canva03@example.com|pass03
```

File sản phẩm nhận đúng ba dòng; lịch sử cũng nhận ba dòng với `item=1/3`,
`item=2/3`, `item=3/3`.

## Cấu hình `.env`

```dotenv
API_ID=12345678
API_HASH=0123456789abcdef0123456789abcdef
BOT_TOKEN=123456789:TOKEN_MOI_CUA_BOT
ONLY_OUTGOING=true
SOURCE_CHAT_IDS=
DATA_DIR=data
STATE_DIR=state
LOG_DIR=logs
TIMEZONE=Asia/Ho_Chi_Minh
LOG_LEVEL=INFO
```

- `API_ID`, `API_HASH`: tạo bằng tài khoản Telegram cá nhân tại
  <https://my.telegram.org/apps>. Chúng nhận diện ứng dụng MTProto, không biến
  tool thành tài khoản cá nhân.
- `BOT_TOKEN`: token mới của bot bán hàng. Nếu token từng bị công khai, phải
  `/revoke` trong `@BotFather` trước khi dùng.
- `ONLY_OUTGOING=true`: chỉ xét tin nhắn do chính bot gửi, bỏ qua nội dung khách
  nhắn cho bot.
- `SOURCE_CHAT_IDS`: để trống để theo dõi mọi chat. Chỉ điền khi muốn giới hạn
  một số chat ID, phân cách bằng dấu phẩy.

Không chia sẻ `.env`, file `state/*.session`, API hash hoặc bot token.

## Chạy trên Wispbyte

1. Tải ZIP lên File Manager và giải nén trực tiếp trong `/home/container`.
2. Khi cài mới, sao chép `.env.example` thành `.env`; khi cập nhật thì giữ
   nguyên `.env` hiện có. Điền `API_ID`, `API_HASH`, `BOT_TOKEN` nếu còn trống.
3. Trong Startup/Configuration đặt:
   - `PY_FILE=main.py`
   - `REQUIREMENTS_FILE=requirements.txt`
4. Bấm **Start**.

Lần đầu, Telethon tạo file session trong `state/`. Bot đăng nhập bằng token nên
không cần mã OTP. Log chạy đúng sẽ có dạng:

```text
🟢 [READY] @ten_bot · ALL CHATS
📊 [STATS] 128 đơn · 157 tài khoản · 24 sản phẩm
```

Sau một đơn thành công, log sẽ có:

```text
✅ [SOLD ] x3 · ORD003 · Canva Pro 1 Tháng
```

Console Wispbyte dùng định dạng ngắn vì Wispbyte đã tự thêm thời gian. File
`logs/app.log` vẫn giữ ngày giờ, mức log, module, người mua, message ID và đường
dẫn file đầy đủ để kiểm tra chi tiết. Cả hai nơi đều không ghi mật khẩu tài khoản.
Những cảnh báo kết nối nội bộ của Telethon được giữ trong `logs/app.log`
nhưng không làm rối console.

## Cơ chế lưu an toàn

- File sản phẩm luôn được mở bằng mode `"a"`; không truncate/ghi đè dữ liệu cũ.
- Nếu file cũ thiếu newline ở cuối, tool tự chèn newline trước dòng mới.
- Cùng `order_id` và cùng danh sách tài khoản chỉ được ghi một lần.
- Cùng `order_id` nhưng dữ liệu khác bị từ chối.
- SQLite lưu trạng thái ghi dở để khôi phục sau khi VPS tắt đột ngột.
- Tên sản phẩm Unicode được giữ nguyên; ký tự không hợp lệ trong tên file được
  thay bằng `_`.
- Log không in token, API hash hoặc mật khẩu tài khoản đã giao.
- Tool chỉ nghe đơn mới sau khi khởi động; không phát lại toàn bộ backlog cũ.

## Khi gặp `wrong session ID` hoặc `very old message`

Bản này đã tắt chế độ đọc backlog. Nếu `logs/app.log` vẫn lặp lại cảnh
báo phiên cũ sau khi cập nhật:

1. Bấm **Stop** server Wispbyte và bảo đảm không có bản tool thứ hai đang chạy.
2. Chỉ xóa file `state/mtproto_bot_<BOT_ID>.session` và file
   `state/mtproto_bot_<BOT_ID>.session-journal` nếu có.
3. Giữ nguyên `state/orders.sqlite3`, thư mục `data/` và
   `purchase_history.txt`, sau đó bấm **Start**.

Session MTProto sẽ được tạo lại bằng bot token; dữ liệu đơn đã lưu không bị
xóa. Không xóa toàn bộ thư mục `state/` vì trong đó có cơ sở dữ liệu
chống trùng `order_id`.

## Giới hạn cần biết

Đây là observer MTProto kiểu Telegram client, không phải bản sao webhook. Telegram
không bảo đảm một bot đăng nhập đồng thời ở nhiều hệ thống sẽ nhận đủ 100% mọi
update. Tool không thay đổi webhook và không chủ động thao tác bot, nhưng vẫn là
một phiên đăng nhập bot thứ hai. Hãy kiểm thử bằng một đơn nhỏ trước khi chạy thật.

Nếu đổi sang bot khác, tool tự dùng session khác dựa trên bot ID. Không sao chép
file session sang nơi không tin cậy vì file đó có quyền truy cập tài khoản bot.

## Kiểm thử cục bộ

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```
