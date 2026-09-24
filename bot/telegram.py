"""Telegram Bot API client over urllib. No third-party packages."""

import html
import json
import time
import urllib.error
import urllib.request

API_ROOT = "https://api.telegram.org/bot"
MAX_MESSAGE = 4096
# Leave room for the <pre> wrapper and for entity-escaping growth.
CHUNK_LIMIT = 3500
# Never turn one command into a wall of messages.
MAX_CHUNKS = 5


class TelegramError(Exception):
    pass


def esc(text):
    """Escape untrusted text for parse_mode=HTML."""
    return html.escape(str(text), quote=False)


def chunk(text, limit=CHUNK_LIMIT):
    """Split on line boundaries so console output stays readable."""
    text = text or ""
    if len(text) <= limit:
        return [text]

    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        # A single line longer than the limit has to be hard-split.
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks


class Bot:
    def __init__(self, token, timeout=20):
        self.token = token
        self.timeout = timeout

    def call(self, method, **params):
        url = f"{API_ROOT}{self.token}/{method}"
        data = json.dumps(params).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        # Long polling holds the connection open, so allow for it.
        socket_timeout = self.timeout + params.get("timeout", 0) + 10
        try:
            with urllib.request.urlopen(request, timeout=socket_timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                description = json.loads(body).get("description", body)
            except ValueError:
                description = body
            raise TelegramError(f"{method} failed: {description}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise TelegramError(f"{method} failed: {e}") from e

        if not payload.get("ok"):
            raise TelegramError(f"{method} failed: {payload.get('description')}")
        return payload.get("result")

    # --------------------------------------------------------------- methods
    def get_me(self):
        return self.call("getMe")

    def delete_webhook(self):
        # getUpdates is rejected while a webhook is registered.
        return self.call("deleteWebhook")

    def get_updates(self, offset=None, timeout=30, allowed=None):
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        if allowed is not None:
            params["allowed_updates"] = allowed
        return self.call("getUpdates", **params)

    def send(self, chat_id, text, preformatted=False, buttons=None, reply_to=None):
        """Send text, splitting when it exceeds Telegram's limit.

        Returns the last message sent, so callers can edit it later.
        """
        result = None
        pieces = chunk(text if text.strip() else "(no output)")
        if len(pieces) > MAX_CHUNKS:
            pieces = pieces[:MAX_CHUNKS]
            pieces[-1] += f"\n\n[... truncated, {MAX_CHUNKS} messages max]"
        for index, piece in enumerate(pieces):
            body = f"<pre>{esc(piece)}</pre>" if preformatted else esc(piece)
            params = {
                "chat_id": chat_id,
                "text": body,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            # Buttons and the reply anchor belong on the final chunk only.
            if index == len(pieces) - 1:
                if buttons:
                    params["reply_markup"] = {"inline_keyboard": buttons}
                if reply_to:
                    params["reply_to_message_id"] = reply_to
                    params["allow_sending_without_reply"] = True
            result = self.call("sendMessage", **params)
        return result

    def edit(self, chat_id, message_id, text, preformatted=False, buttons=None):
        body = f"<pre>{esc(text)}</pre>" if preformatted else esc(text)
        params = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": body[:MAX_MESSAGE],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        params["reply_markup"] = {"inline_keyboard": buttons} if buttons else {"inline_keyboard": []}
        try:
            return self.call("editMessageText", **params)
        except TelegramError as e:
            # Editing to identical text is an error we genuinely do not care about.
            if "message is not modified" in str(e):
                return None
            raise

    def answer_callback(self, callback_id, text=None):
        params = {"callback_query_id": callback_id}
        if text:
            params["text"] = text[:200]
        try:
            return self.call("answerCallbackQuery", **params)
        except TelegramError:
            # An unanswered callback only leaves a spinner; never kill the loop for it.
            return None

    def set_commands(self, commands):
        return self.call(
            "setMyCommands",
            commands=[{"command": c, "description": d} for c, d in commands],
        )


def poll(bot, handle_update, offset=None, stop=None, log=print):
    """Long-poll getUpdates forever, with backoff on network trouble."""
    backoff = 1
    while not (stop and stop.is_set()):
        try:
            updates = bot.get_updates(offset=offset, timeout=30)
            backoff = 1
        except TelegramError as e:
            log(f"getUpdates: {e}; retrying in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            try:
                handle_update(update)
            except Exception as e:  # one bad command must not kill the bot
                log(f"handler error: {e!r}")
    return offset
