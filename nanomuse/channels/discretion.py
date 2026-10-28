"""Discretion: what a chat may do, decided before the agent hears about it.

Every inbound message and every approval answer passes through one matrix, the
same for every vendor. The rules, in the order they are checked:

1. A sender nobody allowlisted or paired is heard by nobody: denied.
2. A group chat is read-only for approvals: a button or a word from a group
   never settles an approval. Approvals are settled in a direct chat, where
   there is one person to answer for the yes.
3. A group chat with the ``mention`` policy is heard only when the bot was
   addressed (an @-mention or a reply to it); with the ``open`` policy every
   message in the group is heard. A direct chat is always heard.
4. Anything else is allowed.

The manager owns the allowlist and the pairing (who a sender is); this module
owns only the shape of the decision, so the rule can be tested without a
server, a channel, or a vendor account.
"""

from __future__ import annotations

from dataclasses import dataclass

OPERATIONS = ("message", "approve", "deliver")


@dataclass(frozen=True)
class DiscretionQuery:
    """One thing a chat wants to do, with everything the decision needs."""

    channel: str
    chat_id: str
    sender_id: str
    is_group: bool
    operation: str  # one of OPERATIONS
    # the sender is on the allowlist or paired (the manager checked)
    sender_allowed: bool
    # in a group: the bot was addressed; direct chats always count as addressed
    mentioned: bool = True
    # a group hears every message, not only addressed ones
    mention_required: bool = True


def allows(query: DiscretionQuery) -> bool:
    """The matrix's answer. Unknown operations are denied: a new kind of action
    is not waved through by a matrix written before it existed."""
    if query.operation not in OPERATIONS:
        return False
    if not query.sender_allowed:
        return False
    if query.is_group and query.operation == "approve":
        return False
    if query.is_group and query.mention_required and not query.mentioned:
        return False
    return True


@dataclass(frozen=True)
class ChatRule:
    """One row of a per-chat override: a chat (or every chat of a channel) with
    its own operations. Rows are checked in order; the first row that names the
    chat and the operation decides."""

    channel: str = "*"  # a channel name, or "*" for every channel
    chat_id: str = "*"  # a chat id, or "*" for every chat
    operations: frozenset[str] = frozenset(OPERATIONS)
    effect: str = "allow"  # "allow" or "deny"


class DiscretionMatrix:
    """The base matrix (:func:`allows`) plus per-chat overrides.

    An override can tighten a chat (a group that may read but never be delivered
    to) or loosen nothing: an override never re-allows what the base matrix
    denied, so a row cannot talk a group into settling approvals.
    """

    def __init__(self, rules: list[ChatRule] | None = None):
        self.rules = list(rules or [])

    def allows(self, query: DiscretionQuery) -> bool:
        if not allows(query):
            return False
        for rule in self.rules:
            if rule.channel != "*" and rule.channel != query.channel:
                continue
            if rule.chat_id != "*" and rule.chat_id != query.chat_id:
                continue
            if query.operation not in rule.operations:
                continue
            return rule.effect == "allow"
        return True


__all__ = ["OPERATIONS", "ChatRule", "DiscretionMatrix", "DiscretionQuery", "allows"]
