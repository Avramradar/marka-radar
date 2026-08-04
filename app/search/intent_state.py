import time
from dataclasses import dataclass


STATE_TTL_SECONDS = 15 * 60


@dataclass
class IntentState:
    created_at: float
    groups: list[dict]


_states: dict[tuple[int, int], IntentState] = {}


def save_intent_groups(
    *,
    chat_id: int,
    user_id: int,
    groups: list[dict],
) -> None:
    """
    Сохраняет уточняющие группы для пользователя.

    Ключ включает чат и пользователя, поэтому ответы
    разных людей не будут смешиваться.
    """

    cleanup_expired_states()

    _states[(chat_id, user_id)] = IntentState(
        created_at=time.monotonic(),
        groups=groups,
    )


def get_intent_group(
    *,
    chat_id: int,
    user_id: int,
    index: int,
) -> dict | None:
    """
    Возвращает выбранную уточняющую группу по индексу.
    """

    cleanup_expired_states()

    state = _states.get(
        (chat_id, user_id)
    )

    if state is None:
        return None

    if index < 0 or index >= len(state.groups):
        return None

    return state.groups[index]


def clear_intent_groups(
    *,
    chat_id: int,
    user_id: int,
) -> None:
    """
    Удаляет сохранённые уточнения пользователя.
    """

    _states.pop(
        (chat_id, user_id),
        None,
    )


def cleanup_expired_states() -> None:
    """
    Удаляет состояния старше 15 минут.
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
