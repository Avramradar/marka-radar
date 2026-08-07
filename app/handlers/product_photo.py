import logging
from html import escape

from aiogram import F, Router
from aiogram.types import Message

from app.database.repositories.product_repository import (
    search_products,
)
from app.database.session import async_session_maker


router = Router()
logger = logging.getLogger(__name__)


def normalize_text( value: str | None, ) -> str:
    """ Простая нормализация текста для проверки соответствия подписи найденному товару. """

    return " ".join(
        str(value or "")
        .strip()
        .lower()
        .replace("ё", "е")
        .split()
    )


def build_candidate_text( *, product, brand, ) -> str:
    """ Формирует поисковый текст найденного товара. """

    return normalize_text(
        f"{getattr(brand, 'name', '')} "
        f"{getattr(product, 'name', '')}"
    )


def calculate_match_score( *, query: str, candidate: str, ) -> float:
    """ Оценивает совпадение подписи фотографии с найденной карточкой товара. 1.0 — все слова запроса присутствуют в карточке. """

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


@router.message(F.photo)
async def product_photo_handler( message: Message, ) -> None:
    """ Сохраняет пользовательскую фотографию в уже существующую карточку товара. Пользователь отправляет фотографию с подписью, например: Poetti Leggenda Кофе Poetti Сметана Чабан MarkaRadar ищет товар в своей базе и, если совпадение достаточно уверенное, сохраняет Telegram file_id в product.image_url. Telegram file_id можно напрямую передавать в message.answer_photo(), поэтому отдельное файловое хранилище на первом этапе не требуется. """

    if not message.photo:
        return

    caption = (
        message.caption
        or ""
    ).strip()

    if not caption:
        await message.answer(
            "📷 <b>Фото получено.</b>\n\n"
            "Добавьте к фотографии подпись "
            "с названием товара.\n\n"
            "Например:\n"
            "<code>Кофе Poetti Leggenda</code>\n"
            "или\n"
            "<code>Сметана Чабан</code>"
        )
        return

    # Берём самое большое изображение,
    # Telegram располагает PhotoSize
    # от меньшего к большему.
    photo = message.photo[-1]

    file_id = photo.file_id

    try:
        async with async_session_maker() as session:
            rows = await search_products(
                session=session,
                query=caption,
                limit=5,
            )

            if not rows:
                await message.answer(
                    "🔍 <b>Такого товара пока "
                    "нет в базе MarkaRadar.</b>\n\n"
                    "Сначала найдите или добавьте "
                    "карточку товара, а затем "
                    "пришлите фотографию ещё раз."
                )
                return

            scored_rows = []

            for (
                product,
                brand,
                category,
            ) in rows:
                candidate_text = (
                    build_candidate_text(
                        product=product,
                        brand=brand,
                    )
                )

                score = calculate_match_score(
                    query=caption,
                    candidate=candidate_text,
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

            # Не привязываем фотографию,
            # если уверенность слишком низкая.
            if best_score < 0.60:
                await message.answer(
                    "🤔 <b>Не удалось уверенно "
                    "определить товар.</b>\n\n"
                    "Попробуйте отправить фото ещё "
                    "раз и написать более точное "
                    "название и бренд.\n\n"
                    "Например:\n"
                    "<code>Poetti Leggenda Original</code>"
                )
                return

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

            if brand_name and normalize_text(
                brand_name
            ) not in {
                "",
                "бренд не указан",
                "не указан",
                "unknown",
                "no brand",
                "без бренда",
            }:
                title = (
                    f"{escape(brand_name)} — "
                    f"{escape(product_name)}"
                )
            else:
                title = escape(
                    product_name
                )

            if old_image:
                action_text = (
                    "Фотография карточки обновлена."
                )
            else:
                action_text = (
                    "Фотография добавлена "
                    "в карточку товара."
                )

            logger.info(
                "Product photo saved: "
                "product_id=%s user_id=%s "
                "score=%.2f file_id=%s",
                product.id,
                (
                    message.from_user.id
                    if message.from_user
                    else None
                ),
                best_score,
                file_id,
            )

            await message.answer_photo(
                photo=file_id,
                caption=(
                    "✅ <b>"
                    f"{escape(action_text)}"
                    "</b>\n\n"
                    f"<b>{title}</b>\n\n"
                    "Теперь MarkaRadar сможет "
                    "показывать это фото другим "
                    "пользователям при открытии "
                    "карточки."
                ),
            )

    except Exception:
        logger.exception(
            "Ошибка сохранения фотографии "
            "товара: caption=%r",
            caption,
        )

        await message.answer(
            "⚠️ Не удалось сохранить фотографию.\n"
            "Попробуйте ещё раз немного позже."
        )
