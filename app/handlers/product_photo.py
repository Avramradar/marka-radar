import logging
import time
from dataclasses import dataclass
from html import escape

from aiogram import F, Router
from aiogram.filters import BaseFilter
from aiogram.types import Message

from app.database.repositories.product_repository import (
    search_products,
)
from app.database.session import async_session_maker


router = Router()
logger = logging.getLogger(__name__)


PENDING_PHOTO_TTL_SECONDS = 10 * 60


@dataclass(slots=True)
class PendingProductPhoto:
    file_id: str
    created_at: float


_pending_photos: dict[
    tuple[int, int],
    PendingProductPhoto,
] = {}


def _user_key( message: Message, ) -> tuple[int, int] | None:
    """ Возвращает ключ состояния: (chat_id, user_id). """

    if message.from_user is None:
        return None

    return (
        message.chat.id,
        message.from_user.id,
    )


def _cleanup_expired() -> None:
    """ Удаляет незавершённые фото старше TTL. """

    now = time.monotonic()

    expired = [
        key
        for key, value in _pending_photos.items()
        if (
            now - value.created_at
            > PENDING_PHOTO_TTL_SECONDS
        )
    ]

    for key in expired:
        _pending_photos.pop(
            key,
            None,
        )


def _get_pending_photo( message: Message, ) -> PendingProductPhoto | None:
    _cleanup_expired()

    key = _user_key(
        message
    )

    if key is None:
        return None

    return _pending_photos.get(
        key
    )


def _set_pending_photo( *, message: Message, file_id: str, ) -> None:
    _cleanup_expired()

    key = _user_key(
        message
    )

    if key is None:
        return

    _pending_photos[key] = (
        PendingProductPhoto(
            file_id=file_id,
            created_at=time.monotonic(),
        )
    )


def _clear_pending_photo( message: Message, ) -> None:
    key = _user_key(
        message
    )

    if key is None:
        return

    _pending_photos.pop(
        key,
        None,
    )


class HasPendingProductPhoto(
    BaseFilter
):
    """ Пропускает текстовый обработчик только тогда, когда пользователь перед этим отправил фото без подписи. Благодаря фильтру обычный текст по-прежнему попадёт в app.handlers.search. """

    async def __call__( self, message: Message, ) -> bool:
        return (
            _get_pending_photo(
                message
            )
            is not None
        )


def normalize_text( value: str | None, ) -> str:
    return " ".join(
        str(value or "")
        .strip()
        .lower()
        .replace(
            "ё",
            "е",
        )
        .split()
    )


def build_candidate_text( *, product, brand, ) -> str:
    return normalize_text(
        f"{getattr(brand, 'name', '')} "
        f"{getattr(product, 'name', '')}"
    )


def calculate_match_score( *, query: str, candidate: str, ) -> float:
    """ Оценивает совпадение подписи с найденной карточкой товара. """

    query_tokens = {
        token
        for token in normalize_text(
            query
        ).split()
        if len(token) >= 2
    }

    if not query_tokens:
        return 0.0

    candidate_text = normalize_text(
        candidate
    )

    matched = sum(
        1
        for token in query_tokens
        if token in candidate_text
    )

    return (
        matched
        / len(query_tokens)
    )


def is_real_brand_name( value: str | None, ) -> bool:
    return normalize_text(
        value
    ) not in {
        "",
        "бренд не указан",
        "не указан",
        "unknown",
        "no brand",
        "без бренда",
    }


async def save_photo_to_product( *, message: Message, file_id: str, query: str, ) -> bool:
    """ Ищет наиболее подходящий товар и сохраняет Telegram file_id в Product.image_url. Возвращает True, если фотография сохранена. """

    cleaned_query = " ".join(
        str(query or "")
        .strip()
        .split()
    )

    if not cleaned_query:
        return False

    try:
        async with async_session_maker() as session:
            rows = await search_products(
                session=session,
                query=cleaned_query,
                limit=8,
            )

            if not rows:
                await message.answer(
                    "🔍 <b>Не нашёл подходящую "
                    "карточку товара.</b>\n\n"
                    "Попробуйте написать точнее: "
                    "бренд + название.\n\n"
                    "Например:\n"
                    "<code>Poetti Leggenda Original</code>"
                )
                return False

            scored_rows = []

            for (
                product,
                brand,
                category,
            ) in rows:
                score = calculate_match_score(
                    query=cleaned_query,
                    candidate=(
                        build_candidate_text(
                            product=product,
                            brand=brand,
                        )
                    ),
                )

                scored_rows.append(
                    (
                        score,
                        product,
                        brand,
                        category,
                    )
                )

            scored_rows.sort(
                key=lambda item: item[0],
                reverse=True,
            )

            (
                best_score,
                product,
                brand,
                _category,
            ) = scored_rows[0]

            if best_score < 0.60:
                await message.answer(
                    "🤔 <b>Не могу уверенно "
                    "привязать фото к товару.</b>\n\n"
                    "Напишите более точное название "
                    "и бренд.\n\n"
                    "Например:\n"
                    "<code>Poetti Leggenda Original</code>"
                )
                return False

            old_image = getattr(
                product,
                "image_url",
                None,
            )

            product.image_url = file_id

            await session.commit()

            brand_name = str(
                getattr(
                    brand,
                    "name",
                    "",
                )
                or ""
            ).strip()

            product_name = str(
                getattr(
                    product,
                    "name",
                    "",
                )
                or ""
            ).strip()

            if is_real_brand_name(
                brand_name
            ):
                title = (
                    f"{escape(brand_name)} — "
                    f"{escape(product_name)}"
                )
            else:
                title = escape(
                    product_name
                )

            logger.info(
                "Product photo saved: "
                "product_id=%s user_id=%s "
                "score=%.2f old_image=%r "
                "new_file_id=%s",
                product.id,
                (
                    message.from_user.id
                    if message.from_user
                    else None
                ),
                best_score,
                old_image,
                file_id,
            )

            action_text = (
                "Фотография карточки обновлена."
                if old_image
                else (
                    "Фотография добавлена "
                    "в карточку товара."
                )
            )

            await message.answer_photo(
                photo=file_id,
                caption=(
                    "✅ <b>"
                    f"{escape(action_text)}"
                    "</b>\n\n"
                    f"<b>{title}</b>\n\n"
                    "Фото сохранено в MarkaRadar. "
                    "Теперь оно будет показываться "
                    "при открытии этой карточки."
                ),
            )

            return True

    except Exception:
        logger.exception(
            "Ошибка сохранения фотографии "
            "товара: query=%r",
            cleaned_query,
        )

        await message.answer(
            "⚠️ Не удалось сохранить фотографию.\n"
            "Попробуйте ещё раз немного позже."
        )

        return False


@router.message( F.photo )
async def product_photo_handler( message: Message, ) -> None:
    """ Два сценария: 1. Фото сразу с подписью: фото сохраняется немедленно. 2. Фото без подписи: запоминаем file_id и ждём следующее текстовое сообщение пользователя. """

    if not message.photo:
        return

    photo = message.photo[-1]
    file_id = photo.file_id

    caption = " ".join(
        str(
            message.caption
            or ""
        )
        .strip()
        .split()
    )

    if caption:
        _clear_pending_photo(
            message
        )

        await save_photo_to_product(
            message=message,
            file_id=file_id,
            query=caption,
        )

        return

    _set_pending_photo(
        message=message,
        file_id=file_id,
    )

    await message.answer(
        "📷 <b>Фото получено.</b>\n\n"
        "Теперь следующим сообщением напишите "
        "название товара и бренд.\n\n"
        "Например:\n"
        "<code>Кофе Poetti Leggenda</code>\n\n"
        "Я привяжу это фото к найденной "
        "карточке товара."
    )


@router.message( F.text, HasPendingProductPhoto(), )
async def pending_product_photo_name_handler( message: Message, ) -> None:
    """ Перехватывает только следующее текстовое сообщение после фото без подписи. Обычный текст, когда ожидаемого фото нет, сюда вообще не попадёт и продолжит обрабатываться search_router. """

    pending = _get_pending_photo(
        message
    )

    if pending is None:
        return

    text = " ".join(
        str(
            message.text
            or ""
        )
        .strip()
        .split()
    )

    if not text:
        return

    if text.startswith("/"):
        _clear_pending_photo(
            message
        )
        return

    saved = await save_photo_to_product(
        message=message,
        file_id=pending.file_id,
        query=text,
    )

    # Если карточка нашлась и фото записалось —
    # завершаем состояние.
    #
    # Если совпадение плохое, оставляем фото ещё
    # на несколько минут: пользователь сможет
    # просто отправить более точное название,
    # не загружая картинку повторно.
    if saved:
        _clear_pending_photo(
            message
        )
