"""One student's past conversations, for the chat-history screen.

The store keeps every turn as one row with the moment it was said. This groups
those rows into CONVERSATIONS the way a chat app lists them -- a talk that
started this morning is one entry, the one from last night another -- and
cleans each turn back into what was actually said, without the ANSWER: header
and the [ACTION: ...] tags the model writes for the device to act on.

Only ever for one user_id at a time, which is the whole privacy rule: the
screen asks for the active student's conversations and the query underneath
filters on that id, so there is no path from here to anybody else's.
"""

import re

import store

# A pause this long starts a new conversation. Long enough that thinking about
# an answer, or a song playing in between, does not split one talk in two;
# short enough that the morning's homework and the evening's questions are
# listed separately.
NEW_CHAT_AFTER_S = 30 * 60

RE_USER_PREFIX = re.compile(r'^\s*User:\s*', re.IGNORECASE)
RE_ANSWER = re.compile(r'^\s*ANSWER:\s*', re.IGNORECASE | re.MULTILINE)
RE_EMOTION = re.compile(r'^[ \t]*EMOTION:.*\n?', re.IGNORECASE | re.MULTILINE)
# Same shape as assistant.RE_ACTION_TAG_STRIP: one level of [...] nested inside.
RE_ACTION = re.compile(r'\[\s*ACTION\s*:(?:[^\[\]]|\[[^\[\]]*\]?)*\]?', re.IGNORECASE)


def clean(role, content):
    text = content or ""
    if role == "user":
        text = RE_USER_PREFIX.sub("", text)
    else:
        text = RE_EMOTION.sub("", text)
        text = RE_ACTION.sub("", text)
        text = RE_ANSWER.sub("", text)
    return re.sub(r'[ \t]+\n', '\n', text).strip()


def conversations(user_id):
    """[{"start", "end", "title", "turns": [{"role", "text", "at"}]}], newest
    conversation first, turns inside each oldest first. None if the store is
    down -- the screen says so rather than showing an empty list, which would
    read as "you have never talked to her"."""
    rows = store.chat_log(user_id)
    if rows is None:
        return None
    chats, current, last_at = [], None, None
    for row in rows:
        at = row["created_at"]
        text = clean(row["role"], row["content"])
        if not text:
            continue
        if current is None or (at - last_at).total_seconds() > NEW_CHAT_AFTER_S:
            current = {"start": at, "turns": []}
            chats.append(current)
        current["turns"].append({"role": row["role"], "text": text, "at": at})
        current["end"] = last_at = at
    for chat in chats:
        first_question = next((t["text"] for t in chat["turns"]
                               if t["role"] == "user"), chat["turns"][0]["text"])
        chat["title"] = " ".join(first_question.split())
    chats.reverse()
    return chats
