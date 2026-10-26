"""Scrub what the factory writes about itself into its own public repository: no hosts, addresses or tokens."""

import re

from .leaks import SECRETS

RULES = [
    *((re.compile(pattern), "<secret>") for pattern in SECRETS.values()),
    (re.compile(r"[a-z][a-z0-9+.-]*://[^\s'\"<>)]+", re.I), "<url>"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "<email>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<ip>"),
    (
        re.compile(r"\b(?:[a-z0-9-]+\.)+(?:ru|kz|com|net|org|io|local|internal|corp|lan)(?::\d+)?\b", re.I),
        "<host>",
    ),
    # Absolute paths outside the task workspace name machines, users and volumes.
    (re.compile(r"(?<![\w/])/(?!workspace\b)(?:[\w.-]+/)+[\w.-]+"), "<path>"),
    # Long opaque strings: ids, hashes and keys say nothing a fix needs.
    (re.compile(r"\b[A-Za-z0-9_-]{32,}\b"), "<id>"),
]


def scrub(text, patterns=(), names=()):
    """`patterns`: the owner's leak patterns; `names`: project or person names to hide."""
    for rule, mark in RULES:
        text = rule.sub(mark, text)
    for item in [*patterns, *names]:
        if item:
            text = re.sub(re.escape(item), "<redacted>", text, flags=re.I)
    return text
