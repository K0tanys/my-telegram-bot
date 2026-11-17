
# ============================================================
# 10. /CHECK — ADMIN AND ANONYMOUS ADMIN SUPPORT
# ============================================================

from telethon import utils


def is_anonymous_group_admin(event) -> bool:
    """
    Проверяет, отправлено ли сообщение от имени
    самой целевой группы.

    В Telegram анонимный администратор представлен
    как sender_chat, а не как обычный пользователь.
    """

    message = event.message

    sender_id = event.sender_id

    # Основная проверка: сообщение отправлено
    # непосредственно от имени целевой группы.
    if sender_id == TARGET_CHAT_ID:
        return True

    # Дополнительная проверка PeerChannel.
    from_id = getattr(message, "from_id", None)

    if from_id is not None:
        try:
            peer_id = utils.get_peer_id(from_id)

            if peer_id == TARGET_CHAT_ID:
                return True

        except Exception:
            logger.exception(
                "Ошибка определения sender_chat"
            )

    return False


async def is_group_admin(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(
            chat_id=TARGET_CHAT_ID,
            user_id=user_id,
        )

        status = getattr(
            member.status,
            "value",
            member.status,
        )

        return status in (
            "administrator",
            "creator",
        )

    except TelegramAPIError:
        logger.exception(
            "Не удалось проверить права пользователя %s",
            user_id,
        )

        return False


async def get_latest_source_post():
    """
    Получает последнюю публикацию источника.
    Если это альбом — собирает все его элементы.
    """

    latest_messages = await client.get_messages(
        source_entity,
        limit=100,
    )

    if not latest_messages:
        return []

    latest = next(
        (
            message
            for message in latest_messages
            if message.message or message.media
        ),
        None,
    )

    if latest is None:
        return []

    if latest.grouped_id:
        album = [
            message
            for message in latest_messages
            if message.grouped_id == latest.grouped_id
        ]

        return sorted(
            album,
            key=lambda item: item.id,
        )

    return [latest]


@client.on(
    events.NewMessage(chats=TARGET_CHAT_ID)
)
async def handle_check(event):
    global check_in_progress

    text = event.raw_text or ""

    if not text.strip():
        return

    first_token = text.strip().split(
        maxsplit=1
    )[0]

    command = first_token.split(
        "@",
        1,
    )[0].lower()

    if command != "/check":
        return

    topic_id = get_topic_id(event)

    logger.info(
        "Обнаружена команда /check: "
        "message_id=%s, sender_id=%s, "
        "topic_id=%s, sender_chat=%s",
        event.id,
        event.sender_id,
        topic_id,
        is_anonymous_group_admin(event),
    )

    # Команда работает только в теме новостей.
    if topic_id != NEWS_TOPIC_ID:
        logger.warning(
            "/check отклонена: неправильная тема. "
            "Получено: %s, ожидается: %s",
            topic_id,
            NEWS_TOPIC_ID,
        )
        return

    # Защита от повторного запуска.
    if check_in_progress or publish_lock.locked():
        with suppress(Exception):
            await event.delete()

        logger.warning(
            "Повторная /check пропущена: "
            "публикация уже выполняется"
        )
        return

    check_in_progress = True

    try:
        # ----------------------------------------------------
        # ПРОВЕРКА ПРАВ
        # ----------------------------------------------------

        anonymous_admin = is_anonymous_group_admin(
            event
        )

        if anonymous_admin:
            # Telegram представляет анонимного
            # администратора от имени самой группы.
            logger.info(
                "Анонимный администратор подтверждён "
                "по sender_chat целевой группы."
            )

        else:
            user_id = event.sender_id

            if not user_id:
                logger.warning(
                    "/check: отправитель не определён"
                )
                return

            if not await is_group_admin(user_id):
                logger.warning(
                    "Отказ в /check: пользователь "
                    "%s не является администратором",
                    user_id,
                )
                return

            logger.info(
                "Администратор подтверждён. User ID: %s",
                user_id,
            )

        # ----------------------------------------------------
        # УДАЛЕНИЕ КОМАНДЫ
        # ----------------------------------------------------

        try:
            await event.delete()

        except Exception:
            logger.exception(
                "Не удалось удалить команду /check"
            )

        # ----------------------------------------------------
        # ПРИНУДИТЕЛЬНАЯ ПУБЛИКАЦИЯ
        # ----------------------------------------------------

        async with publish_lock:

            latest_post = await get_latest_source_post()

            if not latest_post:
                logger.warning(
                    "В источнике нет публикаций для /check"
                )
                return

            logger.info(
                "Получена последняя публикация. IDs=%s",
                [message.id for message in latest_post],
            )

            # Принудительная публикация.
            # Проверка SQLite намеренно обходится.
            await publish_news(latest_post)

            # Сохраняем ключ, чтобы автоматический
            # обработчик не опубликовал тот же пост повторно.
            mark_published(
                make_news_key(latest_post)
            )

            logger.info(
                "УСПЕХ: /check завершена. "
                "Анонимный администратор: %s",
                anonymous_admin,
            )

    except Exception:
        logger.exception(
            "Критическая ошибка обработки /check"
        )

    finally:
        check_in_progress = False
