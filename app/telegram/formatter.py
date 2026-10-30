# app/telegram/formatter.py
# Chuẩn bị và gửi câu trả lời của agent lên Telegram:
#   - cắt tin nhắn dài (Telegram giới hạn 4096 ký tự) ở ranh giới đoạn/dòng,
#   - đổi Markdown cơ bản của Claude (**đậm**, `code`, # tiêu đề, - gạch đầu dòng,
#     bảng |...|) sang HTML mà Telegram hiểu, đã escape an toàn,
#   - gửi chữ / file / ảnh; nếu Telegram từ chối HTML thì gửi lại dạng chữ thường.
# Hàm split_message và to_telegram_html là hàm thuần, dễ test (tests/test_formatter.py).

import html
import logging
import re

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, Message

logger = logging.getLogger(__name__)

# Telegram cho tối đa 4096 ký tự. Chừa lề vì đổi sang HTML làm chuỗi dài thêm
# (&lt; &amp; <b>...</b>).
MAX_LEN = 3500
CAPTION_MAX = 1024

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`\n]+)`")
_HEADER = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$")
_BULLET = re.compile(r"^(\s*)[-*]\s+")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?[\s:\-|]+\|?\s*$")


def split_message(text: str, limit: int = MAX_LEN) -> list[str]:
    """Cắt văn bản thành các phần <= limit ký tự, ưu tiên cắt ở dòng trống,
    rồi đến ngắt dòng, cuối cùng mới cắt cứng."""
    text = text.strip()
    if not text:
        return []

    # Tách thành các "đơn vị" kèm dấu phân cách đứng trước nó.
    units: list[tuple[str, str]] = []
    for para in text.split("\n\n"):
        if len(para) <= limit:
            units.append(("\n\n", para))
            continue
        for i, line in enumerate(para.split("\n")):
            sep = "\n\n" if i == 0 else "\n"
            while len(line) > limit:
                units.append((sep, line[:limit]))
                line, sep = line[limit:], "\n"
            units.append((sep, line))

    chunks: list[str] = []
    current = ""
    for sep, unit in units:
        if current and len(current) + len(sep) + len(unit) <= limit:
            current += sep + unit
        else:
            if current:
                chunks.append(current)
            current = unit
    if current:
        chunks.append(current)
    return chunks


def _inline(line: str) -> str:
    """Escape một dòng rồi đổi **đậm**, `code`, gạch đầu dòng sang HTML."""
    header = _HEADER.match(line)
    if header:
        return f"<b>{_inline(header.group(1))}</b>"
    escaped = html.escape(line, quote=False)
    escaped = _BULLET.sub(r"\1• ", escaped)
    escaped = _BOLD.sub(r"<b>\1</b>", escaped)
    return _CODE.sub(r"<code>\1</code>", escaped)


def _render_table(rows: list[str]) -> str:
    """Bảng Markdown -> khối <pre> có căn cột, đọc được trên điện thoại."""
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    width = max(len(r) for r in cells)
    cells = [r + [""] * (width - len(r)) for r in cells]
    widths = [max(len(r[i]) for r in cells) for i in range(width)]
    lines = [" | ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in cells]
    return "<pre>" + html.escape("\n".join(lines), quote=False) + "</pre>"


def to_telegram_html(text: str) -> str:
    """Đổi Markdown cơ bản của Claude sang HTML Telegram (parse_mode=HTML)."""
    out: list[str] = []
    table: list[str] = []

    def flush_table() -> None:
        if table:
            out.append(_render_table(table))
            table.clear()

    for line in text.split("\n"):
        if line.lstrip().startswith("|"):
            if "-" in line and _TABLE_SEPARATOR.match(line):
                continue  # dòng ngăn |---|---|
            table.append(line)
            continue
        flush_table()
        out.append(_inline(line))
    flush_table()
    return "\n".join(out)


async def send_text(message: Message, text: str) -> None:
    """Gửi câu trả lời, tự cắt nhỏ. Lỗi HTML thì gửi lại dạng chữ thường."""
    for chunk in split_message(text):
        try:
            await message.answer(to_telegram_html(chunk), parse_mode=ParseMode.HTML)
        except TelegramBadRequest as e:
            logger.warning("Telegram từ chối HTML, gửi chữ thường: %s", e)
            await message.answer(chunk, parse_mode=None)


async def send_document(
    message: Message, data: bytes, filename: str, caption: str | None = None
) -> None:
    """Gửi file (ví dụ dashboard HTML) kèm chú thích ngắn."""
    await message.answer_document(
        BufferedInputFile(data, filename=filename),
        caption=(caption or None) and caption[:CAPTION_MAX],
        parse_mode=None,
    )


async def send_photo(
    message: Message, data: bytes, filename: str = "chart.png", caption: str | None = None
) -> None:
    """Gửi ảnh biểu đồ (PNG) kèm chú thích ngắn."""
    await message.answer_photo(
        BufferedInputFile(data, filename=filename),
        caption=(caption or None) and caption[:CAPTION_MAX],
        parse_mode=None,
    )