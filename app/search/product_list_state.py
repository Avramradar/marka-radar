import time
from dataclasses import dataclass


STATE_TTL_SECONDS = 15 * 60


@dataclass
class ProductListState:
    created_at: float
    title: str
    products: list[dict]


_states: dict[
    tuple[int, int],
    ProductListState,
] = {}


def save_product_list(
    *,
    chat_id: int,
    user_id: int,
    title: str,
    products: list[dict],
) -> None:
    """
    Сохраняет найденные товары для пагинации.

    В состоянии хранятся только простые словари,
    а не SQLAlchemy-объекты.
    """

    cleanup_expired_states()

    _states[(chat_id, user_id)] = ProductListState(
        created_at=time.monotonic(),
        title=title,
        products=products,
    )


def get_product_list(
    *,
    chat_id: int,
    user_id: int,
) -> ProductListState | None:
    """
    Возвращает сохранённый список товаров.
    """

    cleanup_expired_states()

    return _states.get(
        (chat_id, user_id)
    )


def clear_product_list(
    *,
    chat_id: int,
    user_id: int,
) -> None:
    """
    Удаляет список товаров пользователя.
    """

    _states.pop(
        (chat_id, user_id),
        None,
    )


def cleanup_expired_states() -> None:
    """
    Удаляет списки старше 15 минут.
    """

    current_time = time.monotonic()

    expired_keys = [
        key
        for key, state in _states.items()
        if (
            current_time - state.created_at
            > STATE_TTL_SECONDS
        )
    ]

    for key in expired_keys:
        _states.pop(
            key,
            None,
        )
