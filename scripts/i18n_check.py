"""Check UI translations in darkfactory/static.

Interface strings are written in Russian and wrapped in tr(); index.html is translated by text node.
The script lists Russian text in scripts that is not wrapped, then compares every locale file with
the collected keys: missing keys, stale keys and placeholders that differ from the source.

    python scripts/i18n_check.py            # report, exit 1 on any problem
    python scripts/i18n_check.py --keys     # print the collected keys as JSON
"""

import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "darkfactory" / "static"
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
LITERAL = r"'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"|`(?:[^`\\$]|\\.|\$(?!\{))*`"
CALL = re.compile(r"\btr\(\s*(" + LITERAL + r")")
COMMENT = re.compile(r"^\s*//[^\n]*$|^\s*/\*[\s\S]*?\*/", re.M)
PLACEHOLDER = re.compile(r"\{(\w+)\}")
ATTRIBUTES = ("title", "placeholder", "aria-label")
VOID = {"br", "hr", "img", "input", "meta", "link", "source", "wbr"}


def unquote(literal):
    body = literal[1:-1]
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), body)


class Markup(HTMLParser):
    """Collects the text nodes, attributes and data-i18n-html blocks that i18n.js translates."""

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.source, self.keys, self.whole = source, set(), 0
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.whole:
            self.whole += tag not in VOID
            return
        for name in ATTRIBUTES:
            if attrs.get(name) and CYRILLIC.search(attrs[name]):
                self.keys.add(attrs[name])
        if "data-i18n-html" in attrs:
            line, column = self.getpos()
            offset = sum(len(row) for row in self.source.splitlines(True)[: line - 1]) + column
            start = self.source.index(">", offset) + 1
            self.keys.add(self.source[start : self.source.index(f"</{tag}>", start)].strip())
            self.whole = 1

    def handle_endtag(self, tag):
        if self.whole:
            self.whole -= 1

    def handle_data(self, data):
        if not self.whole and CYRILLIC.search(data):
            self.keys.add(data.strip())


def collect():
    keys, unwrapped = set(Markup((STATIC / "index.html").read_text()).keys), []
    for path in sorted(STATIC.glob("*.js")):
        if path.name == "i18n.js":
            continue
        source = COMMENT.sub("", path.read_text())
        keys.update(unquote(m.group(1)) for m in CALL.finditer(source))
        rest = CALL.sub("tr(", source)
        for number, line in enumerate(rest.splitlines(), 1):
            if CYRILLIC.search(line):
                unwrapped.append(f"{path.name}:{number}: {line.strip()[:140]}")
    return keys, unwrapped


def main():
    keys, unwrapped = collect()
    if "--keys" in sys.argv:
        print(json.dumps(sorted(keys), ensure_ascii=False, indent=1))
        return 0
    problems = [f"unwrapped: {line}" for line in unwrapped]
    for path in sorted((STATIC / "locales").glob("*.json")):
        messages = json.loads(path.read_text())
        problems += [f"{path.name}: missing {key!r}" for key in sorted(keys - messages.keys())]
        problems += [f"{path.name}: stale {key!r}" for key in sorted(messages.keys() - keys)]
        problems += [
            f"{path.name}: placeholders differ in {key!r}"
            for key in sorted(keys & messages.keys())
            if set(PLACEHOLDER.findall(key)) != set(PLACEHOLDER.findall(messages[key]))
        ]
    print("\n".join(problems) or f"{len(keys)} keys, all locales complete")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
