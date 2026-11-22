"""
Second Brain - a meeting-prep assistant for a manager's scattered notes.

Run it with:  python app.py   then open http://localhost:5050

The one rule: your knowledge stays yours.
  * On device: the AI is a model running on this computer (Ollama). The app
    blocks every network connection that isn't to this computer, and logs
    any attempt, so nothing it reads can leave the device.
  * No copies: notes stay in your own folders. The app reads them when you
    ask a question and keeps nothing of its own: no database, no cache file,
    no history. Photo readings are held in memory only until you press
    "Forget" or stop the app.
  * You choose: you pick which folders it can see, can hide any folder or
    file, or pause it completely, and change your mind at any time.

What it does:
  * Prepare for a meeting: type the meeting (e.g. "Leeds site review") and get
    a one-page brief from your notes: key points, decisions, open actions,
    what has changed since, and what your notes don't tell you. Every line
    shows the exact words it came from, and the app checks those words
    really are in your note.
  * Ask: find something you know you wrote down ("what did we decide about
    Bristol?"). If it isn't in your notes, it says so.
  * Connect: spots links between notes from different places and dates.
  * Capture: jot a note or snap a photo of handwriting; it's saved as a plain
    text file in YOUR notes folder, not inside the app.

Works with several kinds of notes: .md / .txt notes, photos of handwriting and
screenshots (.jpg .png), saved links (.url .webloc) and calendar reminders (.ics).
"""

import base64
import ipaddress
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. The network lock. This runs before anything else is imported or started.
# ---------------------------------------------------------------------------
# Every outgoing connection in this program goes through socket.connect and
# every hostname lookup goes through socket.getaddrinfo. We wrap both so that
# only this computer (127.0.0.1 / ::1 / localhost) can be reached. Anything
# else is refused and written to the privacy log, which you can see in the app.

AUDIT = []            # in-memory log of what the app did (never written to disk)
AUDIT_MAX = 300
_audit_lock = threading.Lock()
START_TIME = time.time()


def audit(kind, detail):
    with _audit_lock:
        AUDIT.append({"time": datetime.now().strftime("%H:%M:%S"), "kind": kind, "detail": detail})
        del AUDIT[:-AUDIT_MAX]


def _is_local_host(host):
    if host in (None, "", "localhost", "0.0.0.0", "::"):
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host).split("%")[0]
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host.lower() == "localhost" or host.lower().endswith(".localhost")
    return ip.is_loopback or ip.is_unspecified


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


def _check_address(sock, address):
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        return  # e.g. local unix sockets
    host = address[0] if isinstance(address, tuple) else address
    if not _is_local_host(host):
        audit("blocked", f"Blocked a connection to {host}:{address[1] if isinstance(address, tuple) else ''} (not this computer)")
        raise PermissionError(f"Second Brain blocks connections that leave this computer ({host}).")


def _guarded_connect(self, address):
    _check_address(self, address)
    if self.family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple):
        audit("connect", f"Connected to {address[0]}:{address[1]} (this computer)")
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    _check_address(self, address)
    return _real_connect_ex(self, address)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if not _is_local_host(host):
        audit("blocked", f"Blocked a lookup of '{host}' (not this computer)")
        raise socket.gaierror(f"Second Brain blocks lookups of outside addresses ({host}).")
    return _real_getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
socket.getaddrinfo = _guarded_getaddrinfo

# Now it's safe to import the rest.
from flask import Flask, Response, jsonify, render_template, request, send_file  # noqa: E402

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# 2. Settings
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5vl:7b")
OLLAMA_CONTEXT = int(os.environ.get("OLLAMA_CONTEXT", "16384"))
DEMO_MODE = os.environ.get("DEMO_MODE", "").lower() in ("1", "true", "yes")
PHONE_ACCESS = os.environ.get("PHONE_ACCESS", "").lower() in ("1", "true", "yes")

if not _is_local_host(urllib.parse.urlparse(OLLAMA_URL).hostname):
    raise SystemExit(
        f"OLLAMA_URL must point at this computer (localhost), not {OLLAMA_URL}.\n"
        "Second Brain only talks to an AI running on your own device."
    )

TEXT_TYPES = {".md", ".txt", ".markdown"}
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}
LINK_TYPES = {".url", ".webloc"}
CAL_TYPES = {".ics"}
ALL_TYPES = TEXT_TYPES | set(IMAGE_TYPES) | LINK_TYPES | CAL_TYPES
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules"}
MAX_NOTE_CHARS = 6000
MAX_CONTEXT_CHARS = int(os.environ.get("MAX_CONTEXT_CHARS", "24000"))
MAX_UPLOAD_MB = 10
OLD_AFTER_DAYS = 14

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


def today():
    return date.today()


# ---------------------------------------------------------------------------
# 3. What the assistant can see (kept in memory only; you change it in the app)
# ---------------------------------------------------------------------------

def _initial_folders():
    raw = os.environ.get("NOTES_FOLDERS", "").strip()
    folders = [p.strip() for p in raw.split(",") if p.strip()] or [str(BASE_DIR / "sample-notes")]
    out = []
    for p in folders:
        path = Path(os.path.expanduser(p)).resolve()
        if path.is_dir():
            out.append(path)
        else:
            print(f"  NOTE: folder not found, skipping: {path}")
    return out


ACCESS = {
    "roots": _initial_folders(),   # folders you've let it see
    "hidden": set(),               # "root#/relative/path" keys you've switched off
    "paused": False,               # True = it can see nothing at all
}
_access_lock = threading.Lock()

# Photo readings live here, in memory, so the same photo isn't re-read for every
# question. They vanish when you press "Forget" or stop the app.
MEMORY = {"image_text": {}}
_memory_lock = threading.Lock()


def _key(root_idx, rel):
    return f"{root_idx}#{rel}"


def _resolve_key(key):
    """Turn a key back into a real path, making sure it stays inside a chosen folder."""
    try:
        idx_s, rel = key.split("#", 1)
        root = ACCESS["roots"][int(idx_s)]
    except (ValueError, IndexError):
        return None, None
    path = (root / rel).resolve() if rel else root
    try:
        path.relative_to(root)
    except ValueError:
        return None, None
    return root, path


def _is_hidden(root_idx, rel):
    parts = rel.split("/") if rel else []
    for i in range(len(parts) + 1):
        if _key(root_idx, "/".join(parts[:i])) in ACCESS["hidden"]:
            return True
    return False


def _walk(root):
    """All note files under root, as relative paths, skipping hidden/system files."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith((".", "_")))
        for f in sorted(filenames):
            if f.startswith((".", "_")) or Path(f).suffix.lower() not in ALL_TYPES:
                continue
            out.append(Path(dirpath, f).relative_to(root).as_posix())
    return out


def kind_of(rel):
    ext = Path(rel).suffix.lower()
    if ext in IMAGE_TYPES:
        return "screenshot" if ("screenshot" in rel.lower() or ext == ".png") else "photo"
    if ext in LINK_TYPES:
        return "link"
    if ext in CAL_TYPES:
        return "reminder"
    return "note"


def access_tree():
    """What the 'What I can see' panel shows."""
    roots = []
    with _access_lock:
        for idx, root in enumerate(ACCESS["roots"]):
            files = _walk(root)
            folders = {}
            for rel in files:
                folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
                folders.setdefault(folder, []).append(rel)
            roots.append({
                "key": _key(idx, ""), "name": root.name, "path": str(root),
                "visible": not _is_hidden(idx, ""),
                "folders": [
                    {
                        "key": _key(idx, folder), "name": folder or "(top level)", "is_top": folder == "",
                        "visible": not _is_hidden(idx, folder) if folder else not _is_hidden(idx, ""),
                        "files": [{"key": _key(idx, rel), "name": rel.rsplit("/", 1)[-1], "kind": kind_of(rel),
                                   "visible": not _is_hidden(idx, rel)} for rel in rels],
                    }
                    for folder, rels in sorted(folders.items())
                ],
            })
        return {"paused": ACCESS["paused"], "roots": roots}


# ---------------------------------------------------------------------------
# 4. Reading notes (fresh from disk every time; nothing is saved)
# ---------------------------------------------------------------------------

DATE_IN_NAME = re.compile(r"(20\d\d)-(\d\d)-(\d\d)")


def _date_from_name(name):
    m = DATE_IN_NAME.search(name)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _ics_date(value):
    m = re.match(r"(\d{4})(\d\d)(\d\d)", value.strip())
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _parse_ics(text):
    items, cur = [], None
    for line in text.replace("\r\n ", "").splitlines():
        line = line.strip()
        if line in ("BEGIN:VEVENT", "BEGIN:VTODO"):
            cur = {"type": "Calendar event" if "EVENT" in line else "Reminder / to-do"}
        elif line in ("END:VEVENT", "END:VTODO") and cur is not None:
            items.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            k, v = line.split(":", 1)
            cur[k.split(";")[0]] = v.replace("\\n", "\n").replace("\\,", ",")
    return items


def _link_text(path):
    raw = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".url":
        m = re.search(r"^URL=(.+)$", raw, re.M)
    else:
        m = re.search(r"<string>(.+?)</string>", raw, re.S)
    url = m.group(1).strip() if m else "(no address found)"
    return f"Saved link: {path.stem}\nAddress: {url}"


class AIError(Exception):
    """A problem talking to the AI, with a message that's safe to show the user."""


def _read_image_text(key, path):
    st = path.stat()
    cache_key = (key, st.st_mtime, st.st_size)
    with _memory_lock:
        if cache_key in MEMORY["image_text"]:
            return MEMORY["image_text"][cache_key]
    if DEMO_MODE:
        text = demo_image_text(path.name)
    else:
        media = IMAGE_TYPES[path.suffix.lower()]
        prompt = TRANSCRIBE_PROMPT.replace("{today}", today().strftime("%A %Y-%m-%d"))
        result = ask_ai_for_json(prompt, TRANSCRIBE_SCHEMA, image=(path.read_bytes(), media))
        text = result.get("text", "").strip() or "(no readable text in this image)"
    audit("read", f"Read the {kind_of(path.name)} '{path.name}' on this computer (kept in memory only)")
    with _memory_lock:
        MEMORY["image_text"][cache_key] = text
    return text


def load_visible_notes(read_images=True):
    """Read every note the assistant is allowed to see. Returns a list of dicts."""
    with _access_lock:
        if ACCESS["paused"]:
            return [], {"paused": True, "hidden": 0}
        roots = list(enumerate(ACCESS["roots"]))
        hidden_count = 0
        visible = []
        for idx, root in roots:
            for rel in _walk(root):
                if _is_hidden(idx, rel):
                    hidden_count += 1
                else:
                    visible.append((idx, root, rel))
    notes = []
    for idx, root, rel in visible:
        path = root / rel
        key = _key(idx, rel)
        kind = kind_of(rel)
        try:
            mtime = date.fromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
        base = {"key": key, "path": f"{root.name}/{rel}", "name": Path(rel).stem, "kind": kind}
        try:
            if kind == "reminder":
                for i, ev in enumerate(_parse_ics(path.read_text(encoding="utf-8", errors="replace"))):
                    when = _ics_date(ev.get("DTSTART") or ev.get("DUE") or "")
                    text = f"{ev['type']}: {ev.get('SUMMARY', '(no title)')}"
                    if when:
                        text += f"\nWhen / due: {when.strftime('%A %d %B %Y')}"
                    if ev.get("DESCRIPTION"):
                        text += f"\nDetails: {ev['DESCRIPTION']}"
                    notes.append(dict(base, key=f"{key}@{i}", name=ev.get("SUMMARY", "Reminder"), text=text,
                                      date=(when or mtime).isoformat(), date_basis="calendar date" if when else "file last changed"))
                continue
            if kind == "link":
                text = _link_text(path)
            elif kind in ("photo", "screenshot"):
                if not read_images:
                    continue
                text = _read_image_text(key, path)
            else:
                text = path.read_text(encoding="utf-8", errors="replace")
        except AIError:
            raise
        except OSError:
            continue
        named = _date_from_name(Path(rel).name)
        notes.append(dict(base, text=text[:MAX_NOTE_CHARS],
                          date=(named or mtime).isoformat(),
                          date_basis="date in file name" if named else "file last changed (no date written)"))
    notes.sort(key=lambda n: (n["date"], n["path"]))
    for i, n in enumerate(notes, 1):
        n["id"] = f"N{i}"
        n["age_days"] = (today() - date.fromisoformat(n["date"])).days
    if notes:
        audit("read", f"Read {len(notes)} notes from your folders to answer (nothing saved)")
    return notes, {"paused": False, "hidden": hidden_count}


def pick_for_context(notes, query):
    """Fit notes into what a local model can read at once. Prefer ones that mention the query."""
    total = sum(len(n["text"]) for n in notes)
    if total <= MAX_CONTEXT_CHARS:
        return notes, []
    words = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2}

    def score(n):
        blob = (n["name"] + " " + n["text"]).lower()
        return (sum(w in blob for w in words), n["date"])

    chosen, used = [], 0
    for n in sorted(notes, key=score, reverse=True):
        if used + len(n["text"]) <= MAX_CONTEXT_CHARS:
            chosen.append(n)
            used += len(n["text"])
    left_out = [n for n in notes if n not in chosen]
    chosen.sort(key=lambda n: (n["date"], n["path"]))
    return chosen, left_out


def notes_listing(notes):
    return "\n\n".join(
        f"[{n['id']}] {n['kind']} | {n['name']} | dated {n['date']} ({n['age_days']} days ago)\n{n['text']}"
        for n in notes
    )


# ---------------------------------------------------------------------------
# 5. Talking to the local AI (Ollama on this computer)
# ---------------------------------------------------------------------------

# No proxies: requests go straight to Ollama on this machine.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def ask_ai_for_json(prompt, schema, image=None):
    message = {"role": "user", "content": prompt}
    if image:
        message["images"] = [base64.b64encode(image[0]).decode("ascii")]
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [message],
        "format": schema,  # Ollama "structured outputs": forces JSON in this shape
        "stream": False,
        "options": {"temperature": 0, "num_ctx": OLLAMA_CONTEXT},
    }
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with _opener.open(req, timeout=900) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        if exc.code == 404 or "not found" in detail.lower():
            raise AIError(f"Ollama doesn't have the model '{OLLAMA_MODEL}' yet. In Terminal run:  ollama pull {OLLAMA_MODEL}") from exc
        raise AIError(f"Ollama returned an error: {detail[:300]}") from exc
    except (urllib.error.URLError, ConnectionError, PermissionError) as exc:
        raise AIError("Can't reach Ollama. Make sure the Ollama app is open (or run 'ollama serve' in Terminal).") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise AIError("The local AI took too long to answer. Try hiding some folders or using a smaller model.") from exc
    text = body.get("message", {}).get("content", "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AIError("The local AI returned something unexpected. Please try again.") from exc


TRANSCRIBE_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}, "unclear_words": {"type": "array", "items": {"type": "string"}}},
    "required": ["text", "unclear_words"],
}

TRANSCRIBE_PROMPT = """This image is one of a manager's notes: a photo of handwriting, a whiteboard or a screenshot.

Write out ALL the text in it, exactly as written, line by line. Keep bullet points as lines starting with "- ".
Include who sent a message and when, if the screenshot shows it.
Fix nothing except obvious spelling. If a word is hard to read, write your best guess followed by [?] and add it to unclear_words.
If there is no text, return an empty string. Today is {today}."""

ITEM = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "source": {"type": "string"},
        "quote": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "low"]},
    },
    "required": ["text", "source", "quote", "confidence"],
}
ACTION = {
    "type": "object",
    "properties": dict(ITEM["properties"], owner={"type": "string"}, due={"type": "string"},
                       status={"type": "string", "enum": ["open", "done", "unclear"]}),
    "required": ITEM["required"] + ["owner", "due", "status"],
}
CHANGE = {
    "type": "object",
    "properties": {"text": {"type": "string"}, "old_source": {"type": "string"},
                   "new_source": {"type": "string"}, "quote": {"type": "string"}},
    "required": ["text", "old_source", "new_source", "quote"],
}

BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "headline": {"type": "string"},
        "key_points": {"type": "array", "items": ITEM},
        "decisions": {"type": "array", "items": ITEM},
        "actions": {"type": "array", "items": ACTION},
        "changed": {"type": "array", "items": CHANGE},
        "gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["relevant", "headline", "key_points", "decisions", "actions", "changed", "gaps"],
}

RULES = """Rules you must follow:
- Use ONLY the notes below. Never use outside knowledge. Never invent names, numbers or dates.
- Every item cites exactly ONE note id in "source" (like "N3") and copies a short quote (3 to 15 words) from that note that supports it. Copy the quote character for character, typos included.
- confidence is "high" only if the note says it plainly. Use "low" if you are inferring, if the note is a fragment, unfinished or has a "?" or "??", or if notes disagree.
- Notes are messy and some are out of date. If a later note changes or contradicts an earlier one, go with the later note and explain the change in "changed" (old_source = earlier note, new_source = later note, quote from the later note)."""

BRIEF_PROMPT = """You help a busy manager prepare for a meeting in two minutes, using their own scattered notes.
Today is {today}. The meeting: "{meeting}"

{rules}
- headline: one sentence on the most important thing to walk in knowing.
- key_points: the 3 to 6 facts that matter most for THIS meeting (latest numbers, status, risks).
- decisions: decisions already made that are relevant, latest version only.
- actions: action items relevant to this meeting. owner and due exactly as written ("" if not written; due as YYYY-MM-DD if a date is given). status "done" only if a later note shows it happened, otherwise "open" (or "unclear").
- gaps: specific things the manager will probably need for this meeting that the notes do NOT answer, phrased as questions to ask. Do not make up answers to them.
- Leave out notes that have nothing to do with this meeting. If no note is relevant, set relevant to false and leave the lists empty.

Notes (oldest first, each with how many days old it is):
{notes}"""

ASK_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "answer": {"type": "string"},
        "points": {"type": "array", "items": ITEM},
        "gaps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["found", "answer", "points", "gaps"],
}

ASK_PROMPT = """You help a manager find things they wrote down in their own notes. Today is {today}.

{rules}
- answer: a short plain answer (1 to 3 sentences). Mention dates like "On 3 Nov you noted...". If notes disagree, say so and give the latest.
- points: the evidence for the answer, one item per supporting note.
- If the notes do not contain the answer, set found to false, points to [], and say plainly that it isn't in the notes. Do NOT guess.
- gaps: anything the question asks that the notes don't cover.

Conversation so far:
{history}

Notes (oldest first):
{notes}

Question: {question}"""

CONNECT_SCHEMA = {
    "type": "object",
    "properties": {
        "threads": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "summary": {"type": "string"},
                           "sources": {"type": "array", "items": {"type": "string"}},
                           "next_step": {"type": "string"}},
            "required": ["name", "summary", "sources", "next_step"]}},
        "links": {"type": "array", "items": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "source_a": {"type": "string"},
                           "source_b": {"type": "string"}},
            "required": ["text", "source_a", "source_b"]}},
    },
    "required": ["threads", "links"],
}

CONNECT_PROMPT = """Below are a manager's scattered notes from different places and dates. Join the dots. Today is {today}.

- threads: group notes into the 2 to 5 running threads of work they belong to. For each give a name, a 1 to 2 sentence summary of where it stands NOW (latest notes win), the note ids, and the obvious next step ("" if none).
- links: up to 5 non-obvious connections between two notes from different places or dates that the manager might have missed (e.g. a delay in one note that affects a plan in another). Explain each in one sentence.
- Use ONLY the notes. Use note ids exactly as given. Never invent facts.

Notes:
{notes}"""


# ---------------------------------------------------------------------------
# 6. Checking the AI's work (so it can't make things up unnoticed)
# ---------------------------------------------------------------------------

def _norm(s):
    return " ".join(re.findall(r"[a-z0-9£$€%]+", s.lower()))


def check_quote(quote, source_text):
    """Is the quote really in the note? Returns 'exact', 'close' or None."""
    q, src = _norm(quote), _norm(source_text)
    if not q:
        return None
    if q in src:
        return "exact"
    qt = q.split()
    st = set(src.split())
    if len(qt) >= 3 and sum(t in st for t in qt) / len(qt) >= 0.8:
        return "close"
    return None


def _numbers(s):
    return set(re.findall(r"\d+(?:\.\d+)?", s))


def verify_item(item, by_id, src_field="source", also=None):
    """Attach the real note to an item and check its quote and numbers. None = drop it.

    `also` is an extra note whose numbers count too (the older note in a "changed" item)."""
    note = by_id.get(str(item.get(src_field, "")).strip().strip("[]"))
    if not note:
        return None
    match = check_quote(item.get("quote", ""), note["text"])
    warnings = []
    if not match:
        warnings.append("I couldn't find these exact words in the note, so treat this with care.")
    allowed = set()
    for n in (note, also):
        if n:
            allowed |= _numbers(n["text"]) | _numbers(n["name"]) | _numbers(n["date"])
    missing = _numbers(item.get("text", "")) - allowed
    if missing:
        warnings.append("Contains a number (" + ", ".join(sorted(missing)) + ") that isn't in the note.")
    out = dict(item)
    out["source"] = note["id"]
    out["verified"] = bool(match) and not missing
    out["warnings"] = warnings
    if warnings:
        out["confidence"] = "low"
    return out


def source_card(n):
    age = n["age_days"]
    return {
        "id": n["id"], "key": n["key"], "path": n["path"], "name": n["name"], "kind": n["kind"],
        "date": n["date"], "date_basis": n["date_basis"], "age_days": age,
        "old": age > OLD_AFTER_DAYS, "text": n["text"],
    }


KIND_WORDS = {"note": ("typed note", "typed notes"), "photo": ("photo of handwriting", "photos of handwriting"),
              "screenshot": ("screenshot", "screenshots"), "link": ("saved link", "saved links"),
              "reminder": ("calendar reminder", "calendar reminders")}


def explain_sources(used, all_notes, meta, left_out):
    """A plain-English sentence on where the help came from."""
    if meta.get("paused"):
        return "I'm paused, so I can't see any of your notes."
    counts = {}
    for n in used:
        counts[n["kind"]] = counts.get(n["kind"], 0) + 1
    parts = [f"{c} {KIND_WORDS[k][0 if c == 1 else 1]}" for k, c in counts.items()]
    if not used:
        text = f"I looked through {len(all_notes)} of your notes and none of them answered this."
    else:
        text = (f"Built from {len(used)} of the {len(all_notes)} notes I can see: {', '.join(parts)}. "
                "Everything was read on this computer by the local AI, and nothing was saved.")
    imgs = sum(1 for n in used if n["kind"] in ("photo", "screenshot"))
    if imgs:
        text += f" The {'image was' if imgs == 1 else f'{imgs} images were'} turned into text on this computer first."
    if meta.get("hidden"):
        text += f" {meta['hidden']} file{'s' if meta['hidden'] != 1 else ''} you've hidden {'were' if meta['hidden'] != 1 else 'was'} not looked at."
    if left_out:
        text += f" {len(left_out)} note{'s' if len(left_out) != 1 else ''} didn't fit in the AI's reading limit and {'were' if len(left_out) != 1 else 'was'} skipped."
    old = [n for n in used if n["age_days"] > OLD_AFTER_DAYS]
    if old:
        text += f" {len(old)} of the notes used {'are' if len(old) != 1 else 'is'} over {OLD_AFTER_DAYS} days old, so check {'they are' if len(old) != 1 else 'it is'} still true."
    return text


def finish(result_items, notes, meta, left_out, extra_ids=()):
    """Collect the notes actually cited, and the plain-language explanation."""
    by_id = {n["id"]: n for n in notes}
    ids = []
    for it in result_items:
        for f in ("source", "old_source", "new_source", "source_a", "source_b"):
            if it.get(f) in by_id and it[f] not in ids:
                ids.append(it[f])
    for i in extra_ids:
        if i in by_id and i not in ids:
            ids.append(i)
    used = sorted((by_id[i] for i in ids), key=lambda n: (n["date"], n["path"]))
    return [source_card(n) for n in used], explain_sources(used, notes, meta, left_out)


def _parse_due(s):
    try:
        return datetime.strptime((s or "").strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def build_brief(meeting):
    notes, meta = load_visible_notes()
    if not notes:
        return {"empty": True, "explain": explain_sources([], [], meta, [])}
    chosen, left_out = pick_for_context(notes, meeting)
    if DEMO_MODE:
        raw = demo_brief(meeting, chosen)
    else:
        prompt = (BRIEF_PROMPT.replace("{rules}", RULES).replace("{today}", today().strftime("%A %d %B %Y"))
                  .replace("{meeting}", meeting).replace("{notes}", notes_listing(chosen)))
        raw = ask_ai_for_json(prompt, BRIEF_SCHEMA)
    by_id = {n["id"]: n for n in chosen}
    out = {"headline": raw.get("headline", ""), "gaps": [g for g in raw.get("gaps", []) if g.strip()]}
    for section in ("key_points", "decisions", "actions"):
        out[section] = [v for v in (verify_item(i, by_id) for i in raw.get(section, [])) if v]
    for a in out["actions"]:
        due = _parse_due(a.get("due"))
        a["overdue"] = bool(due and due < today() and a.get("status") != "done")
    out["changed"] = []
    for c in raw.get("changed", []):
        if c.get("old_source") in by_id and c.get("new_source") in by_id:
            v = verify_item(dict(c, source=c["new_source"]), by_id, also=by_id[c["old_source"]])
            if v:
                v.pop("source", None)
                out["changed"].append(v)
    items = out["key_points"] + out["decisions"] + out["actions"] + out["changed"]
    if not raw.get("relevant", True) or not items:
        out["nothing_found"] = True
        out["headline"] = f"I couldn't find anything in your notes about “{meeting}”."
    if items and not any(i["verified"] for i in items):
        out["headline"] = "I'm not sure about this one: I couldn't match any of it to the exact words in your notes."
    out["sources"], out["explain"] = finish(items, chosen, meta, left_out)
    out["meeting"] = meeting
    return out


def build_answer(question, history):
    notes, meta = load_visible_notes()
    if not notes:
        return {"found": False, "answer": explain_sources([], [], meta, []), "points": [], "gaps": [], "sources": [], "explain": ""}
    chosen, left_out = pick_for_context(notes, question)
    if DEMO_MODE:
        raw = demo_ask(question, chosen)
    else:
        convo = "\n".join(
            f"{'User' if h.get('role') == 'user' else 'Assistant'}: {str(h.get('text', ''))[:800]}"
            for h in history[-6:] if isinstance(h, dict)
        ) or "(none)"
        prompt = (ASK_PROMPT.replace("{rules}", RULES).replace("{today}", today().strftime("%A %d %B %Y"))
                  .replace("{history}", convo).replace("{notes}", notes_listing(chosen)).replace("{question}", question))
        raw = ask_ai_for_json(prompt, ASK_SCHEMA)
    by_id = {n["id"]: n for n in chosen}
    points = [v for v in (verify_item(p, by_id) for p in raw.get("points", [])) if v]
    out = {"found": bool(raw.get("found")) and bool(points), "answer": raw.get("answer", ""),
           "points": points, "gaps": [g for g in raw.get("gaps", []) if g.strip()]}
    if not points:
        # Safety net: an answer with no real note behind it is never shown.
        out["found"] = False
        out["answer"] = "I couldn't find that in your notes, so I won't guess."
    elif not any(p["verified"] for p in points):
        out["unsure"] = True
    out["sources"], out["explain"] = finish(points, chosen, meta, left_out)
    return out


def build_connections():
    notes, meta = load_visible_notes()
    if not notes:
        return {"threads": [], "links": [], "sources": [], "explain": explain_sources([], [], meta, [])}
    chosen, left_out = pick_for_context(notes, "")
    if DEMO_MODE:
        raw = demo_connect(chosen)
    else:
        prompt = CONNECT_PROMPT.replace("{today}", today().strftime("%A %d %B %Y")).replace("{notes}", notes_listing(chosen))
        raw = ask_ai_for_json(prompt, CONNECT_SCHEMA)
    by_id = {n["id"]: n for n in chosen}
    threads = []
    for t in raw.get("threads", []):
        t["sources"] = [s for s in dict.fromkeys(t.get("sources", [])) if s in by_id]
        if t["sources"]:
            threads.append(t)
    links = [l for l in raw.get("links", []) if l.get("source_a") in by_id and l.get("source_b") in by_id and l["source_a"] != l["source_b"]]
    ids = [s for t in threads for s in t["sources"]]
    sources, explain = finish(links, chosen, meta, left_out, extra_ids=ids)
    return {"threads": threads, "links": links, "sources": sources, "explain": explain}


# ---------------------------------------------------------------------------
# 7. Demo mode (DEMO_MODE=1): example results with no AI, for trying the app.
#    The same checking and "where this came from" code runs on these results.
# ---------------------------------------------------------------------------

DEMO_IMAGES = {
    "trial site mtg": "Tues - trial site mtg\n- Leeds recruitment behind!! 12/40\n- ask Priya re extra nurse cover\n- next check-in Fri 28th\n\nHackathon pitch\n- demo: photo of notes -> board\n- keep it to 3 min\n- Sam doing slides\n\ncall supplier re delayed batch before Thurs",
    "marcus message": "Marcus Hale (Finance) - Thu 12 Nov 14:02\nHi Jordan - approved the agency nurse budget for Leeds, £16k (not 18) to end of March. Priya can book from 1 Dec.\nPO number to follow next week.",
}


def demo_image_text(filename):
    low = filename.lower()
    for k, v in DEMO_IMAGES.items():
        if k in low:
            return v
    return "(Demo mode: this image would be read by the local AI.)"


def _find(notes, name_part):
    for n in notes:
        if name_part.lower() in (n["name"] + " " + n["path"]).lower():
            return n["id"]
    return None


def _demo_items(notes, specs):
    out = []
    for spec in specs:
        sid = _find(notes, spec[0])
        if sid:
            item = {"source": sid, "quote": spec[1], "text": spec[2], "confidence": spec[3]}
            if len(spec) > 4:
                item.update(spec[4])
            out.append(item)
    return out


def demo_brief(meeting, notes):
    words = ("leeds", "okafor", "priya", "site", "recruit", "trial", "review", "nurse")
    if not any(w in meeting.lower() for w in words):
        return {"relevant": False, "headline": "", "key_points": [], "decisions": [], "actions": [], "changed": [], "gaps": []}
    key_points = _demo_items(notes, [
        ("Leeds site call", "recruitment now 12/40", "Leeds is at 12 of 30 patients (latest count, 3 Nov). The target was lowered from 40.", "high"),
        ("Marcus message", "approved the agency nurse budget for Leeds", "Finance approved the agency nurse budget: £16k to end of March. Priya can book from 1 Dec.", "high"),
        ("supplier call", "now arriving 2 Dec (was 20 Nov)", "The delayed batch of recruitment kits now arrives 2 Dec. Leeds has about 6 kits left, roughly 1.5 weeks' worth.", "high"),
        ("trial site mtg", "next check-in Fri 28th", "Your handwritten note says the next check-in is Friday 28th.", "low"),
        ("1-1 Priya", "approx 1 in 3 failing on bloods", "About 1 in 3 patients are failing screening on blood tests.", "low"),
    ])
    decisions = _demo_items(notes, [
        ("Leeds site call", "they AGREED to drop the target to 30 by end of Dec", "Target is now 30 patients by end of December, agreed with the sponsor.", "high"),
        ("Leeds site call", "DECISION: start Bristol site setup now", "Start setting up Bristol as a second site now, not after November.", "high"),
    ])
    actions = _demo_items(notes, [
        ("1-1 Priya", "ACTION me: email Marcus re nurse budget by 5 Nov", "Email Marcus about the nurse budget", "high",
         {"owner": "me", "due": "2026-11-05", "status": "done"}),
        ("1-1 Priya", "ACTION Priya: send screen-fail breakdown", "Priya to send the screen-fail breakdown", "high",
         {"owner": "Priya", "due": "", "status": "open"}),
        ("Leeds site call", "me: tell Sam new target is 30", "Tell Sam the new target is 30 so the dashboard is right", "low",
         {"owner": "me", "due": "", "status": "unclear"}),
        ("Leeds site call", "Dr Okafor: send training slides", "Dr Okafor to send protocol training slides for Bristol", "high",
         {"owner": "Dr Okafor", "due": "", "status": "open"}),
        ("Q4 recruitment numbers", "Send Q4 recruitment numbers to Sam", "Send Q4 recruitment numbers to Sam", "high",
         {"owner": "me", "due": "2026-11-24", "status": "open"}),
    ])
    changed = []
    a, b = _find(notes, "team standup"), _find(notes, "Leeds site call")
    if a and b:
        changed.append({"old_source": a, "new_source": b, "quote": "drop the target to 30 by end of Dec (was 40)",
                        "text": "On 20 Oct the target was 40 patients by end of December. On 3 Nov the sponsor lowered it to 30."})
        changed.append({"old_source": a, "new_source": b, "quote": "DECISION: start Bristol site setup now, don't wait for Nov numbers",
                        "text": "On 20 Oct you decided to wait for November numbers before opening Bristol. On 3 Nov that changed to start now."})
    a, b = _find(notes, "1-1 Priya"), _find(notes, "Marcus message")
    if a and b:
        changed.append({"old_source": a, "new_source": b, "quote": "£16k (not 18) to end of March",
                        "text": "The nurse cost was estimated at about £18k; the approved budget is £16k."})
    a, b = _find(notes, "draft update to Sam"), _find(notes, "Leeds site call")
    if a and b:
        changed.append({"old_source": a, "new_source": b, "quote": "they AGREED to drop the target to 30",
                        "text": "Your unfinished draft to Sam still says 12 of 40. The target is now 30, so update it before sending."})
    gaps = [
        "Has Priya sent the screen-fail breakdown yet? Nothing in your notes says so.",
        "Is the check-in on Friday 27th or the 28th? Your photo says “Fri 28th” but your calendar has Friday 27 November.",
        "Can Bristol lend kits to Leeds before 2 Dec? Your note asks this but there's no answer yet.",
        "There are no recruitment numbers after 3 Nov. Ask for the latest count.",
    ]
    return {"relevant": True,
            "headline": "Leeds is at 12 of a new target of 30, nurse budget is approved, but kits could run out before the delayed batch arrives on 2 Dec.",
            "key_points": key_points, "decisions": decisions, "actions": actions, "changed": changed, "gaps": gaps}


STOP = {"what", "about", "have", "the", "any", "notes", "did", "are", "there", "with", "for", "and", "how", "who", "when",
        "this", "that", "from", "show", "all", "tell", "decide", "decided", "was", "were", "does", "say", "said", "need", "know", "our", "much", "many", "where", "which", "why"}


def demo_ask(question, notes):
    """Demo only: a note matches if it mentions every key word of the question."""
    words = [w[:4] for w in re.findall(r"[a-z0-9]+", question.lower()) if len(w) > 2 and w not in STOP]
    points = []
    for n in notes:
        blob = (n["name"] + " " + n["text"]).lower()
        if not words or not all(w in blob for w in words):
            continue
        lines = [l.strip(" -\t") for l in n["text"].splitlines() if len(l.strip(" -\t")) > 8]
        best = max(lines, key=lambda l: sum(w in l.lower() for w in words), default="")
        if best:
            points.append({"source": n["id"], "quote": best[:120], "text": best, "confidence": "high"})
    if not points:
        return {"found": False, "answer": "", "points": [], "gaps": []}
    return {"found": True, "points": points,
            "answer": f"(Demo mode: simple word match, no AI.) {len(points)} note{'s' if len(points) != 1 else ''} mention that, oldest first.",
            "gaps": []}


def demo_connect(notes):
    def ids(*parts):
        return [i for i in (_find(notes, p) for p in parts) if i]
    threads = [
        {"name": "Leeds recruitment", "summary": "12 patients so far against a lowered target of 30 by end of December. Screen failures on bloods are a concern.",
         "sources": ids("team standup", "Leeds site call", "trial site mtg", "draft update to Sam"), "next_step": "Get the latest count before Friday's review."},
        {"name": "Nurse cover", "summary": "Agency nurse budget approved at £16k; Priya can book from 1 Dec.",
         "sources": ids("1-1 Priya", "Marcus message", "Agency nurse"), "next_step": "Confirm Priya has booked the agency nurse."},
        {"name": "Kits and supply", "summary": "Delayed kit batch now due 2 Dec; Leeds has about 1.5 weeks of kits left.",
         "sources": ids("supplier call"), "next_step": "Ask Dr Okafor whether Bristol can lend kits."},
        {"name": "Opening Bristol", "summary": "Decision on 3 Nov to start Bristol setup now; training slides are coming from Dr Okafor.",
         "sources": ids("Leeds site call", "ideas"), "next_step": ""},
    ]
    links = []
    for a, b, text in [
        ("supplier call", "Leeds site call", "The kit delay (2 Dec) could stall recruitment just as Bristol is being set up, and borrowing Bristol's kits would link the two."),
        ("1-1 Priya", "ideas", "Priya's worry about blood test failures matches your idea that fasting instructions may be unclear."),
        ("draft update to Sam", "Leeds site call", "Your unfinished email to Sam uses the old target of 40; the site call changed it to 30."),
    ]:
        sa, sb = _find(notes, a), _find(notes, b)
        if sa and sb:
            links.append({"text": text, "source_a": sa, "source_b": sb})
    return {"threads": [t for t in threads if t["sources"]], "links": links}


# ---------------------------------------------------------------------------
# 8. Privacy check: what a user can look at to see the rule is kept
# ---------------------------------------------------------------------------

WRITTEN = []  # files the user chose to save this session (always into their own notes folder)


def changed_files_in_app_folder():
    """Any file in the app's own folder created or changed since the app started (should be none)."""
    roots = [r for r in ACCESS["roots"]]
    out = []
    for dirpath, dirnames, filenames in os.walk(BASE_DIR):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            p = Path(dirpath, f)
            if any(str(p).startswith(str(r) + os.sep) for r in roots):
                continue  # your notes folders are reported separately
            try:
                if p.stat().st_mtime > START_TIME:
                    out.append(str(p.relative_to(BASE_DIR)))
            except OSError:
                pass
    return out


def privacy_report():
    with _audit_lock:
        log = list(AUDIT)
    connects = [e for e in log if e["kind"] == "connect"]
    blocked = [e for e in log if e["kind"] == "blocked"]
    with _memory_lock:
        held = len(MEMORY["image_text"])
    old_store = [n for n in ("notes.json", "notes.tmp") if (BASE_DIR / n).exists()]
    return {
        "ai": f"{'Demo mode (no AI)' if DEMO_MODE else 'Ollama'} at {OLLAMA_URL}" + ("" if DEMO_MODE else f", model {OLLAMA_MODEL}"),
        "listening": "this computer only (127.0.0.1)" if not PHONE_ACCESS else "your Wi-Fi network too (PHONE_ACCESS=1)",
        "connections": len(connects),
        "outside_connections": 0,
        "blocked": blocked[-10:],
        "held_in_memory": held,
        "app_files_changed": changed_files_in_app_folder(),
        "old_store": old_store,
        "written_by_you": list(WRITTEN),
        "log": log[-60:][::-1],
        "uptime_min": int((time.time() - START_TIME) / 60),
    }


# ---------------------------------------------------------------------------
# 9. Web routes
# ---------------------------------------------------------------------------

@app.after_request
def lock_down_browser(resp):
    # The browser may only talk to this app: no outside scripts, fonts, images or requests.
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )
    resp.headers["Referrer-Policy"] = "no-referrer"
    # Tell the browser not to keep its own copies of your notes.
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _err(msg, code=400):
    if code >= 500:
        print(f"  PROBLEM: {msg}")  # also show it in Terminal
    return jsonify(error=msg), code


@app.get("/")
def index():
    return render_template("index.html", demo_mode=DEMO_MODE, model=OLLAMA_MODEL)


@app.get("/api/access")
def get_access():
    return jsonify(access_tree())


@app.post("/api/access/toggle")
def toggle_access():
    body = request.get_json(silent=True) or {}
    key, on = str(body.get("key", "")), bool(body.get("visible"))
    with _access_lock:
        root, path = _resolve_key(key)
        if root is None:
            return _err("Unknown file or folder.")
        if on:
            ACCESS["hidden"].discard(key)
            idx, rel = key.split("#", 1)
            # Showing a folder shows everything inside it.
            prefix = _key(int(idx), rel + "/" if rel else "")
            ACCESS["hidden"] = {k for k in ACCESS["hidden"] if not k.startswith(prefix)}
            # Showing a file inside a hidden folder: hide its siblings instead of the whole folder.
            parts = rel.split("/") if rel else []
            for i in range(len(parts)):
                parent = _key(int(idx), "/".join(parts[:i]))
                if parent in ACCESS["hidden"]:
                    ACCESS["hidden"].discard(parent)
                    for other in _walk(ACCESS["roots"][int(idx)]):
                        if other.startswith("/".join(parts[:i]) + "/" if i else "") and other != rel and not other.startswith(rel + "/"):
                            ACCESS["hidden"].add(_key(int(idx), other))
        else:
            ACCESS["hidden"].add(key)
    audit("access", f"You {'showed' if on else 'hid'} '{path.name or root.name}' {'to' if on else 'from'} the assistant")
    return jsonify(access_tree())


@app.post("/api/access/pause")
def pause_access():
    body = request.get_json(silent=True) or {}
    with _access_lock:
        ACCESS["paused"] = bool(body.get("paused"))
    audit("access", "You paused the assistant: it can see nothing" if ACCESS["paused"] else "You resumed the assistant")
    return jsonify(access_tree())


@app.post("/api/access/folder")
def add_folder():
    body = request.get_json(silent=True) or {}
    raw = str(body.get("path", "")).strip()
    if not raw:
        return _err("Type the folder's location, e.g. ~/Documents/Notes")
    path = Path(os.path.expanduser(raw)).resolve()
    if not path.is_dir():
        return _err(f"I can't find a folder at {path}")
    with _access_lock:
        if path in ACCESS["roots"]:
            return _err("That folder is already in the list.")
        ACCESS["roots"].append(path)
    audit("access", f"You let the assistant see the folder {path}")
    return jsonify(access_tree())


@app.delete("/api/access/folder")
def remove_folder():
    body = request.get_json(silent=True) or {}
    key = str(body.get("key", ""))
    with _access_lock:
        root, _ = _resolve_key(key)
        if root is None:
            return _err("Unknown folder.")
        idx = ACCESS["roots"].index(root)
        ACCESS["roots"].pop(idx)
        # Re-number the hidden keys of the folders after it.
        new_hidden = set()
        for k in ACCESS["hidden"]:
            i, rest = k.split("#", 1)
            i = int(i)
            if i < idx:
                new_hidden.add(k)
            elif i > idx:
                new_hidden.add(_key(i - 1, rest))
        ACCESS["hidden"] = new_hidden
        MEMORY["image_text"].clear()
    audit("access", f"You removed the folder {root} from what the assistant can see")
    return jsonify(access_tree())


@app.get("/api/note")
def get_note():
    """Open one note so you can see the exact words an answer came from."""
    key = request.args.get("key", "")
    file_key = key.split("@", 1)[0]
    with _access_lock:
        root, path = _resolve_key(file_key)
        if root is None or not path.is_file():
            return _err("Note not found.", 404)
        idx, rel = file_key.split("#", 1)
        if ACCESS["paused"] or _is_hidden(int(idx), rel):
            return _err("You've hidden this note from the assistant.", 403)
    if path.suffix.lower() in IMAGE_TYPES:
        return send_file(path, mimetype=IMAGE_TYPES[path.suffix.lower()], max_age=0)
    return Response(path.read_text(encoding="utf-8", errors="replace"), mimetype="text/plain; charset=utf-8")


@app.post("/api/brief")
def brief():
    body = request.get_json(silent=True) or {}
    meeting = str(body.get("meeting", "")).strip()[:300]
    if not meeting:
        return _err("Type the meeting you're preparing for.")
    try:
        return jsonify(build_brief(meeting))
    except AIError as exc:
        return _err(str(exc), 502)


@app.post("/api/ask")
def ask():
    body = request.get_json(silent=True) or {}
    question = str(body.get("question", "")).strip()[:1000]
    if not question:
        return _err("Type a question first.")
    history = body.get("history") if isinstance(body.get("history"), list) else []
    try:
        return jsonify(build_answer(question, history))
    except AIError as exc:
        return _err(str(exc), 502)


@app.post("/api/connect")
def connect():
    try:
        return jsonify(build_connections())
    except AIError as exc:
        return _err(str(exc), 502)


@app.post("/api/capture/photo")
def capture_photo():
    """Read a photo you've just taken. The photo itself is not kept anywhere."""
    file = request.files.get("photo")
    if not file or not file.filename:
        return _err("Please choose a photo.")
    media = (file.mimetype or "").lower().replace("image/jpg", "image/jpeg")
    if media not in IMAGE_TYPES.values():
        return _err("Please use a JPG, PNG, GIF or WEBP image. (iPhone HEIC photos: set Camera > Formats > Most Compatible.)")
    data = file.read()
    try:
        if DEMO_MODE:
            result = {"text": DEMO_IMAGES["trial site mtg"], "unclear_words": []}
        else:
            prompt = TRANSCRIBE_PROMPT.replace("{today}", today().strftime("%A %Y-%m-%d"))
            result = ask_ai_for_json(prompt, TRANSCRIBE_SCHEMA, image=(data, media))
    except AIError as exc:
        return _err(str(exc), 502)
    finally:
        del data
    audit("read", "Read a new photo on this computer. The photo was not saved; you choose whether to save the text.")
    if not result.get("text", "").strip():
        return _err("I couldn't find any writing in that photo.", 422)
    return jsonify(text=result["text"], unclear_words=result.get("unclear_words", []))


@app.post("/api/capture/save")
def capture_save():
    """Save a note as a plain text file in YOUR notes folder (you pick which)."""
    body = request.get_json(silent=True) or {}
    text = str(body.get("text", "")).strip()
    if not text:
        return _err("Write something first.")
    try:
        when = datetime.strptime(str(body.get("date") or today().isoformat()), "%Y-%m-%d").date()
    except ValueError:
        when = today()
    title = str(body.get("title") or text.splitlines()[0])[:60]
    safe = re.sub(r"[^\w\- ]+", "", title).strip() or "note"
    with _access_lock:
        try:
            root = ACCESS["roots"][int(body.get("root", 0))]
        except (ValueError, IndexError):
            return _err("Choose one of your folders to save into.")
    folder = root / "inbox"
    folder.mkdir(exist_ok=True)
    path = folder / f"{when.isoformat()} {safe}.md"
    n = 2
    while path.exists():
        path = folder / f"{when.isoformat()} {safe} ({n}).md"
        n += 1
    path.write_text(text + "\n", encoding="utf-8")
    rel = path.relative_to(root).as_posix()
    WRITTEN.append(f"{root.name}/{rel}")
    audit("write", f"You saved a note to your own folder: {root.name}/{rel}")
    return jsonify(saved=f"{root.name}/{rel}", access=access_tree())


@app.get("/api/privacy")
def privacy():
    return jsonify(privacy_report())


@app.post("/api/privacy/test")
def privacy_test():
    """Try to reach the internet on purpose, to show it gets blocked."""
    try:
        socket.create_connection(("example.com", 443), timeout=3).close()
        result = "WARNING: the connection went through. Please report this."
        ok = False
    except (PermissionError, socket.gaierror, OSError) as exc:
        result = f"Blocked, as it should be: {exc}"
        ok = isinstance(exc, (PermissionError, socket.gaierror))
    return jsonify(ok=ok, result=result, report=privacy_report())


@app.post("/api/forget")
def forget():
    with _memory_lock:
        n = len(MEMORY["image_text"])
        MEMORY["image_text"].clear()
    audit("forget", f"You pressed Forget: cleared {n} photo reading{'s' if n != 1 else ''} from memory")
    return jsonify(cleared=n, report=privacy_report())


if __name__ == "__main__":
    print()
    if DEMO_MODE:
        print("  DEMO MODE: no AI will be called; you'll see example results.")
    else:
        try:
            with _opener.open(f"{OLLAMA_URL}/api/tags", timeout=3) as r:
                names = [m["name"] for m in json.loads(r.read()).get("models", [])]
            if not any(n == OLLAMA_MODEL or n.split(":")[0] == OLLAMA_MODEL for n in names):
                print(f"  WARNING: Ollama is running but doesn't have '{OLLAMA_MODEL}'. Run:  ollama pull {OLLAMA_MODEL}")
        except (urllib.error.URLError, OSError):
            print(f"  WARNING: can't reach Ollama at {OLLAMA_URL}. Open the Ollama app first (see README).")
    print("  Notes folders it can see:", ", ".join(str(r) for r in ACCESS["roots"]) or "(none)")
    print("  Network lock is on: only connections to this computer are allowed.")
    port = int(os.environ.get("PORT", 5050))
    host = "0.0.0.0" if PHONE_ACCESS else "127.0.0.1"
    if PHONE_ACCESS:
        print("  PHONE_ACCESS is on: other devices on your Wi-Fi can open the app.")
    print(f"  Second Brain running at http://localhost:{port}\n")
    app.run(host=host, port=port, debug=False)
