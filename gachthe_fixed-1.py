import asyncio
import html
import os
import random
import sqlite3
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG
# =========================================================

USER_BOT_TOKEN = os.getenv("USER_BOT_TOKEN", "8861317548:AAFmlvLtL7vu0Lrc5v5jWpBjKViV1uZqM1E").strip()

ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN", "8789675953:AAHxrvD63hw5qCNNT43Bx3MGJ8xG5l23z4M").strip()

# ID Telegram của admin
ADMIN_IDS = {
    8894046691,
}

DB_FILE = "cardbot.db"

MIN_WITHDRAW = 5000
MAX_WITHDRAW = 2_000_000

NETWORKS = {
    "viettel": "Viettel",
    "mobifone": "Mobifone",
    "vinaphone": "Vinaphone",
}

DENOMINATIONS = [
    10_000,
    20_000,
    50_000,
    100_000,
    200_000,
    500_000,
]

DEFAULT_DISCOUNT = 12.0


# =========================================================
# DATABASE
# =========================================================

def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def generate_request_code(table):
    conn = db()

    prefix = datetime.now().strftime("%d%m%y")

    while True:
        number = random.randint(0, 999999)
        code = f"CARDXVN-{prefix}-{number:06d}"

        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE request_code = ? LIMIT 1",
            (code,)
        ).fetchone()

        if not row:
            conn.close()
            return code

def init_db():
    conn = db()
    cur = conn.cursor()

    # Schema mới không có cột request_code trùng tên.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            full_name TEXT DEFAULT '',
            balance INTEGER DEFAULT 0,
            locked_balance INTEGER DEFAULT 0,
            banned INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_code TEXT DEFAULT '',
            user_id INTEGER,
            network TEXT,
            denomination INTEGER,
            serial TEXT,
            card_code TEXT,
            discount REAL DEFAULT 0,
            received INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            decided_at TEXT,
            decided_by INTEGER
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_code TEXT DEFAULT '',
            user_id INTEGER,
            method TEXT DEFAULT 'bank',
            amount INTEGER,
            bank TEXT DEFAULT '',
            account_number TEXT DEFAULT '',
            account_name TEXT DEFAULT '',
            momo_phone TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            reject_reason TEXT DEFAULT '',
            created_at TEXT,
            decided_at TEXT,
            decided_by INTEGER
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            type TEXT,
            amount INTEGER,
            balance_before INTEGER,
            balance_after INTEGER,
            note TEXT,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            network TEXT,
            denomination INTEGER,
            discount REAL,
            PRIMARY KEY(network, denomination)
        )
    """)

    # Migration cho DB cũ. Chỉ ADD khi cột chưa tồn tại.
    def ensure_column(table, column, definition):
        existing = {row[1] for row in cur.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    ensure_column("users", "locked_balance", "INTEGER DEFAULT 0")
    ensure_column("users", "banned", "INTEGER DEFAULT 0")
    ensure_column("withdrawals", "method", "TEXT DEFAULT 'bank'")
    ensure_column("withdrawals", "momo_phone", "TEXT DEFAULT ''")
    ensure_column("withdrawals", "reject_reason", "TEXT DEFAULT ''")
    ensure_column("withdrawals", "request_code", "TEXT DEFAULT ''")
    ensure_column("cards", "request_code", "TEXT DEFAULT ''")
    ensure_column("cards", "decided_at", "TEXT")
    ensure_column("cards", "decided_by", "INTEGER")
    ensure_column("withdrawals", "decided_at", "TEXT")
    ensure_column("withdrawals", "decided_by", "INTEGER")

    # Bổ sung index để truy vấn pending nhanh và mã yêu cầu không trùng trong cùng bảng.
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cards_status ON cards(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals(status)")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cards_request_code ON cards(request_code) WHERE request_code != ''")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_withdrawals_request_code ON withdrawals(request_code) WHERE request_code != ''")

    # Tạo mã cho dữ liệu cũ.
    used_codes = set()
    for table in ("cards", "withdrawals"):
        for row in cur.execute(f"SELECT request_code FROM {table} WHERE request_code IS NOT NULL AND request_code != ''"):
            used_codes.add(str(row[0]))

    for table in ("cards", "withdrawals"):
        rows = cur.execute(f"SELECT id FROM {table} WHERE request_code IS NULL OR request_code = ''").fetchall()
        for row in rows:
            while True:
                code = f"CARDXVN-{datetime.now().strftime('%d%m%y')}-{random.randint(0, 999999):06d}"
                if code not in used_codes:
                    break
            used_codes.add(code)
            cur.execute(f"UPDATE {table} SET request_code = ? WHERE id = ?", (code, row[0]))

    for network in NETWORKS:
        for denomination in DENOMINATIONS:
            cur.execute("""
                INSERT OR IGNORE INTO settings (network, denomination, discount)
                VALUES (?, ?, ?)
            """, (network, denomination, DEFAULT_DISCOUNT))

    conn.commit()
    conn.close()


# =========================================================
# DATABASE HELPERS
# =========================================================

def get_user(user_id):
    conn = db()

    row = conn.execute(
        "SELECT * FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()

    conn.close()

    return row


def ensure_user(tg_user):
    conn = db()

    row = conn.execute(
        "SELECT id FROM users WHERE id = ?",
        (tg_user.id,)
    ).fetchone()

    if not row:
        conn.execute("""
            INSERT INTO users
            (id, username, full_name, created_at)
            VALUES (?, ?, ?, ?)
        """, (
            tg_user.id,
            tg_user.username or "",
            tg_user.full_name or "",
            now()
        ))
    else:
        conn.execute("""
            UPDATE users
            SET username = ?, full_name = ?
            WHERE id = ?
        """, (
            tg_user.username or "",
            tg_user.full_name or "",
            tg_user.id
        ))

    conn.commit()
    conn.close()


def is_admin(user_id):
    return user_id in ADMIN_IDS


def is_banned(user_id):
    user = get_user(user_id)
    return bool(user and user["banned"])


def format_money(value):
    return f"{int(value):,}".replace(",", ".") + "đ"


def get_discount(network, denomination):
    conn = db()

    row = conn.execute("""
        SELECT discount
        FROM settings
        WHERE network = ? AND denomination = ?
    """, (
        network,
        denomination
    )).fetchone()

    conn.close()

    if row:
        return float(row["discount"])

    return DEFAULT_DISCOUNT


def add_transaction(
    user_id,
    tx_type,
    amount,
    before,
    after,
    note
):
    conn = db()

    conn.execute("""
        INSERT INTO transactions
        (
            user_id,
            type,
            amount,
            balance_before,
            balance_after,
            note,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        tx_type,
        amount,
        before,
        after,
        note,
        now()
    ))

    conn.commit()
    conn.close()


# =========================================================
# USER KEYBOARDS
# =========================================================

def user_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💳 Nạp thẻ",
                callback_data="u_topup"
            ),
            InlineKeyboardButton(
                "💸 Rút tiền",
                callback_data="u_withdraw"
            )
        ],
        [
            InlineKeyboardButton(
                "💰 Số dư",
                callback_data="u_balance"
            ),
            InlineKeyboardButton(
                "📜 Lịch sử",
                callback_data="u_history"
            )
        ],
        [
            InlineKeyboardButton(
                "👤 Tài khoản",
                callback_data="u_profile"
            ),
            InlineKeyboardButton(
                "📖 Hướng dẫn",
                callback_data="u_help"
            )
        ]
    ])


def back_cancel():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "↩️ Quay lại",
                callback_data="u_back"
            ),
            InlineKeyboardButton(
                "❌ Hủy",
                callback_data="u_cancel"
            )
        ]
    ])


def admin_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💳 Duyệt thẻ",
                callback_data="a_cards"
            ),
            InlineKeyboardButton(
                "💸 Duyệt rút",
                callback_data="a_withdrawals"
            )
        ],
        [
            InlineKeyboardButton(
                "👥 Người dùng",
                callback_data="a_users"
            ),
            InlineKeyboardButton(
                "📊 Thống kê",
                callback_data="a_stats"
            )
        ],
        [
            InlineKeyboardButton(
                "⚙️ Chiết khấu",
                callback_data="a_discount"
            ),
            InlineKeyboardButton(
                "📖 Hướng dẫn",
                callback_data="a_help"
            )
        ]
    ])


def admin_back():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "↩️ Quay lại",
                callback_data="a_home"
            ),
            InlineKeyboardButton(
                "❌ Hủy",
                callback_data="a_cancel"
            )
        ]
    ])


# =========================================================
# HELP
# =========================================================

USER_HELP = """
📖 <b>HƯỚNG DẪN USER BOT</b>

💳 <b>NẠP THẺ</b>
• Chọn nhà mạng
• Chọn mệnh giá
• Nhập Serial
• Nhập mã thẻ
• Kiểm tra thông tin
• Xác nhận gửi thẻ
• Chờ Admin duyệt

💸 <b>RÚT TIỀN</b>
• Chọn Ngân hàng hoặc MoMo/ZaloPay
• Nhập số tiền
• Nhập thông tin nhận tiền
• Kiểm tra thông tin
• Xác nhận Yêu cầu
• Chờ Admin xử lý

💰 <b>SỐ DƯ</b>
• Xem số dư khả dụng
• Xem tiền đang bị khóa

📜 <b>LỊCH SỬ</b>
• Xem lịch sử nạp/rút
• Xem các giao dịch cộng/trừ tiền

👤 <b>TÀI KHOẢN</b>
• Xem ID
• Username
• Số dư

↩️ <b>QUAY LẠI</b>
Quay về bước trước.

❌ <b>HỦY</b>
Hủy thao tác hiện tại.

⚠️ <b>LƯU Ý</b>
• Kiểm tra thông tin trước khi xác nhận.
• Thẻ chỉ được cộng tiền sau khi Admin duyệt.
• Rút tiền bị từ chối sẽ được hoàn lại tiền.
"""


ADMIN_HELP = """
📖 <b>HƯỚNG DẪN ADMIN BOT</b>

💳 <b>DUYỆT THẺ</b>
• Xem danh sách thẻ đang chờ.
• Kiểm tra thông tin thẻ.
• Duyệt hoặc từ chối.

💸 <b>DUYỆT RÚT</b>
• Xem yêu cầu rút tiền.
• Kiểm tra thông tin nhận tiền.
• Duyệt hoặc từ chối.
• Khi từ chối phải nhập lý do.
• Tiền sẽ được hoàn lại cho User.

👥 <b>QUẢN LÝ USER</b>
/ban ID
/unban ID

💰 <b>ĐIỀU CHỈNH TIỀN</b>
/tien ID + 10000
/tien ID - 10000

⚙️ <b>ĐỔI CHIẾT KHẤU</b>
/ck viettel 20000 12

📊 <b>THỐNG KÊ</b>
Xem tổng User, số dư và yêu cầu đang chờ.

↩️ <b>QUAY LẠI</b>
Quay về Admin Panel.

❌ <b>HỦY</b>
Hủy thao tác hiện tại.
"""


# =========================================================
# USER BOT - START
# =========================================================

async def user_start(update, context):
    user = update.effective_user

    ensure_user(user)

    if is_banned(user.id):
        await update.message.reply_text(
            "🚫 <b>Tài khoản của bạn đã bị khóa.</b>",
            parse_mode=ParseMode.HTML
        )
        return

    context.user_data.clear()

    text = (
        "🎉 <b>CHÀO MỪNG BẠN ĐẾN USER BOT</b>\n\n"
        f"👤 Xin chào: <b>{html.escape(user.full_name)}</b>\n"
        f"🆔 ID: <code>{user.id}</code>\n\n"
        + USER_HELP
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=user_menu()
    )


async def user_help_command(update, context):
    await update.message.reply_text(
        USER_HELP,
        parse_mode=ParseMode.HTML,
        reply_markup=user_menu()
    )


async def user_cancel_command(update, context):
    context.user_data.clear()

    await update.message.reply_text(
        "❌ Đã hủy thao tác.",
        reply_markup=user_menu()
    )


# =========================================================
# USER - MESSAGE GUARD
# =========================================================

async def user_text_handler(update, context):
    user = update.effective_user

    ensure_user(user)

    if is_banned(user.id):
        await update.message.reply_text(
            "🚫 Tài khoản của bạn đã bị khóa."
        )
        return

    state = context.user_data.get("state")

    if not state:
        await update.message.reply_text(
            "📌 Vui lòng chọn chức năng bằng nút bên dưới.",
            reply_markup=user_menu()
        )
        return

    text = update.message.text.strip()

    # -----------------------------------------------------
    # TOPUP SERIAL
    # -----------------------------------------------------

    if state == "topup_serial":
        if text.lower() == "/cancel":
            context.user_data.clear()

            await update.message.reply_text(
                "❌ Đã hủy.",
                reply_markup=user_menu()
            )
            return

        context.user_data["serial"] = text
        context.user_data["state"] = "topup_code"

        await update.message.reply_text(
            "🔐 <b>BƯỚC 2/2</b>\n\n"
            "⌨️ Nhập <b>mã thẻ</b>:",
            parse_mode=ParseMode.HTML,
            reply_markup=back_cancel()
        )
        return

    # -----------------------------------------------------
    # TOPUP CODE
    # -----------------------------------------------------

    if state == "topup_code":
        context.user_data["card_code"] = text
        context.user_data["state"] = "topup_confirm"

        network = context.user_data["network"]
        denomination = context.user_data["denomination"]
        serial = context.user_data["serial"]

        discount = get_discount(network, denomination)

        fee = int(denomination * discount / 100)
        received = denomination - fee

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Xác nhận",
                    callback_data="u_topup_confirm"
                )
            ],
            [
                InlineKeyboardButton(
                    "↩️ Quay lại",
                    callback_data="u_topup_back_code"
                ),
                InlineKeyboardButton(
                    "❌ Hủy",
                    callback_data="u_cancel"
                )
            ]
        ])

        await update.message.reply_text(
            "📋 <b>XÁC NHẬN NẠP THẺ</b>\n\n"
            f"🌐 Nhà mạng: <b>{NETWORKS[network]}</b>\n"
            f"💵 Mệnh giá: <b>{format_money(denomination)}</b>\n"
            f"📉 Chiết khấu: <b>{discount:.2f}%</b>\n"
            f"💸 Phí chiết khấu: <b>{format_money(fee)}</b>\n"
            f"💰 Thực nhận: <b>{format_money(received)}</b>\n"
            f"🔢 Serial: <code>{html.escape(serial)}</code>\n"
            f"🔐 Mã thẻ: <code>{html.escape(text)}</code>\n\n"
            "⚠️ Kiểm tra kỹ trước khi gửi.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )
        return

    # -----------------------------------------------------
    # WITHDRAW AMOUNT
    # -----------------------------------------------------

    if state == "withdraw_amount":
        try:
            amount = int(
                text.replace(".", "")
                .replace(",", "")
            )
        except ValueError:
            await update.message.reply_text(
                "❌ Số tiền không hợp lệ.",
                reply_markup=back_cancel()
            )
            return

        if amount < MIN_WITHDRAW:
            await update.message.reply_text(
                f"❌ Số tiền tối thiểu: {format_money(MIN_WITHDRAW)}",
                reply_markup=back_cancel()
            )
            return

        if amount > MAX_WITHDRAW:
            await update.message.reply_text(
                f"❌ Số tiền tối đa: {format_money(MAX_WITHDRAW)}",
                reply_markup=back_cancel()
            )
            return

        user = get_user(user.id)

        if user["balance"] < amount:
            await update.message.reply_text(
                "❌ Số dư không đủ.\n\n"
                f"💰 Số dư: <b>{format_money(user['balance'])}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=back_cancel()
            )
            return

        context.user_data["withdraw_amount"] = amount
        method = context.user_data["withdraw_method"]

        if method == "bank":
            context.user_data["state"] = "withdraw_bank"

            await update.message.reply_text(
                "🏦 <b>THÔNG TIN NGÂN HÀNG</b>\n\n"
                "Nhập theo mẫu:\n"
                "<code>Tên ngân hàng | Số tài khoản | Tên chủ tài khoản</code>\n\n"
                "Ví dụ:\n"
                "<code>MBBank | 0123456789 | NGUYEN VAN A</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=back_cancel()
            )
        else:
            context.user_data["state"] = "withdraw_momo"

            await update.message.reply_text(
                "📱 <b>THÔNG TIN MOMO/ZALOPAY</b>\n\n"
                "Nhập theo mẫu:\n"
                "<code>Số điện thoại | Tên chủ tài khoản</code>\n\n"
                "Ví dụ:\n"
                "<code>0987654321 | NGUYEN VAN A</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=back_cancel()
            )

        return

    # -----------------------------------------------------
    # BANK
    # -----------------------------------------------------

    if state == "withdraw_bank":
        parts = [x.strip() for x in text.split("|")]

        if len(parts) != 3:
            await update.message.reply_text(
                "❌ Sai định dạng.\n\n"
                "Dùng:\n"
                "<code>Ngân hàng | Số tài khoản | Tên</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=back_cancel()
            )
            return

        bank, account_number, account_name = parts

        context.user_data["bank"] = bank
        context.user_data["account_number"] = account_number
        context.user_data["account_name"] = account_name
        context.user_data["state"] = "withdraw_confirm"

        await show_withdraw_confirm(update, context)
        return

    # -----------------------------------------------------
    # MOMO
    # -----------------------------------------------------

    if state == "withdraw_momo":
        parts = [x.strip() for x in text.split("|")]

        if len(parts) != 2:
            await update.message.reply_text(
                "❌ Sai định dạng.\n\n"
                "Dùng:\n"
                "<code>Số điện thoại | Tên</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=back_cancel()
            )
            return

        phone, account_name = parts

        context.user_data["momo_phone"] = phone
        context.user_data["account_name"] = account_name
        context.user_data["state"] = "withdraw_confirm"

        await show_withdraw_confirm(update, context)
        return

    await update.message.reply_text(
        "📌 Không xác định được thao tác.\n"
        "Hãy dùng /start để quay về menu."
    )


async def show_withdraw_confirm(update, context):
    data = context.user_data

    method = data["withdraw_method"]
    amount = data["withdraw_amount"]

    text = (
        "📋 <b>XÁC NHẬN RÚT TIỀN</b>\n\n"
        f"💰 Số tiền: <b>{format_money(amount)}</b>\n"
    )

    if method == "bank":
        text += (
            f"🏦 Ngân hàng: <b>{html.escape(data['bank'])}</b>\n"
            f"🔢 STK: <code>{html.escape(data['account_number'])}</code>\n"
            f"👤 Chủ TK: <b>{html.escape(data['account_name'])}</b>\n"
        )
    else:
        text += (
            "📱 Phương thức: <b>MoMo</b>\n"
            f"📞 SĐT: <code>{html.escape(data['momo_phone'])}</code>\n"
            f"👤 Chủ TK: <b>{html.escape(data['account_name'])}</b>\n"
        )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Xác nhận rút",
                callback_data="u_withdraw_confirm"
            )
        ],
        [
            InlineKeyboardButton(
                "↩️ Quay lại",
                callback_data="u_withdraw_back"
            ),
            InlineKeyboardButton(
                "❌ Hủy",
                callback_data="u_cancel"
            )
        ]
    ])

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )
    else:
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )


# =========================================================
# USER CALLBACK
# =========================================================

async def user_callback(update, context):
    query = update.callback_query

    await query.answer()

    user = query.from_user

    ensure_user(user)

    if is_banned(user.id):
        await query.edit_message_text(
            "🚫 Tài khoản của bạn đã bị khóa."
        )
        return

    data = query.data

    # -----------------------------------------------------
    # CANCEL
    # -----------------------------------------------------

    if data == "u_cancel":
        context.user_data.clear()

        await query.edit_message_text(
            "❌ <b>Đã hủy thao tác.</b>\n\n"
            "Bạn có thể chọn chức năng mới.",
            parse_mode=ParseMode.HTML,
            reply_markup=user_menu()
        )
        return

    # -----------------------------------------------------
    # HELP
    # -----------------------------------------------------

    if data == "u_help":
        await query.edit_message_text(
            USER_HELP,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "↩️ Quay lại",
                        callback_data="u_home"
                    )
                ]
            ])
        )
        return

    # -----------------------------------------------------
    # HOME
    # -----------------------------------------------------

    if data == "u_home":
        context.user_data.clear()

        await query.edit_message_text(
            "🏠 <b>MENU CHÍNH</b>\n\n"
            "📌 Chọn chức năng:",
            parse_mode=ParseMode.HTML,
            reply_markup=user_menu()
        )
        return

    # -----------------------------------------------------
    # TOPUP
    # -----------------------------------------------------

    if data == "u_topup":
        context.user_data.clear()
        context.user_data["state"] = "topup_network"

        keyboard = []

        for key, name in NETWORKS.items():
            keyboard.append([
                InlineKeyboardButton(
                    f"🌐 {name}",
                    callback_data=f"u_network_{key}"
                )
            ])

        keyboard.append([
            InlineKeyboardButton(
                "↩️ Quay lại",
                callback_data="u_home"
            ),
            InlineKeyboardButton(
                "❌ Hủy",
                callback_data="u_cancel"
            )
        ])

        await query.edit_message_text(
            "💳 <b>NẠP THẺ</b>\n\n"
            "🌐 Chọn nhà mạng:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if data.startswith("u_network_"):
        network = data.replace(
            "u_network_",
            ""
        )

        context.user_data["network"] = network
        context.user_data["state"] = "topup_denomination"

        keyboard = []

        for denomination in DENOMINATIONS:
            discount = get_discount(
                network,
                denomination
            )

            keyboard.append([
                InlineKeyboardButton(
                    f"💵 {format_money(denomination)} "
                    f"• CK {discount:.0f}%",
                    callback_data=f"u_denom_{denomination}"
                )
            ])

        keyboard.append([
            InlineKeyboardButton(
                "↩️ Quay lại",
                callback_data="u_topup"
            ),
            InlineKeyboardButton(
                "❌ Hủy",
                callback_data="u_cancel"
            )
        ])

        await query.edit_message_text(
            f"🌐 Nhà mạng: <b>{NETWORKS[network]}</b>\n\n"
            "💵 Chọn mệnh giá:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if data.startswith("u_denom_"):
        denomination = int(
            data.replace(
                "u_denom_",
                ""
            )
        )

        context.user_data["denomination"] = denomination
        context.user_data["state"] = "topup_serial"

        await query.edit_message_text(
            "💳 <b>NẠP THẺ</b>\n\n"
            f"💵 Mệnh giá: <b>{format_money(denomination)}</b>\n\n"
            "⌨️ Nhập <b>Serial</b>:",
            parse_mode=ParseMode.HTML,
            reply_markup=back_cancel()
        )
        return

    if data == "u_topup_back_code":
        context.user_data["state"] = "topup_code"

        await query.edit_message_text(
            "🔐 Nhập lại <b>mã thẻ</b>:",
            parse_mode=ParseMode.HTML,
            reply_markup=back_cancel()
        )
        return

    if data == "u_topup_confirm":
        if context.user_data.get("state") != "topup_confirm":
            await query.edit_message_text("❌ Yêu cầu này đã được xử lý hoặc đã hết phiên.", reply_markup=user_menu())
            return
        network = context.user_data["network"]
        denomination = context.user_data["denomination"]
        serial = context.user_data["serial"]
        card_code = context.user_data["card_code"]

        discount = get_discount(
            network,
            denomination
        )

        fee = int(
            denomination * discount / 100
        )

        received = denomination - fee

        # Tạo mã yêu cầu random 6 số
        request_code = generate_request_code("cards")

        conn = db()

        cur = conn.execute("""
            INSERT INTO cards
            (
                request_code,
                user_id,
                network,
                denomination,
                serial,
                card_code,
                discount,
                received,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """, (
            request_code,
            user.id,
            network,
            denomination,
            serial,
            card_code,
            discount,
            received,
            now()
        ))

        card_id = cur.lastrowid

        conn.commit()
        conn.close()

        context.user_data.clear()

        await query.edit_message_text(
            "✅ <b>ĐÃ GỬI THẺ</b>\n\n"
            f"🆔 Mã yêu cầu: <code>{request_code}</code>\n"
            f"🌐 Nhà mạng: <b>{NETWORKS[network]}</b>\n"
            f"💵 Mệnh giá: <b>{format_money(denomination)}</b>\n"
            f"📉 Chiết khấu: <b>{discount:.2f}%</b>\n"
            f"💰 Dự kiến nhận: <b>{format_money(received)}</b>\n\n"
            "⏳ Vui lòng chờ Admin kiểm tra và duyệt.",
            parse_mode=ParseMode.HTML,
            reply_markup=user_menu()
        )

        await notify_admins_card(
            context,
            card_id,
            request_code,
            user.id,
            network,
            denomination,
            serial,
            card_code,
            discount,
            received
        )

        return

    # -----------------------------------------------------
    # BALANCE
    # -----------------------------------------------------

    if data == "u_balance":
        user_data = get_user(user.id)

        await query.edit_message_text(
            "💰 <b>SỐ DƯ</b>\n\n"
            f"💵 Khả dụng: <b>{format_money(user_data['balance'])}</b>\n"
            f"🔒 Đang khóa: <b>{format_money(user_data['locked_balance'])}</b>\n"
            f"💎 Tổng: <b>{format_money(user_data['balance'] + user_data['locked_balance'])}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "↩️ Quay lại",
                        callback_data="u_home"
                    )
                ]
            ])
        )
        return

    # -----------------------------------------------------
    # PROFILE
    # -----------------------------------------------------

    if data == "u_profile":
        user_data = get_user(user.id)

        username = (
            "@" + user_data["username"]
            if user_data["username"]
            else "Không có"
        )

        await query.edit_message_text(
            "👤 <b>TÀI KHOẢN</b>\n\n"
            f"🆔 ID: <code>{user.id}</code>\n"
            f"👤 Tên: <b>{html.escape(user_data['full_name'])}</b>\n"
            f"🔗 Username: <b>{html.escape(username)}</b>\n"
            f"💰 Số dư: <b>{format_money(user_data['balance'])}</b>\n"
            f"🔒 Đang khóa: <b>{format_money(user_data['locked_balance'])}</b>\n"
            f"📅 Tham gia: <code>{user_data['created_at']}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "↩️ Quay lại",
                        callback_data="u_home"
                    )
                ]
            ])
        )
        return

    # -----------------------------------------------------
    # HISTORY
    # -----------------------------------------------------

    if data == "u_history":
        conn = db()

        rows = conn.execute("""
            SELECT *
            FROM transactions
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 15
        """, (
            user.id,
        )).fetchall()

        conn.close()

        if not rows:
            text = (
                "📜 <b>LỊCH SỬ</b>\n\n"
                "Chưa có giao dịch."
            )
        else:
            text = "📜 <b>LỊCH SỬ GIAO DỊCH</b>\n\n"

            for row in rows:
                sign = "+" if row["amount"] >= 0 else ""

                text += (
                    f"• <b>{html.escape(row['type'])}</b>\n"
                    f"  💰 {sign}{format_money(row['amount'])}\n"
                    f"  📝 {html.escape(row['note'])}\n"
                    f"  🕐 {row['created_at']}\n\n"
                )

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "↩️ Quay lại",
                        callback_data="u_home"
                    )
                ]
            ])
        )
        return

    # -----------------------------------------------------
    # WITHDRAW
    # -----------------------------------------------------

    if data == "u_withdraw":
        context.user_data.clear()

        await query.edit_message_text(
            "💸 <b>RÚT TIỀN</b>\n\n"
            f"💵 Tối thiểu: <b>{format_money(MIN_WITHDRAW)}</b>\n"
            f"💵 Tối đa: <b>{format_money(MAX_WITHDRAW)}</b>\n\n"
            "Chọn phương thức:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🏦 Ngân hàng",
                        callback_data="u_w_bank"
                    ),
                    InlineKeyboardButton(
                        "📱 MoMo/ZaloPay",
                        callback_data="u_w_momo"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "↩️ Quay lại",
                        callback_data="u_home"
                    ),
                    InlineKeyboardButton(
                        "❌ Hủy",
                        callback_data="u_cancel"
                    )
                ]
            ])
        )
        return

    if data in ("u_w_bank", "u_w_momo"):
        context.user_data["withdraw_method"] = (
            "bank"
            if data == "u_w_bank"
            else "momo"
        )

        context.user_data["state"] = "withdraw_amount"

        await query.edit_message_text(
            "💸 <b>RÚT TIỀN</b>\n\n"
            f"💵 Tối thiểu: <b>{format_money(MIN_WITHDRAW)}</b>\n"
            f"💵 Tối đa: <b>{format_money(MAX_WITHDRAW)}</b>\n\n"
            "⌨️ Nhập số tiền muốn rút:",
            parse_mode=ParseMode.HTML,
            reply_markup=back_cancel()
        )
        return

    if data == "u_withdraw_confirm":
        await create_withdrawal(
            update,
            context
        )
        return

    if data == "u_withdraw_back":
        context.user_data["state"] = "withdraw_amount"

        await query.edit_message_text(
            "💸 Nhập lại số tiền muốn rút:",
            reply_markup=back_cancel()
        )
        return

    await query.edit_message_text(
        "❌ Thao tác không hợp lệ.",
        reply_markup=user_menu()
    )


# =========================================================
# CREATE WITHDRAWAL
# =========================================================

async def create_withdrawal(update, context):
    query = update.callback_query
    user = query.from_user
    data = context.user_data

    # Chặn double-click / callback cũ sau khi yêu cầu đã được tạo.
    required = ("withdraw_amount", "withdraw_method")
    if any(k not in data for k in required):
        await query.edit_message_text("❌ Yêu cầu này đã được xử lý hoặc đã hết phiên.", reply_markup=user_menu())
        return

    amount = int(data["withdraw_amount"])
    method = data["withdraw_method"]
    if amount < MIN_WITHDRAW or amount > MAX_WITHDRAW:
        context.user_data.clear()
        await query.edit_message_text("❌ Số tiền rút không hợp lệ.", reply_markup=user_menu())
        return

    if method == "bank":
        bank = data.get("bank", "").strip()
        account_number = data.get("account_number", "").strip()
        account_name = data.get("account_name", "").strip()
        if not all((bank, account_number, account_name)):
            await query.edit_message_text("❌ Thiếu thông tin ngân hàng.", reply_markup=user_menu())
            return
    else:
        momo_phone = data.get("momo_phone", "").strip()
        account_name = data.get("account_name", "").strip()
        if not all((momo_phone, account_name)):
            await query.edit_message_text("❌ Thiếu thông tin ví.", reply_markup=user_menu())
            return

    request_code = generate_request_code("withdrawals")
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Trừ available + cộng locked trong cùng transaction với INSERT.
        cur = conn.execute("""
            UPDATE users
            SET balance=balance-?, locked_balance=locked_balance+?
            WHERE id=? AND balance>=? AND banned=0
        """, (amount, amount, user.id, amount))
        if cur.rowcount != 1:
            conn.rollback()
            await query.edit_message_text("❌ Không đủ số dư hoặc tài khoản đã bị khóa.", reply_markup=user_menu())
            return

        before = int(conn.execute("SELECT balance FROM users WHERE id=?", (user.id,)).fetchone()[0]) + amount
        after = before - amount

        if method == "bank":
            cur = conn.execute("""
                INSERT INTO withdrawals
                (request_code,user_id,method,amount,bank,account_number,account_name,status,created_at)
                VALUES (?,?,?,?,?,?,?,'pending',?)
            """, (request_code,user.id,method,amount,bank,account_number,account_name,now()))
        else:
            cur = conn.execute("""
                INSERT INTO withdrawals
                (request_code,user_id,method,amount,momo_phone,account_name,status,created_at)
                VALUES (?,?,?,?,?,?,'pending',?)
            """, (request_code,user.id,method,amount,momo_phone,account_name,now()))

        withdrawal_id = cur.lastrowid
        conn.execute("""
            INSERT INTO transactions
            (user_id,type,amount,balance_before,balance_after,note,created_at)
            VALUES (?,?,?,?,?,?,?)
        """, (user.id,"Tạo yêu cầu rút",-amount,before,after,f"Khóa tiền cho yêu cầu {request_code}",now()))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    context.user_data.clear()
    await query.edit_message_text(
        "✅ <b>ĐÃ TẠO YÊU CẦU RÚT</b>\n\n"
        f"🆔 Mã yêu cầu: <code>{request_code}</code>\n"
        f"💰 Số tiền: <b>{format_money(amount)}</b>\n"
        f"📌 Phương thức: <b>{'Ngân hàng' if method == 'bank' else 'MoMo'}</b>\n\n"
        "⏳ Tiền đã được khóa và đang chờ Admin xử lý.",
        parse_mode=ParseMode.HTML, reply_markup=user_menu())
    await notify_admins_withdrawal(context, withdrawal_id)


async def notify_admins_card(
    context,
    card_id,
    request_code,
    user_id,
    network,
    denomination,
    serial,
    card_code,
    discount,
    received
):
    text = (
        "🔔 <b>CÓ THẺ NẠP MỚI</b>\n\n"
        f"🆔 Mã yêu cầu: <code>{request_code}</code>\n"
        f"👤 User ID: <code>{user_id}</code>\n"
        f"🌐 Nhà mạng: <b>{NETWORKS[network]}</b>\n"
        f"💵 Mệnh giá: <b>{format_money(denomination)}</b>\n"
        f"📉 CK: <b>{discount:.2f}%</b>\n"
        f"💰 Thực nhận: <b>{format_money(received)}</b>\n"
        f"🔢 Serial: <code>{html.escape(serial)}</code>\n"
        f"🔐 Mã thẻ: <code>{html.escape(card_code)}</code>\n"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ DUYỆT",
                callback_data=f"a_card_ok_{card_id}"
            ),
            InlineKeyboardButton(
                "❌ TỪ CHỐI",
                callback_data=f"a_card_no_{card_id}"
            )
        ]
    ])

    for admin_id in ADMIN_IDS:
        try:
            await context.application.bot_data[
                "admin_bot"
            ].send_message(
                chat_id=admin_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard
            )
        except Exception as e:
            print("Notify admin card:", e)


async def notify_admins_withdrawal(
    context,
    withdrawal_id
):
    conn = db()

    row = conn.execute("""
        SELECT *
        FROM withdrawals
        WHERE id = ?
    """, (
        withdrawal_id,
    )).fetchone()

    conn.close()

    if not row:
        return

    if row["method"] == "bank":
        info = (
            f"🏦 Ngân hàng: <b>{html.escape(row['bank'])}</b>\n"
            f"🔢 STK: <code>{html.escape(row['account_number'])}</code>\n"
            f"👤 Chủ TK: <b>{html.escape(row['account_name'])}</b>\n"
        )
    else:
        info = (
            "📱 Phương thức: <b>MoMo</b>\n"
            f"📞 SĐT: <code>{html.escape(row['momo_phone'])}</code>\n"
            f"👤 Chủ TK: <b>{html.escape(row['account_name'])}</b>\n"
        )

    text = (
        "🔔 <b>CÓ YÊU CẦU RÚT MỚI</b>\n\n"
        f"🆔 Mã yêu cầu: <code>{row['request_code']}</code>\n"
        f"👤 User ID: <code>{row['user_id']}</code>\n"
        f"💰 Số tiền: <b>{format_money(row['amount'])}</b>\n"
        f"{info}\n"
        "⚠️ Kiểm tra kỹ trước khi duyệt."
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ DUYỆT",
                callback_data=f"a_w_ok_{withdrawal_id}"
            ),
            InlineKeyboardButton(
                "❌ TỪ CHỐI",
                callback_data=f"a_w_no_{withdrawal_id}"
            )
        ]
    ])

    for admin_id in ADMIN_IDS:
        try:
            await context.application.bot_data[
                "admin_bot"
            ].send_message(
                chat_id=admin_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard
            )
        except Exception as e:
            print("Notify admin withdrawal:", e)


# =========================================================
# ADMIN START
# =========================================================

async def admin_start(update, context):
    user = update.effective_user

    if not is_admin(user.id):
        await update.message.reply_text(
            "🚫 <b>BẠN KHÔNG CÓ QUYỀN</b>\n\n"
            "Bot này chỉ dành cho Admin.",
            parse_mode=ParseMode.HTML
        )
        return

    context.user_data.clear()

    await update.message.reply_text(
        "🛠 <b>ADMIN BOT</b>\n\n"
        f"👤 Admin: <b>{html.escape(user.full_name)}</b>\n"
        f"🆔 ID: <code>{user.id}</code>\n\n"
        + ADMIN_HELP,
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu()
    )


async def admin_help_command(update, context):
    if not is_admin(update.effective_user.id):
        return

    await update.message.reply_text(
        ADMIN_HELP,
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu()
    )


async def admin_cancel(update, context):
    if not is_admin(update.effective_user.id):
        return

    context.user_data.clear()

    await update.message.reply_text(
        "❌ Đã hủy thao tác.",
        reply_markup=admin_menu()
    )


# =========================================================
# ADMIN TEXT
# =========================================================

async def admin_text_handler(update, context):
    admin = update.effective_user

    if not is_admin(admin.id):
        await update.message.reply_text(
            "🚫 Không có quyền."
        )
        return

    state = context.user_data.get("state")
    text = update.message.text.strip()

    # -----------------------------------------------------
    # WITHDRAW REJECT REASON
    # -----------------------------------------------------

    if state == "reject_withdraw_reason":
        withdrawal_id = context.user_data.get(
            "withdrawal_id"
        )

        if not text:
            await update.message.reply_text(
                "❌ Lý do không được để trống.",
                reply_markup=admin_back()
            )
            return

        success = await reject_withdrawal(
            context,
            withdrawal_id,
            admin.id,
            text
        )

        context.user_data.clear()

        if success:
            await update.message.reply_text(
                "✅ Đã từ chối yêu cầu.\n"
                "💰 Tiền đã hoàn lại cho User.",
                reply_markup=admin_menu()
            )
        else:
            await update.message.reply_text(
                "❌ Yêu cầu không còn ở trạng thái chờ.",
                reply_markup=admin_menu()
            )

        return

    # -----------------------------------------------------
    # CK
    # -----------------------------------------------------

    if state == "discount":
        parts = text.split()

        if len(parts) != 3:
            await update.message.reply_text(
                "❌ Sai cú pháp.\n\n"
                "Ví dụ:\n"
                "<code>viettel 20000 12</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_back()
            )
            return

        network = parts[0].lower()

        try:
            denomination = int(parts[1])
            discount = float(parts[2])
        except ValueError:
            await update.message.reply_text(
                "❌ Dữ liệu không hợp lệ.",
                reply_markup=admin_back()
            )
            return

        if network not in NETWORKS:
            await update.message.reply_text(
                "❌ Nhà mạng không hợp lệ."
            )
            return

        if denomination not in DENOMINATIONS:
            await update.message.reply_text(
                "❌ Mệnh giá không hợp lệ."
            )
            return

        if discount < 0 or discount >= 100:
            await update.message.reply_text(
                "❌ Chiết khấu phải từ 0 đến dưới 100%."
            )
            return

        conn = db()

        conn.execute("""
            INSERT OR REPLACE INTO settings
            (network, denomination, discount)
            VALUES (?, ?, ?)
        """, (
            network,
            denomination,
            discount
        ))

        conn.commit()
        conn.close()

        context.user_data.clear()

        await update.message.reply_text(
            "✅ <b>ĐÃ CẬP NHẬT CHIẾT KHẤU</b>\n\n"
            f"🌐 {NETWORKS[network]}\n"
            f"💵 {format_money(denomination)}\n"
            f"📉 CK: <b>{discount:.2f}%</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_menu()
        )
        return

    await update.message.reply_text(
        "📌 Không có thao tác đang chờ.\n"
        "Dùng /start để mở Admin Panel."
    )


# =========================================================
# ADMIN CALLBACK
# =========================================================

async def admin_callback(update, context):
    query = update.callback_query

    await query.answer()

    admin = query.from_user

    if not is_admin(admin.id):
        await query.edit_message_text(
            "🚫 Không có quyền."
        )
        return

    data = query.data

    # -----------------------------------------------------
    # CANCEL
    # -----------------------------------------------------

    if data == "a_cancel":
        context.user_data.clear()

        await query.edit_message_text(
            "❌ Đã hủy thao tác.",
            reply_markup=admin_menu()
        )
        return

    # -----------------------------------------------------
    # HOME
    # -----------------------------------------------------

    if data == "a_home":
        context.user_data.clear()

        await query.edit_message_text(
            "🛠 <b>ADMIN PANEL</b>\n\n"
            "📌 Chọn chức năng:",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_menu()
        )
        return

    # -----------------------------------------------------
    # HELP
    # -----------------------------------------------------

    if data == "a_help":
        await query.edit_message_text(
            ADMIN_HELP,
            parse_mode=ParseMode.HTML,
            reply_markup=admin_back()
        )
        return

    # -----------------------------------------------------
    # CARDS
    # -----------------------------------------------------

    if data == "a_cards":
        await show_pending_cards(query)
        return

    # -----------------------------------------------------
    # WITHDRAWALS
    # -----------------------------------------------------

    if data == "a_withdrawals":
        await show_pending_withdrawals(query)
        return

    # -----------------------------------------------------
    # STATS
    # -----------------------------------------------------

    if data == "a_stats":
        conn = db()

        users = conn.execute(
            "SELECT COUNT(*) AS c FROM users"
        ).fetchone()["c"]

        balance = conn.execute(
            "SELECT COALESCE(SUM(balance),0) AS s FROM users"
        ).fetchone()["s"]

        locked = conn.execute(
            "SELECT COALESCE(SUM(locked_balance),0) AS s FROM users"
        ).fetchone()["s"]

        pending_cards = conn.execute(
            "SELECT COUNT(*) AS c FROM cards WHERE status='pending'"
        ).fetchone()["c"]

        pending_withdrawals = conn.execute(
            "SELECT COUNT(*) AS c FROM withdrawals WHERE status='pending'"
        ).fetchone()["c"]

        conn.close()

        await query.edit_message_text(
            "📊 <b>THỐNG KÊ</b>\n\n"
            f"👥 User: <b>{users}</b>\n"
            f"💰 Tổng số dư: <b>{format_money(balance)}</b>\n"
            f"🔒 Đang khóa: <b>{format_money(locked)}</b>\n"
            f"💳 Thẻ chờ duyệt: <b>{pending_cards}</b>\n"
            f"💸 Rút chờ duyệt: <b>{pending_withdrawals}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_back()
        )
        return

    # -----------------------------------------------------
    # USERS
    # -----------------------------------------------------

    if data == "a_users":
        conn = db()

        rows = conn.execute("""
            SELECT id, username, full_name, balance, banned
            FROM users
            ORDER BY id DESC
            LIMIT 20
        """).fetchall()

        conn.close()

        text = "👥 <b>DANH SÁCH USER</b>\n\n"

        if not rows:
            text += "Chưa có User."
        else:
            for row in rows:
                status = "🚫" if row["banned"] else "🟢"

                text += (
                    f"{status} <code>{row['id']}</code> "
                    f"{html.escape(row['full_name'][:20])}\n"
                    f"   💰 {format_money(row['balance'])}\n\n"
                )

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=admin_back()
        )
        return

    # -----------------------------------------------------
    # DISCOUNT
    # -----------------------------------------------------

    if data == "a_discount":
        context.user_data["state"] = "discount"

        await query.edit_message_text(
            "⚙️ <b>CÀI ĐẶT CHIẾT KHẤU</b>\n\n"
            "Nhập theo mẫu:\n"
            "<code>nhamang menhgia chietkhau</code>\n\n"
            "Ví dụ:\n"
            "<code>viettel 20000 12</code>\n\n"
            "Các mạng:\n"
            "• viettel\n"
            "• mobifone\n"
            "• vinaphone",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_back()
        )
        return

    # -----------------------------------------------------
    # CARD APPROVE
    # -----------------------------------------------------

    if data.startswith("a_card_ok_"):
        card_id = int(
            data.replace(
                "a_card_ok_",
                ""
            )
        )

        success = await approve_card(
            context,
            card_id,
            admin.id
        )

        await query.edit_message_reply_markup(
            reply_markup=None
        )

        if success:
            await query.message.reply_text(
                "✅ Đã xử lý duyệt thẻ."
            )
        else:
            await query.message.reply_text(
                "❌ Thẻ không còn ở trạng thái chờ."
            )

        return

    # -----------------------------------------------------
    # CARD REJECT
    # -----------------------------------------------------

    if data.startswith("a_card_no_"):
        card_id = int(
            data.replace(
                "a_card_no_",
                ""
            )
        )

        success = await reject_card(
            context,
            card_id,
            admin.id
        )

        await query.edit_message_reply_markup(
            reply_markup=None
        )

        if success:
            await query.message.reply_text(
                "❌ Đã từ chối thẻ."
            )
        else:
            await query.message.reply_text(
                "❌ Thẻ không còn ở trạng thái chờ."
            )

        return

    # -----------------------------------------------------
    # WITHDRAW APPROVE
    # -----------------------------------------------------

    if data.startswith("a_w_ok_"):
        withdrawal_id = int(
            data.replace(
                "a_w_ok_",
                ""
            )
        )

        success = await approve_withdrawal(
            context,
            withdrawal_id,
            admin.id
        )

        await query.edit_message_reply_markup(
            reply_markup=None
        )

        if success:
            await query.message.reply_text(
                "✅ Đã duyệt yêu cầu rút.\n\n"
                "⚠️ Hệ thống chỉ ghi nhận trạng thái Admin đã duyệt; "
                "không tự động chuyển tiền thật."
            )
        else:
            await query.message.reply_text(
                "❌ Yêu cầu không còn ở trạng thái chờ."
            )

        return

    # -----------------------------------------------------
    # WITHDRAW REJECT
    # -----------------------------------------------------

    if data.startswith("a_w_no_"):
        withdrawal_id = int(
            data.replace(
                "a_w_no_",
                ""
            )
        )

        conn = db()
        row = conn.execute("""
            SELECT request_code, status
            FROM withdrawals
            WHERE id = ?
        """, (withdrawal_id,)).fetchone()
        conn.close()

        if not row or row["status"] != "pending":
            await query.edit_message_text("❌ Yêu cầu không còn ở trạng thái chờ.", reply_markup=admin_back())
            return

        context.user_data["state"] = "reject_withdraw_reason"
        context.user_data["withdrawal_id"] = withdrawal_id

        request_code = (
            row["request_code"]
            if row and row["request_code"]
            else str(withdrawal_id)
        )

        await query.edit_message_text(
            f"❌ <b>TỪ CHỐI YÊU CẦU {request_code}</b>\n\n"
            "⌨️ Nhập <b>lý do từ chối</b>:\n\n"
            "Ví dụ:\n"
            "Thông tin tài khoản nhận tiền không chính xác.",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_back()
        )
        return


# =========================================================
# SHOW PENDING CARDS
# =========================================================

async def show_pending_cards(query):
    conn = db()

    rows = conn.execute("""
        SELECT *
        FROM cards
        WHERE status = 'pending'
        ORDER BY id ASC
        LIMIT 20
    """).fetchall()

    conn.close()

    if not rows:
        await query.edit_message_text(
            "💳 <b>DUYỆT THẺ</b>\n\n"
            "✅ Không có thẻ đang chờ.",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_back()
        )
        return

    keyboard = []

    for row in rows:
        request_code = (
            row["request_code"]
            if row["request_code"]
            else str(row["id"])
        )

        keyboard.append([
            InlineKeyboardButton(
                f"💳 {request_code} • "
                f"{format_money(row['denomination'])}",
                callback_data=f"a_card_view_{row['id']}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "↩️ Quay lại",
            callback_data="a_home"
        )
    ])

    await query.edit_message_text(
        "💳 <b>THẺ ĐANG CHỜ DUYỆT</b>\n\n"
        "Chọn yêu cầu:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================================================
# SHOW PENDING WITHDRAWALS
# =========================================================

async def show_pending_withdrawals(query):
    conn = db()

    rows = conn.execute("""
        SELECT *
        FROM withdrawals
        WHERE status = 'pending'
        ORDER BY id ASC
        LIMIT 20
    """).fetchall()

    conn.close()

    if not rows:
        await query.edit_message_text(
            "💸 <b>DUYỆT RÚT</b>\n\n"
            "✅ Không có yêu cầu đang chờ.",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_back()
        )
        return

    keyboard = []

    for row in rows:
        method = (
            "🏦"
            if row["method"] == "bank"
            else "📱"
        )

        request_code = (
            row["request_code"]
            if row["request_code"]
            else str(row["id"])
        )

        keyboard.append([
            InlineKeyboardButton(
                f"{method} {request_code} • "
                f"{format_money(row['amount'])}",
                callback_data=f"a_w_view_{row['id']}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "↩️ Quay lại",
            callback_data="a_home"
        )
    ])

    await query.edit_message_text(
        "💸 <b>YÊU CẦU RÚT ĐANG CHỜ</b>\n\n"
        "Chọn yêu cầu:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================================================
# CARD VIEW / WITHDRAW VIEW
# =========================================================

async def admin_view_handler(update, context):
    query = update.callback_query

    await query.answer()

    if not is_admin(query.from_user.id):
        return

    data = query.data

    # -----------------------------------------------------
    # CARD VIEW
    # -----------------------------------------------------

    if data.startswith("a_card_view_"):
        card_id = int(
            data.replace(
                "a_card_view_",
                ""
            )
        )

        conn = db()

        row = conn.execute("""
            SELECT *
            FROM cards
            WHERE id = ?
        """, (
            card_id,
        )).fetchone()

        conn.close()

        if not row:
            await query.edit_message_text(
                "❌ Không tìm thấy thẻ.",
                reply_markup=admin_back()
            )
            return

        request_code = (
            row["request_code"]
            if row["request_code"]
            else str(row["id"])
        )

        text = (
            "💳 <b>CHI TIẾT THẺ</b>\n\n"
            f"🆔 Mã yêu cầu: <code>{request_code}</code>\n"
            f"👤 User: <code>{row['user_id']}</code>\n"
            f"🌐 {NETWORKS.get(row['network'], row['network'])}\n"
            f"💵 {format_money(row['denomination'])}\n"
            f"📉 CK: {row['discount']:.2f}%\n"
            f"💰 Nhận: {format_money(row['received'])}\n"
            f"🔢 Serial: <code>{html.escape(row['serial'])}</code>\n"
            f"🔐 Mã: <code>{html.escape(row['card_code'])}</code>\n"
            f"📌 Trạng thái: <b>{row['status']}</b>"
        )

        if row["status"] == "pending":
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ DUYỆT",
                        callback_data=f"a_card_ok_{card_id}"
                    ),
                    InlineKeyboardButton(
                        "❌ TỪ CHỐI",
                        callback_data=f"a_card_no_{card_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "↩️ Danh sách",
                        callback_data="a_cards"
                    )
                ]
            ])
        else:
            keyboard = admin_back()

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )
        return

    # -----------------------------------------------------
    # WITHDRAW VIEW
    # -----------------------------------------------------

    if data.startswith("a_w_view_"):
        withdrawal_id = int(
            data.replace(
                "a_w_view_",
                ""
            )
        )

        conn = db()

        row = conn.execute("""
            SELECT *
            FROM withdrawals
            WHERE id = ?
        """, (
            withdrawal_id,
        )).fetchone()

        conn.close()

        if not row:
            await query.edit_message_text(
                "❌ Không tìm thấy yêu cầu.",
                reply_markup=admin_back()
            )
            return

        request_code = (
            row["request_code"]
            if row["request_code"]
            else str(row["id"])
        )

        if row["method"] == "bank":
            info = (
                f"🏦 Ngân hàng: <b>{html.escape(row['bank'])}</b>\n"
                f"🔢 STK: <code>{html.escape(row['account_number'])}</code>\n"
                f"👤 Chủ TK: <b>{html.escape(row['account_name'])}</b>"
            )
        else:
            info = (
                "📱 Phương thức: <b>MoMo</b>\n"
                f"📞 SĐT: <code>{html.escape(row['momo_phone'])}</code>\n"
                f"👤 Chủ TK: <b>{html.escape(row['account_name'])}</b>"
            )

        text = (
            "💸 <b>CHI TIẾT YÊU CẦU RÚT</b>\n\n"
            f"🆔 Mã yêu cầu: <code>{request_code}</code>\n"
            f"👤 User: <code>{row['user_id']}</code>\n"
            f"💰 Số tiền: <b>{format_money(row['amount'])}</b>\n"
            f"{info}\n"
            f"📌 Trạng thái: <b>{row['status']}</b>\n"
        )

        if row["status"] == "rejected":
            text += (
                f"\n❌ Lý do: "
                f"<b>{html.escape(row['reject_reason'])}</b>"
            )

        if row["status"] == "pending":
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ DUYỆT",
                        callback_data=f"a_w_ok_{withdrawal_id}"
                    ),
                    InlineKeyboardButton(
                        "❌ TỪ CHỐI",
                        callback_data=f"a_w_no_{withdrawal_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "↩️ Danh sách",
                        callback_data="a_withdrawals"
                    )
                ]
            ])
        else:
            keyboard = admin_back()

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )


# =========================================================
# APPROVE CARD
# =========================================================

async def approve_card(context, card_id, admin_id):
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM cards WHERE id = ? AND status = 'pending'", (card_id,)).fetchone()
        if not row:
            conn.rollback()
            return False

        user = conn.execute("SELECT * FROM users WHERE id = ?", (row["user_id"],)).fetchone()
        if not user:
            conn.rollback()
            return False

        request_code = row["request_code"] or str(row["id"])
        before = int(user["balance"])
        after = before + int(row["received"])

        # Điều kiện status=pending nằm ngay trong UPDATE: 2 admin bấm cùng lúc
        # chỉ một giao dịch có thể chuyển pending -> approved.
        cur = conn.execute("""
            UPDATE cards
            SET status='approved', decided_at=?, decided_by=?
            WHERE id=? AND status='pending'
        """, (now(), admin_id, card_id))
        if cur.rowcount != 1:
            conn.rollback()
            return False

        conn.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (row["received"], row["user_id"]))
        conn.execute("""
            INSERT INTO transactions
            (user_id, type, amount, balance_before, balance_after, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (row["user_id"], "Nạp thẻ", row["received"], before, after, f"Thẻ {request_code}", now()))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    try:
        await context.application.bot_data["user_bot"].send_message(
            chat_id=row["user_id"],
            text=(
                "🎉 <b>NẠP THẺ THÀNH CÔNG</b>\n\n"
                f"🆔 Mã yêu cầu: <code>{request_code}</code>\n"
                f"🌐 Nhà mạng: <b>{NETWORKS.get(row['network'], row['network'])}</b>\n"
                f"💵 Mệnh giá: <b>{format_money(row['denomination'])}</b>\n"
                f"📉 Chiết khấu: <b>{row['discount']:.2f}%</b>\n"
                f"💸 Phí: <b>{format_money(row['denomination'] - row['received'])}</b>\n"
                f"💰 Thực nhận: <b>{format_money(row['received'])}</b>\n"
                f"💎 Số dư mới: <b>{format_money(after)}</b>"
            ), parse_mode=ParseMode.HTML)
    except Exception as e:
        print("Notify user card approve:", e)
    return True


async def reject_card(context, card_id, admin_id):
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM cards WHERE id = ? AND status = 'pending'", (card_id,)).fetchone()
        if not row:
            conn.rollback()
            return False
        request_code = row["request_code"] or str(row["id"])
        cur = conn.execute("""
            UPDATE cards
            SET status='rejected', decided_at=?, decided_by=?
            WHERE id=? AND status='pending'
        """, (now(), admin_id, card_id))
        if cur.rowcount != 1:
            conn.rollback()
            return False
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    try:
        await context.application.bot_data["user_bot"].send_message(
            chat_id=row["user_id"],
            text=(
                "❌ <b>THẺ BỊ TỪ CHỐI</b>\n\n"
                f"🆔 Mã yêu cầu: <code>{request_code}</code>\n"
                f"🌐 Nhà mạng: <b>{NETWORKS.get(row['network'], row['network'])}</b>\n"
                f"💵 Mệnh giá: <b>{format_money(row['denomination'])}</b>\n\n"
                "⚠️ Thẻ chưa được cộng tiền."
            ), parse_mode=ParseMode.HTML)
    except Exception as e:
        print("Notify user card reject:", e)
    return True


async def approve_withdrawal(context, withdrawal_id, admin_id):
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM withdrawals WHERE id=? AND status='pending'", (withdrawal_id,)).fetchone()
        if not row:
            conn.rollback()
            return False

        # Chỉ một admin được phép tiêu thụ locked_balance của yêu cầu này.
        cur = conn.execute("""
            UPDATE withdrawals
            SET status='approved', decided_at=?, decided_by=?
            WHERE id=? AND status='pending'
        """, (now(), admin_id, withdrawal_id))
        if cur.rowcount != 1:
            conn.rollback()
            return False

        cur = conn.execute("""
            UPDATE users
            SET locked_balance = locked_balance - ?
            WHERE id=? AND locked_balance >= ?
        """, (row["amount"], row["user_id"], row["amount"]))
        if cur.rowcount != 1:
            conn.rollback()
            return False

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    request_code = row["request_code"] or str(row["id"])
    try:
        await context.application.bot_data["user_bot"].send_message(
            chat_id=row["user_id"],
            text=(
                "✅ <b>YÊU CẦU RÚT ĐÃ ĐƯỢC DUYỆT</b>\n\n"
                f"🆔 Mã yêu cầu: <code>{request_code}</code>\n"
                f"💰 Số tiền: <b>{format_money(row['amount'])}</b>\n\n"
                "⚠️ Admin đã duyệt yêu cầu. Hệ thống không tự chuyển tiền thật."
            ), parse_mode=ParseMode.HTML)
    except Exception as e:
        print("Notify user withdraw approve:", e)
    return True


async def reject_withdrawal(context, withdrawal_id, admin_id, reason):
    reason = (reason or "").strip()
    if not reason:
        return False

    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM withdrawals WHERE id=? AND status='pending'", (withdrawal_id,)).fetchone()
        if not row:
            conn.rollback()
            return False

        user = conn.execute("SELECT balance, locked_balance FROM users WHERE id=?", (row["user_id"],)).fetchone()
        if not user or int(user["locked_balance"]) < int(row["amount"]):
            conn.rollback()
            return False

        # Đổi trạng thái và hoàn khóa tiền trong cùng transaction.
        cur = conn.execute("""
            UPDATE withdrawals
            SET status='rejected', reject_reason=?, decided_at=?, decided_by=?
            WHERE id=? AND status='pending'
        """, (reason, now(), admin_id, withdrawal_id))
        if cur.rowcount != 1:
            conn.rollback()
            return False

        before = int(user["balance"])
        after = before + int(row["amount"])
        cur = conn.execute("""
            UPDATE users
            SET balance=balance+?, locked_balance=locked_balance-?
            WHERE id=? AND locked_balance >= ?
        """, (row["amount"], row["amount"], row["user_id"], row["amount"]))
        if cur.rowcount != 1:
            conn.rollback()
            return False

        conn.execute("""
            INSERT INTO transactions
            (user_id, type, amount, balance_before, balance_after, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (row["user_id"], "Hoàn tiền rút", row["amount"], before, after, f"Hoàn yêu cầu rút {row['request_code'] or row['id']}", now()))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    request_code = row["request_code"] or str(row["id"])
    try:
        await context.application.bot_data["user_bot"].send_message(
            chat_id=row["user_id"],
            text=(
                "❌ <b>YÊU CẦU RÚT BỊ TỪ CHỐI</b>\n\n"
                f"🆔 Mã yêu cầu: <code>{request_code}</code>\n"
                f"💰 Số tiền hoàn: <b>{format_money(row['amount'])}</b>\n"
                f"📝 Lý do: <b>{html.escape(reason)}</b>\n\n"
                f"💎 Số dư hiện tại: <b>{format_money(after)}</b>"
            ), parse_mode=ParseMode.HTML)
    except Exception as e:
        print("Notify user withdraw reject:", e)
    return True


async def cmd_ban(update, context):
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "Dùng: /ban ID"
        )
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "❌ ID không hợp lệ."
        )
        return

    conn = db()

    conn.execute("""
        UPDATE users
        SET banned = 1
        WHERE id = ?
    """, (
        user_id,
    ))

    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"🚫 Đã khóa User <code>{user_id}</code>.",
        parse_mode=ParseMode.HTML
    )


async def cmd_unban(update, context):
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "Dùng: /unban ID"
        )
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "❌ ID không hợp lệ."
        )
        return

    conn = db()

    conn.execute("""
        UPDATE users
        SET banned = 0
        WHERE id = ?
    """, (
        user_id,
    ))

    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ Đã mở khóa User <code>{user_id}</code>.",
        parse_mode=ParseMode.HTML
    )


async def cmd_tien(update, context):
    if not is_admin(update.effective_user.id):
        return

    if len(context.args) != 3:
        await update.message.reply_text(
            "Dùng:\n"
            "/tien ID + 10000\n"
            "/tien ID - 10000"
        )
        return

    try:
        user_id = int(context.args[0])
        operator = context.args[1]
        amount = int(
            context.args[2]
            .replace(".", "")
            .replace(",", "")
        )
    except ValueError:
        await update.message.reply_text(
            "❌ Dữ liệu không hợp lệ."
        )
        return

    if operator not in ("+", "-"):
        await update.message.reply_text(
            "❌ Chỉ dùng + hoặc -."
        )
        return

    if amount <= 0:
        await update.message.reply_text(
            "❌ Số tiền phải > 0."
        )
        return

    conn = db()

    user = conn.execute("""
        SELECT *
        FROM users
        WHERE id = ?
    """, (
        user_id,
    )).fetchone()

    if not user:
        conn.close()

        await update.message.reply_text(
            "❌ Không tìm thấy User."
        )
        return

    before = user["balance"]

    if operator == "+":
        after = before + amount
        real_amount = amount
    else:
        if before < amount:
            conn.close()

            await update.message.reply_text(
                "❌ User không đủ số dư."
            )
            return

        after = before - amount
        real_amount = -amount

    conn.execute("""
        UPDATE users
        SET balance = ?
        WHERE id = ?
    """, (
        after,
        user_id
    ))

    conn.execute("""
        INSERT INTO transactions
        (
            user_id,
            type,
            amount,
            balance_before,
            balance_after,
            note,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        "Admin điều chỉnh",
        real_amount,
        before,
        after,
        "Admin điều chỉnh số dư",
        now()
    ))

    conn.commit()
    conn.close()

    await update.message.reply_text(
        "✅ <b>ĐÃ ĐIỀU CHỈNH</b>\n\n"
        f"👤 User: <code>{user_id}</code>\n"
        f"💰 Trước: <b>{format_money(before)}</b>\n"
        f"💵 Thay đổi: <b>{'+' if real_amount > 0 else ''}{format_money(real_amount)}</b>\n"
        f"💎 Sau: <b>{format_money(after)}</b>",
        parse_mode=ParseMode.HTML
    )

    try:
        await context.application.bot_data[
            "user_bot"
        ].send_message(
            chat_id=user_id,
            text=(
                "🔔 <b>SỐ DƯ ĐƯỢC ADMIN ĐIỀU CHỈNH</b>\n\n"
                f"💵 Thay đổi: <b>{'+' if real_amount > 0 else ''}{format_money(real_amount)}</b>\n"
                f"💰 Số dư mới: <b>{format_money(after)}</b>"
            ),
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass


async def cmd_ck(update, context):
    if not is_admin(update.effective_user.id):
        return

    if len(context.args) != 3:
        await update.message.reply_text(
            "Dùng:\n"
            "/ck viettel 20000 12"
        )
        return

    network = context.args[0].lower()

    try:
        denomination = int(context.args[1])
        discount = float(context.args[2])
    except ValueError:
        await update.message.reply_text(
            "❌ Dữ liệu không hợp lệ."
        )
        return

    if network not in NETWORKS:
        await update.message.reply_text(
            "❌ Nhà mạng không hợp lệ."
        )
        return

    if denomination not in DENOMINATIONS:
        await update.message.reply_text(
            "❌ Mệnh giá không hợp lệ."
        )
        return

    if not 0 <= discount < 100:
        await update.message.reply_text(
            "❌ Chiết khấu không hợp lệ."
        )
        return

    conn = db()

    conn.execute("""
        INSERT OR REPLACE INTO settings
        (network, denomination, discount)
        VALUES (?, ?, ?)
    """, (
        network,
        denomination,
        discount
    ))

    conn.commit()
    conn.close()

    await update.message.reply_text(
        "✅ <b>ĐÃ ĐỔI CHIẾT KHẤU</b>\n\n"
        f"🌐 {NETWORKS[network]}\n"
        f"💵 {format_money(denomination)}\n"
        f"📉 CK: <b>{discount:.2f}%</b>",
        parse_mode=ParseMode.HTML
    )


# =========================================================
# BUILD APPS
# =========================================================

def build_user_app():
    app = (
        Application
        .builder()
        .token(USER_BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            user_start
        )
    )

    app.add_handler(
        CommandHandler(
            "help",
            user_help_command
        )
    )

    app.add_handler(
        CommandHandler(
            "cancel",
            user_cancel_command
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            user_callback,
            pattern=r"^u_"
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            user_text_handler
        )
    )

    return app


def build_admin_app():
    app = (
        Application
        .builder()
        .token(ADMIN_BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            admin_start
        )
    )

    app.add_handler(
        CommandHandler(
            "help",
            admin_help_command
        )
    )

    app.add_handler(
        CommandHandler(
            "cancel",
            admin_cancel
        )
    )

    app.add_handler(
        CommandHandler(
            "ban",
            cmd_ban
        )
    )

    app.add_handler(
        CommandHandler(
            "unban",
            cmd_unban
        )
    )

    app.add_handler(
        CommandHandler(
            "tien",
            cmd_tien
        )
    )

    app.add_handler(
        CommandHandler(
            "ck",
            cmd_ck
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            admin_view_handler,
            pattern=r"^a_(card_view_|w_view_)"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^a_"
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            admin_text_handler
        )
    )

    return app


# =========================================================
# MAIN - RUN 2 BOTS
# =========================================================

async def main():
    if not USER_BOT_TOKEN or not ADMIN_BOT_TOKEN:
        raise RuntimeError(
            "Thiếu USER_BOT_TOKEN hoặc ADMIN_BOT_TOKEN. Hãy đặt biến môi trường trước khi chạy bot."
        )
    init_db()

    if (
        not USER_BOT_TOKEN
        or USER_BOT_TOKEN.startswith("DAN_TOKEN")
    ):
        print("❌ Chưa nhập USER_BOT_TOKEN")
        return

    if (
        not ADMIN_BOT_TOKEN
        or ADMIN_BOT_TOKEN.startswith("DAN_TOKEN")
    ):
        print("❌ Chưa nhập ADMIN_BOT_TOKEN")
        return

    if not ADMIN_IDS or 123456789 in ADMIN_IDS:
        print(
            "⚠️ Hãy đổi ADMIN_IDS "
            "thành ID Telegram thật."
        )

    user_app = build_user_app()
    admin_app = build_admin_app()

    # Liên kết 2 bot
    user_app.bot_data["admin_bot"] = admin_app.bot
    admin_app.bot_data["user_bot"] = user_app.bot

    await user_app.initialize()
    await admin_app.initialize()

    await user_app.start()
    await admin_app.start()

    await user_app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES
    )

    await admin_app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES
    )

    print("===================================")
    print("✅ USER BOT: ONLINE")
    print("✅ ADMIN BOT: ONLINE")
    print("✅ DATABASE: cardbot.db")
    print("===================================")

    try:
        await asyncio.Event().wait()

    finally:
        await user_app.updater.stop()
        await admin_app.updater.stop()

        await user_app.stop()
        await admin_app.stop()

        await user_app.shutdown()
        await admin_app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())