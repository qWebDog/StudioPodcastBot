import time
import logging
from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery
from typing import Callable, Dict, Any, Awaitable
from config import ADMIN_IDS

logger = logging.getLogger(__name__)


class AntiFloodMiddleware(BaseMiddleware):
    def __init__(self, cooldown: float = 1.0):
        self.cooldown = cooldown
        self._timers: Dict[int, float] = {}
        self._warns: Dict[int, float] = {}

    async def __call__(
            self,
            handler: Callable[[Message | CallbackQuery, Dict[str, Any]], Awaitable[Any]],
            event: Message | CallbackQuery,
            data: Dict[str, Any]
    ) -> Any:
        user_id = event.from_user.id

        # Админы не ограничиваются
        if user_id in ADMIN_IDS:
            return await handler(event, data)

        now = time.time()
        last = self._timers.get(user_id, 0)

        if now - last < self.cooldown:
            if isinstance(event, Message):
                # Предупреждение отправляем не чаще раза в 3 секунды
                if now - self._warns.get(user_id, 0) > 3:
                    await event.answer("⏳ Пожалуйста, не отправляйте сообщения так часто.")
                    self._warns[user_id] = now
            elif isinstance(event, CallbackQuery):
                await event.answer("⏳ Не нажимайте так часто!", show_alert=False)

            logger.debug(f"🛡️ Anti-flood: пользователь {user_id}")
            return  # Прерываем цепочку, хендлер не вызовется

        self._timers[user_id] = now
        return await handler(event, data)