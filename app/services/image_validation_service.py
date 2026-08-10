from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp


logger = logging.getLogger(__name__)


DEFAULT_TIMEOUT_SECONDS = 8
MAX_IMAGE_BYTES = 12 * 1024 * 1024


ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}


@dataclass( slots=True, frozen=True, )
class ImageValidationResult:
    """ Результат проверки внешнего изображения. valid: Можно ли считать изображение рабочим. reason: Краткая причина результата. status_code: HTTP-код ответа, если запрос дошёл до сервера. content_type: Content-Type ответа. content_length: Размер ответа в байтах, если известен. final_url: Итоговый URL после редиректов. """

    valid: bool
    reason: str

    status_code: int | None = None
    content_type: str | None = None
    content_length: int | None = None
    final_url: str | None = None


def clean_url( value: str | None, ) -> str:
    """ Безопасно очищает URL. """

    return " ".join(
        str(value or "")
        .strip()
        .split()
    )


def is_http_url( value: str | None, ) -> bool:
    """ Проверяет, является ли строка HTTP/HTTPS URL. """

    url = clean_url(
        value
    )

    return url.startswith(
        (
            "https://",
            "http://",
        )
    )


def normalize_content_type( value: str | None, ) -> str:
    """ Убирает charset и лишние параметры. Например: image/jpeg; charset=UTF-8 -> image/jpeg """

    if not value:
        return ""

    return (
        value
        .split(
            ";",
            1,
        )[0]
        .strip()
        .lower()
    )


def parse_content_length( value: str | None, ) -> int | None:
    """ Безопасно преобразует Content-Length. """

    if not value:
        return None

    try:
        result = int(
            value
        )
    except (
        TypeError,
        ValueError,
    ):
        return None

    if result < 0:
        return None

    return result


def is_allowed_content_type( content_type: str | None, ) -> bool:
    """ Проверяет тип изображения. """

    normalized = normalize_content_type(
        content_type
    )

    return (
        normalized
        in ALLOWED_CONTENT_TYPES
    )


async def validate_external_image( *, image_url: str | None, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS, max_image_bytes: int = MAX_IMAGE_BYTES, ) -> ImageValidationResult:
    """ Проверяет внешний URL изображения. Основные защиты: - URL обязан быть http/https; - сервер должен вернуть 2xx; - Content-Type обязан быть image/* из белого списка; - слишком большой файл отклоняется; - HTML, JSON и страницы магазинов не считаются фото; - редиректы разрешены; - ошибка изображения не бросается наружу. Важно: функция ничего не меняет в БД. """

    url = clean_url(
        image_url
    )

    if not url:
        return ImageValidationResult(
            valid=False,
            reason="empty_url",
        )

    if not is_http_url(
        url
    ):
        return ImageValidationResult(
            valid=False,
            reason="not_http_url",
        )

    safe_timeout = max(
        3,
        min(
            int(
                timeout_seconds
            ),
            20,
        ),
    )

    safe_max_bytes = max(
        256 * 1024,
        min(
            int(
                max_image_bytes
            ),
            25 * 1024 * 1024,
        ),
    )

    timeout = aiohttp.ClientTimeout(
        total=safe_timeout
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Linux; Android 13) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/124.0 Mobile Safari/537.36"
        ),
        "Accept": (
            "image/avif,image/webp,"
            "image/apng,image/svg+xml,"
            "image/*,*/*;q=0.8"
        ),
        "Accept-Language": (
            "ru-RU,ru;q=0.9,en;q=0.5"
        ),
    }

    try:
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
        ) as session:
            async with session.get(
                url,
                allow_redirects=True,
            ) as response:
                status_code = int(
                    response.status
                )

                final_url = str(
                    response.url
                )

                content_type = (
                    normalize_content_type(
                        response.headers.get(
                            "Content-Type"
                        )
                    )
                )

                content_length = (
                    parse_content_length(
                        response.headers.get(
                            "Content-Length"
                        )
                    )
                )

                if not (
                    200
                    <= status_code
                    < 300
                ):
                    return ImageValidationResult(
                        valid=False,
                        reason=(
                            f"http_status_{status_code}"
                        ),
                        status_code=status_code,
                        content_type=content_type or None,
                        content_length=content_length,
                        final_url=final_url,
                    )

                if not is_allowed_content_type(
                    content_type
                ):
                    return ImageValidationResult(
                        valid=False,
                        reason="invalid_content_type",
                        status_code=status_code,
                        content_type=content_type or None,
                        content_length=content_length,
                        final_url=final_url,
                    )

                if (
                    content_length is not None
                    and content_length
                    > safe_max_bytes
                ):
                    return ImageValidationResult(
                        valid=False,
                        reason="image_too_large",
                        status_code=status_code,
                        content_type=content_type,
                        content_length=content_length,
                        final_url=final_url,
                    )

                #
                # Даже если Content-Length отсутствует,
                # не скачиваем бесконечный файл.
                #
                # Нам не нужно сохранять изображение:
                # достаточно убедиться, что сервер
                # действительно отдаёт изображение.
                #
                downloaded = 0

                async for chunk in response.content.iter_chunked(
                    64 * 1024
                ):
                    if not chunk:
                        continue

                    downloaded += len(
                        chunk
                    )

                    if (
                        downloaded
                        > safe_max_bytes
                    ):
                        return ImageValidationResult(
                            valid=False,
                            reason="image_too_large",
                            status_code=status_code,
                            content_type=content_type,
                            content_length=downloaded,
                            final_url=final_url,
                        )

                    #
                    # Для проверки достаточно получить
                    # первые данные изображения.
                    #
                    if downloaded >= 1024:
                        break

                if downloaded <= 0:
                    return ImageValidationResult(
                        valid=False,
                        reason="empty_response_body",
                        status_code=status_code,
                        content_type=content_type,
                        content_length=0,
                        final_url=final_url,
                    )

                return ImageValidationResult(
                    valid=True,
                    reason="ok",
                    status_code=status_code,
                    content_type=content_type,
                    content_length=(
                        content_length
                        if content_length is not None
                        else downloaded
                    ),
                    final_url=final_url,
                )

    except (
        aiohttp.ClientError,
        TimeoutError,
    ) as error:
        logger.info(
            "Image validation failed: "
            "url=%r error_type=%s error=%s",
            url,
            type(error).__name__,
            error,
        )

        return ImageValidationResult(
            valid=False,
            reason=(
                type(error).__name__
                .lower()
            ),
        )

    except Exception as error:
        logger.exception(
            "Unexpected image validation error: "
            "url=%r",
            url,
        )

        return ImageValidationResult(
            valid=False,
            reason=(
                f"unexpected_"
                f"{type(error).__name__.lower()}"
            ),
        )
