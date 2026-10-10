"""Conversations passed between steps.

A conversation is an immutable transcript bound to one provider. A step can
start a fresh one or continue its predecessor's. Continuing keeps the earlier
messages as a byte-identical prefix, so providers can reuse cached input;
whether a step continues is recipe configuration, not pipeline structure.
"""
from dataclasses import dataclass
import hashlib
import json


@dataclass(frozen=True)
class Conversation:
    provider: str
    messages: tuple = ()

    @classmethod
    def start(cls, provider, system, user):
        return cls(provider, (('system', system), ('user', user)))

    def extend(self, *messages):
        """Append ``(role, content)`` pairs and return the new conversation."""
        return Conversation(self.provider, self.messages + tuple(messages))

    def answer(self, content):
        return self.extend(('assistant', content))

    def ask(self, content):
        return self.extend(('user', content))

    def payload(self):
        return [{'role': role, 'content': content} for role, content in self.messages]

    @property
    def sha256(self):
        return hashlib.sha256(json.dumps(self.messages, ensure_ascii=False).encode()).hexdigest()
