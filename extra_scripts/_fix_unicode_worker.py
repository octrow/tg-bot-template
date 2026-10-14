#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Worker for fix_unicode.sh - Unicode replacements + DOC_STYLE.md rules.

Modes:
    fix         Unicode replacements only (rewrite in place).
    warn        Read-only lint. Exit 1 on any error-severity hit
                (or any hit if --style-strict).
    danger-fix  Unicode replacements + lexical rewrites (stem-aware).
                Phrases are never auto-rewritten.

Style checks load rules from --rules-file (default: style_rules.json
next to this file). --style enables them; --style-only skips Unicode.

IMPORTANT: source uses \\uXXXX escapes only - never literal typographic
chars - so running fix_unicode.sh on this file does not corrupt the tables.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# --- Unicode replacements (1:1 chars) -----------------------------------------

REPLACEMENTS = [
    ("\u201c", '"'),   # left double curly
    ("\u201d", '"'),   # right double curly
    ("\u201e", '"'),   # german low double
    ("\u275d", '"'),   # heavy left double ornament
    ("\u275e", '"'),   # heavy right double ornament
    ("\u301d", '"'),   # reversed double prime
    ("\u301e", '"'),   # double prime
    ("\u00ab", '"'),   # left guillemet
    ("\u00bb", '"'),   # right guillemet
    ("\u2018", "'"),   # left single curly
    ("\u2019", "'"),   # right single curly
    ("\u2014", "-"),   # em-dash
    ("\u2013", "-"),   # en-dash
    ("\u2192", "->"),  # right arrow
    ("\u2190", "<-"),  # left arrow
    ("\u2026", "..."), # ellipsis
    ("\u2705", "OK"),  # white heavy check mark
    ("\u00a0", " "),   # nbsp
]

UNICODE_LABELS = {
    "\u201c": "left-double-quote",
    "\u201d": "right-double-quote",
    "\u201e": "german-low-quote",
    "\u275d": "heavy-left-double-quote",
    "\u275e": "heavy-right-double-quote",
    "\u301d": "reversed-double-prime",
    "\u301e": "double-prime",
    "\u00ab": "left-guillemet",
    "\u00bb": "right-guillemet",
    "\u2018": "left-single-quote",
    "\u2019": "right-single-quote",
    "\u2014": "em-dash",
    "\u2013": "en-dash",
    "\u2192": "right-arrow",
    "\u2190": "left-arrow",
    "\u2026": "ellipsis",
    "\u2705": "checkmark-emoji",
    "\u00a0": "nbsp",
}

UNICODE_RE = re.compile("|".join(re.escape(c) for c, _ in REPLACEMENTS))
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FFFF\u2600-\u27bf]+",
    flags=re.UNICODE,
)

# Files excluded from style checks (they contain BAD examples by design).
STYLE_SKIP_BASENAMES = {
    "DOC_STYLE.md",
    "style_rules.json",
    "_fix_unicode_worker.py",
    "fix_unicode.sh",
    "fix_unicode_style_check_plan.md",
}

# --- Skip-zone tokenizer ------------------------------------------------------

_SKIP_PATTERNS = [
    re.compile(r"```.*?```", re.DOTALL),       # fenced code
    re.compile(r"`[^`\n]+`"),                   # inline code
    re.compile(r"<!--.*?-->", re.DOTALL),      # HTML comments
    re.compile(r"https?://\S+"),                # URLs
]


def _yaml_frontmatter_range(text):
    if not text.startswith("---\n"):
        return None
    m = re.match(r"^---\n.*?\n---\n", text, re.DOTALL)
    return (m.start(), m.end()) if m else None


def find_skip_ranges(text):
    ranges = []
    fm = _yaml_frontmatter_range(text)
    if fm:
        ranges.append(fm)
    for pat in _SKIP_PATTERNS:
        for m in pat.finditer(text):
            ranges.append((m.start(), m.end()))
    if not ranges:
        return []
    ranges.sort()
    merged = [ranges[0]]
    for s, e in ranges[1:]:
        ls, le = merged[-1]
        if s <= le:
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s, e))
    return merged


def in_skip(pos, ranges):
    lo, hi = 0, len(ranges)
    while lo < hi:
        mid = (lo + hi) // 2
        s, e = ranges[mid]
        if pos < s:
            hi = mid
        elif pos >= e:
            lo = mid + 1
        else:
            return True
    return False


# --- Bypass directive ---------------------------------------------------------

BYPASS_RE = re.compile(r"(?:<!--|#|//)\s*doc-style:\s*ignore", re.IGNORECASE)


def line_bounds(text, pos):
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    if end == -1:
        end = len(text)
    return start, end


def line_has_bypass(text, line_start, line_end):
    return bool(BYPASS_RE.search(text[line_start:line_end]))


def lineno_of(text, pos):
    return text.count("\n", 0, pos) + 1


# --- Style rules --------------------------------------------------------------

# Cyrillic range U+0400 - U+04FF, used as inflectional tail and word boundary.
CYRILLIC_TAIL = r"[\u0400-\u04ff]*"
WORD_CHAR = r"[\w\u0400-\u04ff]"


class LexicalRule:
    __slots__ = (
        "id", "mode", "pattern", "stem", "literal",
        "replacement", "preserve_ending", "endings",
        "context_required", "context_window", "severity",
    )

    def __init__(self, raw):
        self.id = raw.get("id", "<unnamed>")
        self.mode = raw.get("match", "word")
        self.replacement = raw.get("replacement", "")
        self.preserve_ending = bool(raw.get("preserve_ending", False))
        self.endings = {k.lower(): v for k, v in (raw.get("endings") or {}).items()}
        self.context_required = [s.lower() for s in (raw.get("context_required") or [])]
        self.context_window = int(raw.get("context_window_chars", 200))
        self.severity = raw.get("severity", "error")
        self.stem = raw.get("stem", "")
        self.literal = raw.get("literal", "")

        if self.mode == "word":
            if not self.literal:
                raise ValueError("rule %s: 'word' mode requires 'literal'" % self.id)
            self.pattern = re.compile(
                r"(?<!" + WORD_CHAR + r")"
                + re.escape(self.literal)
                + r"(?!" + WORD_CHAR + r")",
                re.IGNORECASE,
            )
        elif self.mode == "stem":
            if not self.stem:
                raise ValueError("rule %s: 'stem' mode requires 'stem'" % self.id)
            self.pattern = re.compile(
                r"(?<!" + WORD_CHAR + r")"
                + re.escape(self.stem)
                + CYRILLIC_TAIL
                + r"(?!" + WORD_CHAR + r")",
                re.IGNORECASE,
            )
        elif self.mode == "regex":
            if "regex" not in raw:
                raise ValueError("rule %s: 'regex' mode requires 'regex'" % self.id)
            self.pattern = re.compile(raw["regex"])
        else:
            raise ValueError("rule %s: unknown match mode %r" % (self.id, self.mode))

    def context_ok(self, text, start, end):
        if not self.context_required:
            return True
        win_lo = max(0, start - self.context_window)
        win_hi = min(len(text), end + self.context_window)
        window = text[win_lo:win_hi].lower()
        return any(c in window for c in self.context_required)

    def rewrite(self, match_text):
        if self.mode == "stem" and self.preserve_ending:
            suffix = match_text[len(self.stem):]
            mapped = self.endings.get(suffix.lower())
            if mapped is None:
                return None
            out = self.replacement + mapped
        else:
            out = self.replacement
        return _apply_case(out, original=match_text)


def _apply_case(out, *, original):
    if not out or not original:
        return out
    if original[0].isupper() and out[0].islower():
        return out[0].upper() + out[1:]
    return out


class PhraseRule:
    __slots__ = ("category", "pattern", "severity", "raw_text")

    def __init__(self, category, entry):
        self.category = category
        if isinstance(entry, str):
            text = entry
            self.severity = "warning"
        elif isinstance(entry, dict):
            text = entry["pattern"]
            self.severity = entry.get("severity", "warning")
        else:
            raise ValueError("phrase entry must be str or dict, got %s" % type(entry))
        self.raw_text = text
        self.pattern = re.compile(text, re.IGNORECASE)


def load_rules(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise ValueError("%s: unsupported version %r" % (path, data.get("version")))
    lexical = [LexicalRule(r) for r in data.get("lexical", [])]
    phrases = []
    for category, entries in (data.get("phrases") or {}).items():
        for entry in entries:
            phrases.append(PhraseRule(category, entry))
    return lexical, phrases


# --- Hit collection -----------------------------------------------------------

class Hit:
    __slots__ = ("kind", "lineno", "start", "end", "label", "text", "severity",
                 "replacement")

    def __init__(self, *, kind, lineno, start, end, label, text,
                 severity="error", replacement=None):
        self.kind = kind
        self.lineno = lineno
        self.start = start
        self.end = end
        self.label = label
        self.text = text
        self.severity = severity
        self.replacement = replacement


def collect_unicode_hits(text, *, strip_emoji):
    hits = []
    for m in UNICODE_RE.finditer(text):
        ch = m.group()
        hits.append(Hit(
            kind="unicode",
            lineno=lineno_of(text, m.start()),
            start=m.start(), end=m.end(),
            label=UNICODE_LABELS.get(ch, "unknown"),
            text=ch,
            severity="error",
        ))
    if strip_emoji:
        for m in EMOJI_RE.finditer(text):
            hits.append(Hit(
                kind="emoji",
                lineno=lineno_of(text, m.start()),
                start=m.start(), end=m.end(),
                label="emoji U+{:04X}".format(ord(m.group()[0])),
                text=m.group(),
                severity="error",
            ))
    return hits


def collect_style_hits(text, lexical, phrases):
    skip = find_skip_ranges(text)
    hits = []
    for rule in lexical:
        for m in rule.pattern.finditer(text):
            if in_skip(m.start(), skip):
                continue
            ls, le = line_bounds(text, m.start())
            if line_has_bypass(text, ls, le):
                continue
            if not rule.context_ok(text, m.start(), m.end()):
                continue
            replacement = rule.rewrite(m.group())
            hits.append(Hit(
                kind="lexical",
                lineno=lineno_of(text, m.start()),
                start=m.start(), end=m.end(),
                label="style:lexical:" + rule.id,
                text=m.group(),
                severity=rule.severity,
                replacement=replacement,
            ))
    for rule in phrases:
        for m in rule.pattern.finditer(text):
            if in_skip(m.start(), skip):
                continue
            ls, le = line_bounds(text, m.start())
            if line_has_bypass(text, ls, le):
                continue
            hits.append(Hit(
                kind="phrase",
                lineno=lineno_of(text, m.start()),
                start=m.start(), end=m.end(),
                label="style:" + rule.category,
                text=m.group(),
                severity=rule.severity,
            ))
    hits.sort(key=lambda h: (h.start, h.end))
    return hits


# --- Output -------------------------------------------------------------------

def print_warning(path, hit):
    print("WARNING   {}:{}  [{}]  {!r}".format(path, hit.lineno, hit.label, hit.text))


def apply_unicode_fix(text, *, strip_emoji):
    out = text
    total = 0
    for ch, repl in REPLACEMENTS:
        n = out.count(ch)
        if n:
            out = out.replace(ch, repl)
            total += n
    if strip_emoji:
        out, n = EMOJI_RE.subn("", out)
        total += n
    return out, total


def apply_lexical_fix(text, lexical):
    """Rewrite lexical hits left-to-right.

    Returns (new_text, count, skipped_hits). skipped_hits are matches the
    rule decided not to rewrite (e.g. unmapped ending) - caller should warn.
    """
    if not lexical:
        return text, 0, []

    candidates = []
    skipped = []
    skip = find_skip_ranges(text)
    for rule in lexical:
        for m in rule.pattern.finditer(text):
            if in_skip(m.start(), skip):
                continue
            ls, le = line_bounds(text, m.start())
            if line_has_bypass(text, ls, le):
                continue
            if not rule.context_ok(text, m.start(), m.end()):
                continue
            new_text = rule.rewrite(m.group())
            if new_text is None:
                skipped.append(Hit(
                    kind="lexical",
                    lineno=lineno_of(text, m.start()),
                    start=m.start(), end=m.end(),
                    label="style:lexical:" + rule.id + ":unmapped-ending",
                    text=m.group(),
                    severity="warning",
                ))
                continue
            candidates.append((m.start(), m.end(), new_text))

    candidates.sort(key=lambda c: (c[0], c[1]))
    deduped = []
    last_end = -1
    for start, end, new_text in candidates:
        if start >= last_end:
            deduped.append((start, end, new_text))
            last_end = end

    if not deduped:
        return text, 0, skipped

    parts = []
    cursor = 0
    for start, end, new_text in deduped:
        parts.append(text[cursor:start])
        parts.append(new_text)
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts), len(deduped), skipped


# --- Main ---------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("mode", choices=["fix", "warn", "danger-fix"])
    p.add_argument("path")
    p.add_argument("--strip-emoji", action="store_true")
    p.add_argument("--style", action="store_true")
    p.add_argument("--style-only", action="store_true")
    p.add_argument("--style-strict", action="store_true")
    p.add_argument("--rules-file", default="")
    return p.parse_args(argv)


def default_rules_path():
    return Path(__file__).resolve().parent / "style_rules.json"


def is_style_skipped(path):
    return path.name in STYLE_SKIP_BASENAMES


def main(argv):
    args = parse_args(argv)
    file_path = Path(args.path)

    try:
        original = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
        return 0

    style_enabled = args.style or args.style_only or args.mode == "danger-fix"
    style_active = style_enabled and not is_style_skipped(file_path)

    lexical = []
    phrases = []
    if style_active:
        rules_path = Path(args.rules_file) if args.rules_file else default_rules_path()
        if not rules_path.exists():
            print("ERROR     rules file not found: %s" % rules_path, file=sys.stderr)
            return 2
        try:
            lexical, phrases = load_rules(rules_path)
        except (ValueError, json.JSONDecodeError) as exc:
            print("ERROR     %s: %s" % (rules_path, exc), file=sys.stderr)
            return 2

    do_unicode = not args.style_only

    if args.mode == "warn":
        hits = []
        if do_unicode:
            hits.extend(collect_unicode_hits(original, strip_emoji=args.strip_emoji))
        if style_active:
            hits.extend(collect_style_hits(original, lexical, phrases))
        hits.sort(key=lambda h: (h.lineno, h.start))
        for h in hits:
            print_warning(str(file_path), h)
        if not hits:
            return 0
        if args.style_strict:
            return 1
        return 1 if any(h.severity == "error" for h in hits) else 0

    text = original
    total = 0
    if do_unicode:
        text, n = apply_unicode_fix(text, strip_emoji=args.strip_emoji)
        total += n

    if args.mode == "danger-fix" and style_active:
        text, n, skipped = apply_lexical_fix(text, lexical)
        total += n
        for h in skipped:
            print_warning(str(file_path), h)

    if text != original:
        file_path.write_text(text, encoding="utf-8")
        print("MODIFIED  {}  ({} replacement(s))".format(file_path, total))
    else:
        print("UNCHANGED {}".format(file_path))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
