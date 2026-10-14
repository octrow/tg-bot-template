"""Английский каталог текстов бота (спека localization).

Перевод каталога `ru.py`. Ключи и структура идентичны русскому файлу -
смена языка не должна была изменить ни одного ключа. Ключи - прежние
имена констант.

Служебные маркеры протокола с моделью (`NEED_SOURCES`) здесь не лежат: они не
текст для человека и одинаковы во всех локалях.
"""

from .bilingual import STRINGS as BILINGUAL

STRINGS: dict = {
    **BILINGUAL,
    # --- общее ----------------------------------------------------------
    "START": (
        "<b>Type a question.</b>\n\n"
        "All commands: /help\n"
        "Язык / Language: /language"
    ),
    "UNKNOWN_COMMAND": "No such command. All commands: /help",
    "NON_TEXT_HINT": (
        "Please put it in words in one message. The bot does not read voice messages, videos, "
        "stickers or files, but you can send a screenshot as a picture."
    ),
    "WHOAMI": "user_id: {user_id}\nchat_id: {chat_id}",
    "HELP_TEXT": (
        "Commands:\n{lines}\n"
        "Model: {model}\n"
        "Version: {version}"
    ),
    "HELP_COMMAND_LINE": "/{name} - {desc}",
    # --- доступ ---------------------------------------------------------
    "REFUSAL": (
        "The bot is open only to approved employees.\n"
        "Request access: /request_access"
    ),
    "ADMIN_REFUSAL": "This command is only available to admins.",
    # --- меню команд Telegram -------------------------------------------
    "CMD_DESCRIPTIONS": {
        "start": "Start working with the bot",
        "help": "List of commands and help",
        "whoami": "My user_id and chat_id",
        "support": "Feedback about the bot",
        "request_access": "Request access to the bot",
        "language": "Interface language: Russian / English",
        "allow": "Allow a user access",
        "deny": "Remove a user from the allowlist",
        "users": "List of allowlist users",
        "cost": "Token and $ usage for a session",
        "cache_clear": "Clear the agent's answer cache",
        "logs": "Bot log files",
        "feedback": "User feedback",
        "echo": "Echo mode: the bot repeats your text",
    },
    # --- переход по просьбе словами (bot-modes): кнопка под ответом Справки --
    "MODE_GO": {
        "echo": "Go to Echo",
    },
    "MODE_SWITCH_STALE": "This offer is out of date: your next message already has an answer.",
    "MODE_SWITCH_LOST": "The message was lost after a restart. Please send it again.",
    # --- режимы: шапка 'режим · этап' (bot-modes) ----------------
    "MODE_HEADERS": {
        "reference": "💬 Help",
        "echo": "🔁 Echo",
    },
    "MODE_STAGES": {
        "echo_step": "step {n}",
    },
    # --- /echo: демо-режим ------------------------------------------------
    "ECHO_HINT": "Send some text, the bot will repeat it.",
    "ECHO_EXIT_BUTTON": "Back to Help",
    "ECHO_LEFT": "You are in Help. Type a question.",
    # --- /language ------------------------------------------------------
    "LANGUAGE_PROMPT": "Which language should the interface use?",
    "LANGUAGE_NAMES": {"ru": "Русский", "en": "English"},
    "LANGUAGE_CURRENT_MARK": "✓ {name}",
    "LANGUAGE_SET": "Interface language: English.",
    # --- /request_access ------------------------------------------------
    "ACCESS_ALREADY": "You already have access. Type a question.",
    "ACCESS_PENDING": "A request is already sent. Waiting for the admin's decision.",
    "ACCESS_QUESTION": "Your name and why you need access - will you send it in one message or skip?",
    "ACCESS_SENT": "Request sent to the admins. The decision will come here.",
    "ACCESS_SKIP_BUTTON": "Skip",
    "ACCESS_APPROVE_BUTTON": "Approve",
    "ACCESS_DENY_BUTTON": "Deny",
    "ACCESS_NAME_SKIPPED": "(not provided)",
    "ACCESS_CARD": (
        "Access request #{id}\n"
        "user_id: {user_id}{username}\n"
        "Telegram name: {full_name}\n\n"
        "Language: {language}\n"
        "First seen: {first_seen}\n"
        "Denials: {denied_count}, past requests: {past}\n"
        "Name and reason: {name_reason}"
    ),
    "ACCESS_PAST_NONE": "none",
    "ACCESS_DECIDED_NOTE": "\n\nDecided by: {admin} - {verdict}.",
    "ACCESS_VERDICT_APPROVED": "approved",
    "ACCESS_VERDICT_DENIED": "denied",
    "ACCESS_DECIDED_ALREADY": "Already decided.",
    "ACCESS_GRANTED": (
        "Access granted.\n"
        "Type a question."
    ),
    "ACCESS_REJECTED": "Your access request was denied.",
    # --- админские команды ----------------------------------------------
    "USAGE_TARGET": "Usage: {command} and user_id or @username",
    "UNSEEN_USERNAME": (
        "@{username} has not messaged the bot yet.\n"
        "Ask them to send /whoami to the bot, then repeat /allow @{username}."
    ),
    "ALLOW_ADDED": "User {target} added to the allowlist.",
    "ALLOW_ALREADY": "User {target} is already in the allowlist.",
    "DENY_REMOVED": "User {target} removed from the allowlist.",
    "DENY_ABSENT": "User {target} is not in the allowlist.",
    "USERS_ADMINS": "Admins (from env): {admins}",
    "USERS_NONE": "none",
    "USERS_ALLOWLIST": "Allowlist:",
    "USERS_ROW": "  {user_id}{nick} (added by {added_by}, {added_at})",
    "USERS_EMPTY": "  empty",
    # --- /support и /feedback -------------------------------------------
    "SUPPORT_SAVED": "Thanks, feedback recorded. The team will see it.",
    "SUPPORT_USAGE": "Send your feedback as one message: /support and the text.",
    "FEEDBACK_NONE": "No unread feedback.",
    "FEEDBACK_CARD": "Feedback #{id} - {author}, {ts}{session_note}\n\n{text}",
    "FEEDBACK_SESSION_NOTE": ", session {session_id}",
    "FEEDBACK_READ_BUTTON": "✓ Read",
    "FEEDBACK_LOG_BUTTON": "Session log",
    "FEEDBACK_MARKED": "Marked as read.",
    "FEEDBACK_ALREADY_READ": "Already read.",
    "FEEDBACK_NOT_FOUND": "Feedback not found.",
    "FEEDBACK_ALL_HEADER": "Latest feedback ({shown} of {total}):",
    "FEEDBACK_ALL_LINE": "{status} #{id} {ts} {author}: {text}",
    "FEEDBACK_STATUS_READ": "OK",
    "FEEDBACK_STATUS_UNREAD": "new",
    "FEEDBACK_REMINDER": "Unread feedback: {n}. Open: /feedback",
    "CACHE_CLEARED": "Answer cache cleared: entries removed - {removed}.",
    # --- /logs ----------------------------------------------------------
    "LOGS_EMPTY": "No log files: the log directory is empty.",
    "LOGS_PAGE_TITLE": "Log files (page {page} of {pages}, {total} total):",
    "LOGS_NEXT_BUTTON": "Next ->",
    "LOGS_PREV_BUTTON": "<- Back",
    "LOGS_FILE_GONE": "File {name} is gone: removed by size-based rotation.",
    "LOGS_PART": "Part {number} of {total}.",
    "LOGS_PART_LABEL": "[Part {i}/{n}]\n\n",
    # --- /cost ----------------------------------------------------------
    "COST_NOT_FOUND": "Session {session_id} not found.",
    "COST_NO_SESSIONS": "No sessions yet.",
    "COST_TEXT": (
        "Session {session_id} ({status}), user_id {user_id}\n\n"
        "Tokens: in {in_tok}, out {out_tok}\n"
        "Prompt cache: read {cache_read}, write {cache_write}\n"
        "Calls: agent {calls}, from cache {cache_hits}, failed {failed}\n"
        "Unverified answers (full call without touching any file): {unverified}\n"
        "Cost: ${usd:.4f}"
    ),
    # A zero must not read as "these calls were free": the note is printed only
    # when the provider really returned no price.
    "COST_UNPRICED": (
        "\nThe provider returned no price for {unpriced} call(s) - "
        "they are not included in the total."
    ),
    "CHOSEN_PREFIX": "Chosen: ",
    # --- failures (спека failure-messages) -----------------
    "FAILURE_TIMEOUT": (
        "The model didn't answer within {wait}: a provider failure.\n"
        "Retrying right now is unlikely to help, try again later."
    ),
    "FAILURE_BAD_ANSWER": (
        "The model returned a broken answer, it is hidden.\n"
        "Try rephrasing the question."
    ),
    "FAILURE_UNAVAILABLE": "The model is unavailable right now. Try again in a few minutes.",
    "FAILURE_NOT_CONFIGURED": (
        "The bot isn't set up to work with the model.\n"
        "Retrying won't help, an administrator is needed."
    ),
    "FAILURE_BOT": (
        "Something failed in the bot.\n"
        "Try again. If that doesn't work, report it to the developers."
    ),
    "FAILURE_NETWORK": (
        "Telegram did not accept the reply: a network failure.\n"
        "The bot is fine, send the request again."
    ),
    "FAILURE_CODE": "Incident code: {code}",
    "FAILURE_WAIT_MIN": "{n} min",
    "FAILURE_WAIT_SEC": "{n} s",
    "FAILURE_REPORT_BUTTON": "Report to developers",
    "FAILURE_REPORTED": (
        "Sent to the developers.\n"
        "To add details: /support and the text."
    ),
    "FAILURE_ALREADY_REPORTED": "Already sent.",
    "FAILURE_REPORT_FAILED": "Couldn't send it. Try /support and the text.",
    "FAILURE_FEEDBACK_TAG": "[Bot failure, incident code {code}]\nMode: {mode}\nQuestion: {question}",
    "FAILURE_PUSH": "Failure: {kind}, code {code}\nMode: {mode}\nModel: {model}\nTime: {time}\nQuestion: {question}",
    "FAILURE_PUSH_MORE": "\nand {n} more like it",
    # --- промпты агента -------------------------------------------------
    "PROMPT_SYSTEM": (
        "You answer questions from the product's documentation and code. "
        "Answer only from what you actually see in the available files - read them "
        "with the read_file/grep/glob tools, cite concrete paths and symbols. If "
        "the answer isn't in the code or the docs - say so directly, don't invent "
        "behavior from memory. The files may be in another language, but you must "
        "answer the user in English."
    ),
    # reference-language-and-grounding: the help mode has its own system prompt,
    # without the general instructions ("cite paths") that contradict its style.
    "PROMPT_REFERENCE_SYSTEM": (
        "You answer questions about the product and this Telegram bot. About the "
        "product, answer only from what you actually see in the available files "
        "and in the bot guide; if you are given the grep, glob and read_file tools - "
        "start with grep for two or three key words of the question, then "
        "read_file on what you found. Do not show file paths or function names to "
        "the user. If the sources have no answer - say so directly, don't invent "
        "behavior from memory. The files may be in another language, but answer in "
        "the language the request asks for."
    ),
    "PROMPT_OVERVIEW_HEAD": "Available files (part of the list, find the rest with the tools):",
    "PROMPT_OPENAI_SYSTEM": (
        "You help the product's users. "
        "You have NO access to the codebase or documentation - rely on the context "
        "from the request and propose wording based on it. Don't assert facts "
        "about system behavior that aren't in the request. If the "
        "request asks for JSON - return only JSON, no explanations. Answer the "
        "user in English."
    ),
    "PROMPT_OPENAI_TOOLS_SUFFIX": (
        " Tools: grep, glob, read_file. Start with grep on two or three key words "
        "of the task - it's cheaper and more precise than browsing the tree; glob "
        "is only needed if it's unclear where to look; read_file - on what you "
        "found. If the files don't have the answer, say so and still propose "
        "wording from the request's context, without presenting it as a fact "
        "about the system. If the request asks for JSON - return only JSON, no "
        "explanations."
    ),
    # --- Reference mode (спека reference-mode) ---
    # Style rules distilled from the humanizer skill (github.com/blader/humanizer,
    # MIT, after Wikipedia "Signs of AI writing").
    # Rules follow docs/simple-language.md (спека chat-text-formatting).
    "PROMPT_CHAT_STYLE": (
        "How to write:\n"
        "- the first line is the answer itself, one sentence, in bold "
        "(**answer**), no preamble like \"Let's break this down\"; the answer "
        "states only what a source you read confirms; if the sources have no "
        "answer, that is the answer: \"the sources do not have this\", a guess "
        "goes below, marked as a guess;\n"
        "- blocks of at most 5 lines in a row, a blank line between blocks; a line "
        "is one short sentence or one list item; one idea per block; at most 3-4 "
        "blocks per answer;\n"
        "- steps, conditions and options as a \"- \" list, one item per line, no "
        "nesting;\n"
        "- only the first line of a block may be bold: the answer itself or, when "
        "the answer has several independent parts, the part's heading;\n"
        "- details you cannot leave out go in a quote: every line starts with "
        "\"> \", the chat shows it collapsed; never write the answer itself as a "
        "quote;\n"
        "- the action you expect from the person goes in a separate block at the "
        "end;\n"
        "- no file paths, file extensions, function names or full slugs (not: "
        "docs/setup/notifications.md, config.json, "
        "status_reason=user_cancel); a short identifier the person can use to "
        "find the thing or ask a colleague is fine: an order number, a section "
        "name, a setting name;\n"
        "- do not talk about yourself: how you work or what you will do next;\n"
        "- no \"Great question!\", \"I hope this helps\", \"Let me know\" and no "
        "offers to continue;\n"
        "- no \"it's not just X, it's Y\" and no closing line that repeats the "
        "point;\n"
        "- no filler: evaluative words (\"convenient\", \"efficient\"), "
        "intensifiers (\"very\", \"key\"), vague words (\"about\", \"various\"), "
        "bureaucratese (\"is carried out\", \"within the framework of\") and clichés;\n"
        "- plain words: \"is\", \"has\" instead of \"serves as\", \"represents\"; "
        "a term only with an explanation in plain words;\n"
        "- if the sources have no answer, say so; never present a guess as a fact.\n\n"
        "Example.\n"
        "Before: \"Notifications are configured within the Settings section by means "
        "of the corresponding switch, after which the changes are applied "
        "automatically on the user's next sign-in. I hope this helps!\"\n"
        "After:\n"
        "**Notifications are turned on in \"Settings\".**\n\n"
        "- Open \"Settings\".\n"
        "- Turn on \"Notifications\".\n\n"
        "The change works from your next sign-in."
    ),
    "PROMPT_REFERENCE": (
        "You help people with their questions. The questions are about {product} "
        "and this Telegram bot. The person writing is often not technical. Give an "
        "example from their work if it helps.\n\n"
        "Sources are only the available files (docs and code) and the bot guide "
        "below. Back every claim about how things are built and work with a file "
        "you read, but do not show paths or function names to the user. You do "
        "not see logs or working data. For questions about other services or "
        "topics, politely decline in one sentence and suggest asking about your "
        "topic.\n\n"
        "{style}\n\n"
        "Answer in the user's language (en).\n\n"
        "{howto}{history}Question: {question}"
    ),
    "PROMPT_REFERENCE_CHAT": (
        "You help people with their questions. The questions are about {product} "
        "and this Telegram bot. Your only source here is the bot guide below.\n\n"
        "If the message is a greeting, thanks or small talk, answer briefly and "
        "suggest what can be asked: how things are built and work, the bot's "
        "commands. Do not write about your sources or access to files. If it is a "
        "question about the bot itself (commands, language, access), answer from "
        "the bot guide. If the answer requires knowing how the product is built or "
        "behaves, answer exactly {marker} - no other words. Do not invent facts "
        "about the product from memory.\n\n"
        "{style}\n\n"
        "Answer in the user's language (en).\n\n"
        "{howto}{history}Message: {question}"
    ),
    "PROMPT_REFERENCE_RETRY": (
        "You help people with their questions. The questions are about {product} "
        "and this Telegram bot. The previous answer was not clear. Re-check every "
        "claim against the files and the bot guide and explain again: simpler, "
        "shorter than before, without jargon, with an example from the person's "
        "work. If you cannot back something with files, say so plainly.\n\n"
        "{style}\n\n"
        "Answer in the user's language (en).\n\n"
        "{howto}Question: {question}\n\nPrevious (unclear) answer:\n{first_answer}"
    ),
    # Тема Справки без `TGBOT_PRODUCT_NAME`.
    "PROMPT_PRODUCT_DEFAULT": "this product",
    "PROMPT_REFERENCE_HOWTO": "Bot guide:\n<<<\n{howto}\n>>>\n\n",
    "PROMPT_REFERENCE_HISTORY": "Earlier conversation (for follow-up questions):\n{turns}\n\n",
    "PROMPT_REFERENCE_TURN": "Question: {question}\nAnswer: {answer}",
    "STATUS_REFERENCE": "Looking for the answer in the docs and code...",
    "STATUS_REFERENCE_RETRY": "Explaining it more simply and re-checking the code...",
    # Статус лестницы моделей; окончательные тексты - спека failure-messages.
    "FAILURE_SLOW": (
        "The model is taking longer than usual, another one is added.\n"
        "The answer comes from whichever replies first. No need to wait: it will arrive here."
    ),
    "STATUS_ELAPSED": "{clock} so far.",
    "REFERENCE_UNVERIFIED": "[no confirmation in the docs and code - please check with colleagues]",
    "REFERENCE_CLEAR": "Clear",
    "REFERENCE_UNCLEAR": "Not clear",
    "REFERENCE_HUMAN": "Pass the question to people",
    "REFERENCE_HUMAN_PROMPT": (
        "It could not be explained more clearly.\n"
        "The team will see the question and both answers.\n\n"
        "Pass the question to the team?"
    ),
    "REFERENCE_HUMAN_SENT": "The question is passed to the team. The answer will come here or in person.",
    "REFERENCE_FEEDBACK_TAG": "[Reference question - the bot could not explain]\nQuestion: {question}\n\nAnswer 1:\n{first_answer}\n\nAnswer 2:\n{second_answer}",
    # --- Screenshots (спека screenshot-input) ---
    "IMAGE_MARK": "[Read from a screenshot]",
    "IMAGE_MARK_OF": "[Read from screenshot {k}/{n}]",
    "IMAGE_DEFAULT_QUESTION": "What is on this screen and what is wrong here?",
    "IMAGE_BAD_FORMAT": "I can't read this format. Send the screenshot as a photo or as a PNG/JPG file.",
    "IMAGE_TOO_BIG": (
        "The image is over {mb} MB, it can't be read. Send the screenshot as a photo or a smaller PNG/JPG file."
    ),
    "IMAGE_NO_KEY": (
        "Screenshots can't be read right now: the image model is not set up. Describe the question in text."
    ),
    "IMAGE_FAILED": "The screenshot could not be read. Describe the question in text or send the image again.",
    "IMAGE_FAILED_WITH_TEXT": "The screenshot could not be read, the answer is from your text.",
    "IMAGE_SEND_AS_FILE": "If the screenshot has small text, send it as a file, without compression.",
    "IMAGE_FIRST_OF": "The first {k} of {n} screenshots are taken into account.",
    "IMAGE_READ_OF": "Read {k} of {n} screenshots.",
    "IMAGE_PACK_FULL": "At most 10 screenshots per question. These {n} were not read.",
    # Message pack (спека message-packs).
    "PACK_LATE_NOTE": "The answer above covers only the first messages ({k}). {m} more arrived meanwhile.",
    "PACK_REDO_BUTTON": "Answer with the new ones",
    "PACK_REDO_STALE": "These messages are already part of your next question.",
    "PACK_REDO_BUSY": "The answer to your new message is still in progress. Press again once it arrives.",
    "PACK_DROPPED": "Your previous message was not processed.",
}
