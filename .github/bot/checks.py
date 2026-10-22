"""Pure text checks for OE 1 (Git Workflow Solo). No network calls here."""
import re

# Bio fields every student fills in. Matched by keyword so small label
# edits ("Fun fact" vs "Fun fact about me") still count.
BIO_FIELDS = {
    "program": "Program / concentration",
    "background": "Background (one sentence)",
    "superpower": "DevOps superpower",
    "kryptonite": "DevOps kryptonite",
    "tool": "A tool I'd like to learn this term",
    "fun fact": "Fun fact",
}
TIMEZONE_KEYS = ("time zone", "timezone")

FIELD_RE = re.compile(r"^\s*[-*]\s*\*\*(?P<label>.+?)\*\*\s*:?\s*(?P<value>.*)$")
PLACEHOLDER_RE = re.compile(r"<[^>]*>")

# Teammate Bot's rule for OE 1. The key phrase is what the checker looks for,
# so students can reformat the bullet without losing credit.
BOT_RULE = 'Post a short async update every weekday, even if it is just "still working on X."'
BOT_RULE_KEY = "async update every weekday"


def _filled(value):
    v = PLACEHOLDER_RE.sub("", value or "").strip(" .:-*")
    return bool(v)


def parse_bio(text):
    """Return {'name': str|None, 'fields': {key: value}, 'timezone': value|None}."""
    name, fields, tz = None, {}, None
    for line in (text or "").splitlines():
        if line.startswith("# ") and name is None:
            name = line[2:].strip()
            continue
        m = FIELD_RE.match(line)
        if not m:
            continue
        label = m.group("label").lower().rstrip(":")
        value = m.group("value").strip()
        if any(k in label for k in TIMEZONE_KEYS):
            tz = value
            continue
        for key in BIO_FIELDS:
            if key in label and key not in fields:
                fields[key] = value
                break
    return {"name": name, "fields": fields, "timezone": tz}


def bio_problems(text, need_timezone=False):
    """List of problem codes for a bio file. Empty list = complete."""
    if text is None:
        return ["missing_file"]
    bio = parse_bio(text)
    problems = []
    if not bio["name"] or not _filled(bio["name"]) or "your name" in bio["name"].lower():
        problems.append("name")
    for key in BIO_FIELDS:
        if not _filled(bio["fields"].get(key, "")):
            problems.append(f"field:{key}")
    if need_timezone and not _filled(bio["timezone"] or ""):
        problems.append("timezone")
    return problems


def has_conflict_markers(text):
    for line in (text or "").splitlines():
        s = line.rstrip()
        if s.startswith("<<<<<<<") or s.startswith(">>>>>>>") or s == "=======":
            return True
    return False


def bullets(text):
    return [l.strip()[2:].strip() for l in (text or "").splitlines()
            if l.strip().startswith(("- ", "* "))]


def norms_problems(text):
    """Checks TEAM_NORMS.md after the OE 1 conflict is resolved."""
    if text is None:
        return ["missing_file"]
    problems = []
    if has_conflict_markers(text):
        problems.append("markers")
    if re.search(r"\bTBD\b", text):
        problems.append("tbd")
    items = bullets(text)
    has_bot = BOT_RULE_KEY in text.lower()
    own = [b for b in items if BOT_RULE_KEY not in b.lower() and _filled(b) and "tbd" not in b.lower()]
    if not has_bot:
        problems.append("missing_bot_rule")
    if not own:
        problems.append("missing_own_rule")
    if len(items) < 2:
        problems.append("not_bulleted")
    return problems


def branch_suffix(ref, prefix):
    """'bio/jdoe' -> 'jdoe' when prefix == 'bio/'; else None."""
    if ref and ref.startswith(prefix) and len(ref) > len(prefix):
        return ref[len(prefix):]
    return None


def box(done):
    return "✅" if done else "⬜"


FRIENDLY = {
    "missing_file": "I couldn't find the file I expected.",
    "name": "Replace `<Your Name>` in the heading with your name.",
    "field:program": "Fill in **Program / concentration**.",
    "field:background": "Fill in **Background (one sentence)**.",
    "field:superpower": "Fill in **DevOps superpower**.",
    "field:kryptonite": "Fill in **DevOps kryptonite**.",
    "field:tool": "Fill in **A tool I'd like to learn this term**.",
    "field:fun fact": "Fill in **Fun fact**.",
    "timezone": ("Since we'll be working asynchronously, could you add a line "
                 "`- **Time zone:** ...` so I know when you're usually online?"),
    "markers": "The file still contains conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`). Delete them.",
    "tbd": "`TBD` is still in the file. Replace it.",
    "missing_bot_rule": "My rule went missing in the merge. Please keep both rules.",
    "missing_own_rule": "I can't find your rule. Please keep both rules.",
    "not_bulleted": "Please list the rules as a bulleted list, one rule per `- ` line.",
    "conflict": "GitHub still reports a merge conflict on this pull request.",
}


def explain(problems):
    return "\n".join(f"- {FRIENDLY.get(p, p)}" for p in problems)
