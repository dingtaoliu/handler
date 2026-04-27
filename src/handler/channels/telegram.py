"""Telegram channel: receives messages via polling, delivers responses back to chats.

Each Telegram chat gets its own conversation_id (telegram:<chat_id>), so the agent
maintains separate context per user/group. Supports text, photos, documents, and
voice messages. Sends a typing indicator while the agent is processing.
"""

import asyncio
import logging
from pathlib import Path

from ..environment import Channel
from ..paths import UPLOAD_DIR as _UPLOAD_DIR
from ..types import Event
from ..users import resolve_user_from_telegram

logger = logging.getLogger("handler.channels.telegram")

# Telegram limits
MAX_MESSAGE_LENGTH = 4096


class TelegramChannel(Channel):
    """Telegram bot using long polling. One conversation per chat_id."""

    name = "telegram"

    def __init__(self, token: str, allowed_user_ids: set[int] | None = None):
        self.token = token
        self.allowed_user_ids = allowed_user_ids  # None = open to all
        self.queue: asyncio.Queue[Event] | None = None
        self._app = None  # set in start()

    async def start(self, queue: asyncio.Queue) -> None:
        self.queue = queue
        _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

        from telegram import Update
        from telegram.ext import (
            ApplicationBuilder,
            MessageHandler,
            CommandHandler,
            filters,
        )

        app = ApplicationBuilder().token(self.token).build()
        self._app = app
        updater = app.updater
        if updater is None:
            raise RuntimeError("telegram application updater is unavailable")

        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(MessageHandler(filters.PHOTO, self._on_photo))
        app.add_handler(MessageHandler(filters.Document.ALL, self._on_document))
        app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, self._on_voice))
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message)
        )

        logger.info("telegram channel starting (polling)")
        async with app:
            await app.start()
            await updater.start_polling(allowed_updates=Update.ALL_TYPES)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass
            finally:
                await updater.stop()
                await app.stop()

    async def deliver(self, event: Event, response: str) -> None:
        """Deliver via the response future (set by _on_message)."""
        if event._response_future and not event._response_future.done():
            event._response_future.set_result(response)

    async def push_message(self, conversation_id: str, role: str, content: str) -> None:
        """Proactively send a message to a Telegram chat.

        conversation_id format: 'telegram:{chat_id}'
        """
        if not self._app or not conversation_id.startswith("telegram:"):
            return
        try:
            chat_id = int(conversation_id.split(":", 1)[1])
        except (ValueError, IndexError):
            logger.warning(f"invalid telegram conversation_id: {conversation_id}")
            return
        await self._send_response(chat_id, content)

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def _cmd_start(self, update, context) -> None:
        await update.message.reply_text(
            "Connected to Handler. Send me a message to begin."
        )

    async def _cmd_help(self, update, context) -> None:
        await update.message.reply_text(
            "Send me any message and I'll respond.\n"
            "You can also send photos, documents, or voice messages.\n"
            "Each chat has its own conversation history."
        )

    # ------------------------------------------------------------------
    # Message handlers
    # ------------------------------------------------------------------

    async def _on_message(self, update, context) -> None:
        """Handle incoming text messages."""
        if not update.message or not update.message.text:
            return
        await self._process(update, update.message.text)

    async def _on_photo(self, update, context) -> None:
        """Handle photos: download, save, and send as image content to agent."""
        if not update.message or not update.message.photo:
            return

        photo = update.message.photo[-1]  # highest resolution
        file = await photo.get_file()
        ext = Path(file.file_path).suffix if file.file_path else ".jpg"
        local_path = (
            _UPLOAD_DIR
            / f"tg_{update.message.chat_id}_{update.message.message_id}{ext}"
        )
        await file.download_to_drive(str(local_path))
        logger.info(f"telegram photo saved: {local_path}")

        media_type = "image/png" if ext == ".png" else "image/jpeg"
        caption = update.message.caption or ""
        images = [{"path": str(local_path.resolve()), "media_type": media_type}]
        await self._process(update, caption, images=images)

    async def _on_document(self, update, context) -> None:
        """Handle documents: download, save, and send path + caption to agent."""
        if not update.message or not update.message.document:
            return

        doc = update.message.document
        file = await doc.get_file()
        filename = doc.file_name or f"doc_{update.message.message_id}"
        local_path = _UPLOAD_DIR / f"tg_{update.message.chat_id}_{filename}"
        await file.download_to_drive(str(local_path))
        logger.info(f"telegram document saved: {local_path}")

        caption = update.message.caption or ""
        content = f"[Document '{filename}' saved to {local_path.resolve()}]\n{caption}".strip()
        await self._process(update, content)

    async def _on_voice(self, update, context) -> None:
        """Handle voice/audio: download and send path to agent."""
        msg = update.message
        if not msg:
            return

        voice = msg.voice or msg.audio
        if not voice:
            return

        file = await voice.get_file()
        ext = Path(file.file_path).suffix if file.file_path else ".ogg"
        local_path = _UPLOAD_DIR / f"tg_{msg.chat_id}_{msg.message_id}{ext}"
        await file.download_to_drive(str(local_path))
        logger.info(f"telegram voice saved: {local_path}")

        content = f"[Voice message saved to {local_path.resolve()}]"
        await self._process(update, content)

    # ------------------------------------------------------------------
    # Core processing
    # ------------------------------------------------------------------

    async def _process(
        self, update, content: str, *, images: list[dict] | None = None
    ) -> None:
        """Push an event onto the queue and wait for the agent response."""
        chat_id = update.message.chat_id
        user = update.message.from_user
        conversation_id = f"telegram:{chat_id}"
        resolved_user = resolve_user_from_telegram(
            user.id,
            username=user.username,
            first_name=user.first_name,
        )

        if self.allowed_user_ids is not None and user.id not in self.allowed_user_ids:
            logger.warning(f"telegram blocked unauthorized user {user.id} (@{user.username})")
            await update.message.reply_text("Sorry, you're not authorized to use this bot.")
            return

        logger.info(
            f"telegram message from {user.username or user.id} "
            f"(chat {chat_id}, user={resolved_user.id if resolved_user else 'unresolved'}): {content[:100]}"
        )

        # Send typing indicator while processing
        typing_task = asyncio.create_task(self._typing_loop(update.message.chat))

        try:
            future = asyncio.get_running_loop().create_future()
            event_data = {
                "content": content,
                "chat_id": chat_id,
                "telegram_user_id": user.id,
                "username": user.username,
                "first_name": user.first_name,
            }
            if images:
                event_data["images"] = images
            event = Event(
                type="user_message",
                source="telegram",
                data=event_data,
                conversation_id=conversation_id,
                user_id=resolved_user.id if resolved_user else None,
                _response_future=future,
            )
            queue = self.queue
            if queue is None:
                raise RuntimeError("telegram queue not initialized")
            await queue.put(event)
            response = await future
        except Exception as e:
            logger.error(f"telegram processing failed: {e}", exc_info=True)
            response = f"Sorry, an error occurred: {e}"
        finally:
            typing_task.cancel()

        await self._send_response(update.message.chat_id, response)

    async def _typing_loop(self, chat) -> None:
        """Send 'typing...' indicator every 5 seconds until cancelled."""
        try:
            while True:
                await chat.send_action("typing")
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass

    async def _send_response(self, chat_id: int, response: str) -> None:
        """Send a response, splitting if it exceeds Telegram's limit."""
        if not response:
            return
        app = self._app
        if app is None:
            raise RuntimeError("telegram application not started")

        # Try markdown first, fall back to plain text
        chunks = [
            response[i : i + MAX_MESSAGE_LENGTH]
            for i in range(0, len(response), MAX_MESSAGE_LENGTH)
        ]
        for chunk in chunks:
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode="Markdown",
                )
            except Exception:
                # Markdown parsing failed — send as plain text
                try:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                    )
                except Exception as e:
                    logger.error(f"telegram send failed to {chat_id}: {e}")
