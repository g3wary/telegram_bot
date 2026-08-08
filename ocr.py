"""
OCR-проверка скриншота оплаты.

Логика:
1. Скачиваем фото, прогоняем через Tesseract.
2. Ищем в тексте:
   • имя получателя ('Максим', 'Максим Я.', 'Сергеевич' и т.п.)
   • сумму перевода (сравниваем с ожидаемой)
3. Возвращаем dict {name_correct, amount_correct, detected_name, detected_amount, raw_text}
"""
import re
import io
import logging
import asyncio
from PIL import Image
import pytesseract

from config import SBER_RECIPIENT

log = logging.getLogger(__name__)

# Ключевые части имени получателя — бот должен найти ХОТЯ БЫ ОДНУ
RECIPIENT_KEYWORDS = ["максим", "сергеевич"]

# Регексп для суммы: 250, 250.00, 1 250, 1,250 ₽
AMOUNT_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[ \u00a0]?\d{3})*(?:[.,]\d{1,2})?|\d+)(?:\s?₽|\s?руб|RUB|р\.)?",
    re.IGNORECASE,
)


def _normalize_amount(s: str) -> int | None:
    """'1 250,50' -> 1250"""
    s = s.replace("\u00a0", " ").replace(" ", "").replace(",", ".")
    s = re.sub(r"[^\d.]", "", s)
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _ocr_sync(image_bytes: bytes) -> str:
    """Tesseract CPU-bound — держим в executor."""
    img = Image.open(io.BytesIO(image_bytes))
    # Превращаем в ЧБ и увеличиваем — резко поднимает точность на скринах
    img = img.convert("L")
    img = img.resize((img.width * 2, img.height * 2))
    return pytesseract.image_to_string(img, lang="rus+eng")


async def recognize_screenshot(image_bytes: bytes) -> dict:
    """Главная функция — возвращает dict с результатами проверки."""
    try:
        text = await asyncio.to_thread(_ocr_sync, image_bytes)
    except Exception as e:
        log.exception("OCR error")
        return {"ok": False, "error": str(e)}

    text_l = text.lower()

    # 1) Проверяем имя
    name_correct = any(kw in text_l for kw in RECIPIENT_KEYWORDS)

    # 2) Ищем все суммы и берём максимальную (на скрине обычно самая крупная = итог)
    candidates = []
    for m in AMOUNT_RE.finditer(text):
        amt = _normalize_amount(m.group(1))
        if amt and 10 <= amt <= 1_000_000:  # отсекаем мусор
            candidates.append(amt)

    detected_amount = max(candidates) if candidates else None

    return {
        "ok": True,
        "name_correct": name_correct,
        "detected_name": "обнаружено" if name_correct else "не найдено",
        "detected_amount": detected_amount,
        "raw_text": text[:400],  # первые 400 символов — для логов
    }


def check_payment(ocr_result: dict, expected_price: int) -> dict:
    """Сверяет результат OCR с ожидаемой ценой. Возвращает вердикт."""
    if not ocr_result.get("ok"):
        return {"valid": False, "reason": "ocr_error", "details": ocr_result.get("error")}

    if not ocr_result["name_correct"]:
        return {
            "valid": False,
            "reason": "wrong_name",
            "detected": ocr_result["detected_name"],
        }

    detected = ocr_result["detected_amount"]
    if detected is None:
        return {"valid": False, "reason": "no_amount", "detected": None}

    if detected != expected_price:
        return {
            "valid": False,
            "reason": "wrong_amount",
            "detected": detected,
            "expected": expected_price,
        }

    return {"valid": True, "detected_amount":
