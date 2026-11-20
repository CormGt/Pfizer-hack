"""
Notes Manager - an AI assistant that pieces together scattered notes.

Run it with:  python app.py   then open http://localhost:5050

What it does:
  * You type a note, or upload / snap a photo of handwritten notes.
  * The AI reads the handwriting and turns the photo into tidy notes.
  * "Organise my notes" asks the AI to group related notes into topics,
    summarise each topic, pull out action items and link related topics.

Which AI it uses (set AI_PROVIDER in your .env file):
  * ollama  (default) - a model running locally on your own computer via
                        Ollama. Free, private, works offline.
  * claude            - Claude via the Anthropic API (needs an API key).

All notes are saved to notes.json next to this file, so nothing is lost
when you restart the app.
"""

import base64
import json
import os
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request

# Load settings (AI_PROVIDER, API key, ...) from a .env file if one exists.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "notes.json"

PROVIDER = os.environ.get("AI_PROVIDER", "ollama").strip().lower()
if PROVIDER not in ("ollama", "claude"):
    raise SystemExit(f"AI_PROVIDER must be 'ollama' or 'claude', not {PROVIDER!r}")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5vl:7b")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
MODEL = OLLAMA_MODEL if PROVIDER == "ollama" else CLAUDE_MODEL

# Demo mode: lets you click around the app with no AI set up at all.
# No AI is called; the app returns made-up example results instead.
DEMO_MODE = os.environ.get("DEMO_MODE", "").lower() in ("1", "true", "yes")

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_UPLOAD_MB = 10

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Saving and loading notes
# ---------------------------------------------------------------------------

def load_data():
    if DATA_FILE.exists():
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        for n in data["notes"]:  # notes saved before dates existed
            n.setdefault("date", n["created_at"][:10])
        return data
    return {"notes": [], "organised": None}


def save_data(data):
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(DATA_FILE)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today():
    return datetime.now().date().isoformat()


def clean_date(value):
    """Return value if it's a real YYYY-MM-DD date, otherwise today's date."""
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date().isoformat()
    except ValueError:
        return today()


def make_note(title, text, source, date=None):
    return {
        "id": uuid.uuid4().hex[:8],
        "title": title.strip() or "Untitled note",
        "text": text.strip(),
        "source": source,  # "typed" or "photo"
        "date": clean_date(date),  # the day the note is from (YYYY-MM-DD)
        "created_at": now_iso(),
    }


def sort_by_date(notes):
    return sorted(notes, key=lambda n: (n["date"], n["created_at"]))


# ---------------------------------------------------------------------------
# Talking to the AI (local Ollama model, or Claude)
# ---------------------------------------------------------------------------

class AIError(Exception):
    """A problem talking to the AI, with a message that's safe to show the user."""


def ask_ai_for_json(prompt, schema, image=None):
    """Send a prompt (and optionally one image) to the AI and get back JSON matching `schema`.

    `image` is a (bytes, media_type) tuple, or None.
    """
    if PROVIDER == "ollama":
        text = _ask_ollama(prompt, schema, image)
    else:
        text = _ask_claude(prompt, schema, image)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AIError("The AI returned something unexpected. Please try again.") from exc


def _ask_ollama(prompt, schema, image):
    message = {"role": "user", "content": prompt}
    if image:
        message["images"] = [base64.b64encode(image[0]).decode("ascii")]
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [message],
        "format": schema,          # Ollama "structured outputs": forces JSON in this shape
        "stream": False,
        "options": {"temperature": 0},
    }
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        # Local models can be slow, especially the first call while the model loads.
        with urllib.request.urlopen(req, timeout=600) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        if exc.code == 404 or "not found" in detail.lower():
            raise AIError(f"Ollama doesn't have the model '{OLLAMA_MODEL}' yet. In Terminal run:  ollama pull {OLLAMA_MODEL}") from exc
        raise AIError(f"Ollama returned an error: {detail[:300]}") from exc
    except (urllib.error.URLError, ConnectionError) as exc:
        raise AIError("Can't reach Ollama. Make sure the Ollama app is open (or run 'ollama serve' in Terminal).") from exc
    except TimeoutError as exc:
        raise AIError("Ollama took too long to answer. Try a smaller photo or a smaller model.") from exc
    return body.get("message", {}).get("content", "")


_client = None


def _ask_claude(prompt, schema, image):
    global _client
    try:
        import anthropic  # only needed when AI_PROVIDER=claude
    except ImportError as exc:
        raise AIError("To use Claude, install its package first: pip install -r requirements-claude.txt (needs Python 3.10 or newer).") from exc
    if _client is None:
        _client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    content = []
    if image:
        image_b64 = base64.standard_b64encode(image[0]).decode("utf-8")
        content.append({"type": "image", "source": {"type": "base64", "media_type": image[1], "data": image_b64}})
    content.append({"type": "text", "text": prompt})
    try:
        response = _client.beta.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=16000,
            messages=[{"role": "user", "content": content}],
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": schema},
            },
            # If a request is ever declined by a safety check, the API retries it
            # on a suitable fallback model automatically.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as exc:
        raise AIError("Your Anthropic API key is missing or wrong. Check the .env file (see README).") from exc
    except anthropic.APIError as exc:
        raise AIError(f"Couldn't reach Claude: {exc}") from exc
    except TypeError as exc:  # raised by the SDK when no API key is configured at all
        raise AIError("No Anthropic API key found. Add ANTHROPIC_API_KEY to your .env file (see README).") from exc
    if response.stop_reason == "refusal":
        raise AIError("Claude declined to process this request.")
    if response.stop_reason == "max_tokens":
        raise AIError("The answer was too long and got cut off. Try fewer notes.")
    return "".join(b.text for b in response.content if b.type == "text")


TRANSCRIBE_SCHEMA = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "text": {"type": "string"},
                    "date": {"type": "string"},
                },
                "required": ["title", "text", "date"],
                "additionalProperties": False,
            },
        },
        "unclear_words": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["notes", "unclear_words"],
    "additionalProperties": False,
}

TRANSCRIBE_PROMPT = """This is a photo of handwritten notes.

1. Read all the handwriting carefully, including anything in margins, arrows or crossed-out bits that still matter.
2. Split it into separate notes, one per distinct idea or topic on the page. A page about one thing is one note.
3. For each note give a short title (max 8 words) and the text. Keep the writer's meaning and wording; fix obvious spelling only. Keep bullet points as lines starting with "- ".
4. If a word is hard to read, write your best guess followed by [?] and also list it in unclear_words.
5. date: if the page shows a date for a note (for example "12/11" or "Tues 3rd Nov"), give it as YYYY-MM-DD. Today is {today}; use that to fill in a missing year or work out a weekday. If no date is written for a note, use an empty string. Never guess a date that isn't written.

If the photo contains no handwriting or text at all, return an empty notes list."""


ORGANISE_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "summary": {"type": "string"},
                    "note_ids": {"type": "array", "items": {"type": "string"}},
                    "action_items": {"type": "array", "items": {"type": "string"}},
                    "related_topics": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "summary", "note_ids", "action_items", "related_topics"],
                "additionalProperties": False,
            },
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["overview", "topics", "open_questions"],
    "additionalProperties": False,
}

ORGANISE_PROMPT = """You are a helpful notes manager. Below are someone's scattered notes, some typed and some transcribed from handwriting. Piece them together.

- Group the notes into topics. A note belongs to exactly one topic, the one it fits best. Use the note ids exactly as given.
- For each topic write a short, clear summary that combines what all its notes say (2-4 sentences), and list concrete action items (things someone needs to do, with names and dates if the notes mention them).
- In related_topics, name other topics (by their exact topic name) that this topic connects to, if any.
- overview: 2-3 sentences on the big picture across all notes.
- open_questions: things that are unclear, contradictory or missing across the notes.

Notes:
"""


CHAT_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "note_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer", "note_ids"],
    "additionalProperties": False,
}

CHAT_PROMPT = """You answer questions about the user's notes. Today is {today}.

Rules:
- Use ONLY the notes below. Never use outside knowledge and never invent facts, names, dates or numbers.
- note_ids: list the id of EVERY note that is about the subject asked, not just the most important ones.
- If no note is about the subject, set note_ids to [] and say plainly that there are no notes about it. Do not guess or fill the gap.
- When notes are about the subject, answer briefly from them and mention dates (each note has one) where they help, e.g. "On 12 Nov you noted...". If notes disagree, say so and give both with their dates.
- Keep the answer short and plain.

Notes (oldest first):
{notes}

Conversation so far:
{history}

User's question: {question}"""

MAX_CHAT_NOTES = 200
NO_NOTES_ANSWER = "I couldn't find any notes about that, so I can't answer it from your notes."


def pick_candidate_notes(notes, question):
    """If there are lots of notes, keep the ones that share the most words with the question."""
    if len(notes) <= MAX_CHAT_NOTES:
        return notes
    words = {w for w in question.lower().split() if len(w) > 2}
    def score(n):
        blob = (n["title"] + " " + n["text"]).lower()
        return sum(w in blob for w in words)
    return sorted(notes, key=score, reverse=True)[:MAX_CHAT_NOTES]


def chat_about_notes(notes, question, history):
    if not notes:
        return {"answer": "You don't have any notes yet. Add some on the left first.", "note_ids": []}
    if DEMO_MODE:
        return demo_chat(notes, question)
    candidates = sort_by_date(pick_candidate_notes(notes, question))
    listing = "\n\n".join(
        f"[id: {n['id']}] [date: {n['date']}] {n['title']}\n{n['text']}" for n in candidates
    )
    convo = "\n".join(
        f"{'User' if h.get('role') == 'user' else 'Assistant'}: {str(h.get('text', ''))[:1000]}"
        for h in history[-6:]
    ) or "(none)"
    prompt = (CHAT_PROMPT.replace("{today}", datetime.now().strftime("%A %Y-%m-%d"))
              .replace("{notes}", listing).replace("{history}", convo).replace("{question}", question))
    result = ask_ai_for_json(prompt, CHAT_SCHEMA)
    valid = {n["id"] for n in candidates}
    result["note_ids"] = [i for i in dict.fromkeys(result["note_ids"]) if i in valid]
    # Safety net: an answer that isn't backed by any note is never shown.
    if not result["note_ids"]:
        result["answer"] = NO_NOTES_ANSWER
    return result


def transcribe_photo(image_bytes, media_type):
    if DEMO_MODE:
        return demo_transcription()
    prompt = TRANSCRIBE_PROMPT.replace("{today}", datetime.now().strftime("%A %Y-%m-%d"))
    return ask_ai_for_json(prompt, TRANSCRIBE_SCHEMA, image=(image_bytes, media_type))


def organise_notes(notes):
    if DEMO_MODE:
        return demo_organise(notes)
    listing = "\n\n".join(
        f"[id: {n['id']}] [date: {n['date']}] ({n['source']}) {n['title']}\n{n['text']}" for n in sort_by_date(notes)
    )
    result = ask_ai_for_json(ORGANISE_PROMPT + listing, ORGANISE_SCHEMA)
    # Drop any ids the AI made up, and put forgotten notes in an "Other" topic.
    valid = {n["id"] for n in notes}
    seen = set()
    for topic in result["topics"]:
        topic["note_ids"] = [i for i in topic["note_ids"] if i in valid and i not in seen]
        seen.update(topic["note_ids"])
    missing = [n["id"] for n in notes if n["id"] not in seen]
    if missing:
        result["topics"].append({
            "name": "Other", "summary": "Notes that didn't fit a topic.",
            "note_ids": missing, "action_items": [], "related_topics": [],
        })
    result["topics"] = [t for t in result["topics"] if t["note_ids"]]
    return result


# ---------------------------------------------------------------------------
# Demo-mode fake results (only used when DEMO_MODE=1)
# ---------------------------------------------------------------------------

def demo_transcription():
    return {
        "notes": [
            {
                "title": "Trial site meeting - Tues",
                "date": "",
                "text": "- Recruitment behind at Leeds site (12 of 40)\n- Ask Priya re: extra nurse cover\n- Next check-in Fri 28th",
            },
            {
                "title": "Hackathon pitch ideas",
                "date": "",
                "text": "- Demo: photo of notes -> organised board\n- Keep it to 3 min\n- Sam doing slides",
            },
            {
                "title": "Call supplier about delayed batch",
                "date": "",
                "text": "Call supplier re delayed batch before Thurs",
            },
        ],
        "unclear_words": [],
    }


def demo_chat(notes, question):
    stop = {"what", "about", "have", "the", "any", "notes", "did", "are", "there", "with", "for", "and", "how", "who", "when", "this", "that", "from", "show", "all", "tell"}
    words = [w.strip("?.,!").lower() for w in question.split()]
    words = [w for w in words if len(w) > 2 and w not in stop]
    hits = [n for n in sort_by_date(notes)
            if any(w in (n["title"] + " " + n["text"]).lower() for w in words)]
    if not hits:
        return {"answer": NO_NOTES_ANSWER, "note_ids": []}
    return {
        "answer": f"(Demo mode - simple word match, no AI.) I found {len(hits)} note{'s' if len(hits) > 1 else ''} about that, shown below oldest first.",
        "note_ids": [n["id"] for n in hits],
    }


def demo_organise(notes):
    trial, pitch, supply, other = [], [], [], []
    for n in notes:
        blob = (n["title"] + " " + n["text"]).lower()
        if any(w in blob for w in ("trial", "site", "recruit", "patient", "nurse", "priya")):
            trial.append(n["id"])
        elif any(w in blob for w in ("hackathon", "pitch", "demo", "slides")):
            pitch.append(n["id"])
        elif any(w in blob for w in ("supplier", "batch", "shipment", "delivery")):
            supply.append(n["id"])
        else:
            other.append(n["id"])
    topics = []
    if trial:
        topics.append({
            "name": "Clinical trial recruitment",
            "summary": "Recruitment at the Leeds site is behind target (12 of 40). Extra nurse cover is being considered to speed things up.",
            "note_ids": trial,
            "action_items": ["Ask Priya about extra nurse cover", "Prepare numbers for Friday 28th check-in"],
            "related_topics": ["Supply chain"] if supply else [],
        })
    if pitch:
        topics.append({
            "name": "Hackathon pitch",
            "summary": "The pitch is a 3-minute live demo showing a photo of notes turning into an organised board.",
            "note_ids": pitch,
            "action_items": ["Sam to make the slides", "Rehearse the 3-minute demo"],
            "related_topics": [],
        })
    if supply:
        topics.append({
            "name": "Supply chain",
            "summary": "A batch from the supplier is delayed, which could affect site stock. It needs chasing before Thursday.",
            "note_ids": supply,
            "action_items": ["Call the supplier about the delayed batch before Thursday"],
            "related_topics": ["Clinical trial recruitment"] if trial else [],
        })
    if other:
        topics.append({
            "name": "Other", "summary": "Notes that didn't fit a topic.",
            "note_ids": other, "action_items": [], "related_topics": [],
        })
    return {
        "overview": "(Demo mode - example output, no AI was called.) Your notes cover a trial that is behind on recruitment, a delayed supplier batch that may affect it, and a hackathon pitch you're preparing.",
        "topics": topics,
        "open_questions": ["Who owns the Friday check-in?"],
    }


# ---------------------------------------------------------------------------
# Web routes
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html", demo_mode=DEMO_MODE, provider=PROVIDER, model=MODEL)


@app.get("/api/state")
def get_state():
    with _lock:
        return jsonify(load_data())


@app.post("/api/notes")
def add_note():
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify(error="Please write something first."), 400
    title = (body.get("title") or "").strip()
    if not title:
        first_line = text.splitlines()[0]
        title = first_line if len(first_line) <= 60 else first_line[:60].rsplit(" ", 1)[0] + "…"
    with _lock:
        data = load_data()
        note = make_note(title, text, "typed", body.get("date"))
        data["notes"].append(note)
        save_data(data)
    return jsonify(note=note)


@app.post("/api/photo")
def add_photo():
    file = request.files.get("photo")
    if not file or not file.filename:
        return jsonify(error="Please choose a photo."), 400
    media_type = (file.mimetype or "").lower()
    if media_type == "image/jpg":
        media_type = "image/jpeg"
    if media_type not in ALLOWED_IMAGE_TYPES:
        return jsonify(error="Please upload a JPG, PNG, GIF or WEBP image. (iPhone HEIC photos: set Camera > Formats > Most Compatible.)"), 400
    try:
        result = transcribe_photo(file.read(), media_type)
    except AIError as exc:
        return jsonify(error=str(exc)), 502
    if not result["notes"]:
        return jsonify(error="No handwriting was found in that photo."), 422
    with _lock:
        data = load_data()
        new_notes = [make_note(n["title"], n["text"], "photo", n.get("date")) for n in result["notes"]]
        data["notes"].extend(new_notes)
        save_data(data)
    return jsonify(notes=new_notes, unclear_words=result["unclear_words"])


@app.patch("/api/notes/<note_id>")
def update_note(note_id):
    body = request.get_json(silent=True) or {}
    with _lock:
        data = load_data()
        for n in data["notes"]:
            if n["id"] == note_id:
                if "date" in body:
                    n["date"] = clean_date(body["date"])
                save_data(data)
                return jsonify(note=n)
    return jsonify(error="Note not found."), 404


@app.post("/api/chat")
def chat():
    body = request.get_json(silent=True) or {}
    question = (body.get("message") or "").strip()
    if not question:
        return jsonify(error="Type a question first."), 400
    history = body.get("history") if isinstance(body.get("history"), list) else []
    with _lock:
        notes = load_data()["notes"]
    try:
        result = chat_about_notes(notes, question[:2000], history)
    except AIError as exc:
        return jsonify(error=str(exc)), 502
    by_id = {n["id"]: n for n in notes}
    matched = sort_by_date([by_id[i] for i in result["note_ids"] if i in by_id])
    return jsonify(answer=result["answer"], notes=matched)


@app.delete("/api/notes/<note_id>")
def delete_note(note_id):
    with _lock:
        data = load_data()
        data["notes"] = [n for n in data["notes"] if n["id"] != note_id]
        save_data(data)
    return jsonify(ok=True)


@app.post("/api/organise")
def organise():
    with _lock:
        notes = load_data()["notes"]
    if not notes:
        return jsonify(error="Add some notes first."), 400
    try:
        result = organise_notes(notes)
    except AIError as exc:
        return jsonify(error=str(exc)), 502
    result["generated_at"] = now_iso()
    with _lock:
        data = load_data()
        data["organised"] = result
        save_data(data)
    return jsonify(organised=result)


@app.post("/api/reset")
def reset():
    with _lock:
        save_data({"notes": [], "organised": None})
    return jsonify(ok=True)


if __name__ == "__main__":
    if DEMO_MODE:
        print("\n  DEMO MODE: no AI will be called, you'll see example results.\n")
    elif PROVIDER == "claude" and not os.environ.get("ANTHROPIC_API_KEY"):
        print("\n  WARNING: AI_PROVIDER=claude but ANTHROPIC_API_KEY is not set (see README).\n")
    elif PROVIDER == "ollama":
        try:
            with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as r:
                names = [m["name"] for m in json.loads(r.read()).get("models", [])]
            if not any(n == OLLAMA_MODEL or n.split(":")[0] == OLLAMA_MODEL for n in names):
                print(f"\n  WARNING: Ollama is running but doesn't have '{OLLAMA_MODEL}'. Run:  ollama pull {OLLAMA_MODEL}\n")
        except (urllib.error.URLError, OSError):
            print(f"\n  WARNING: can't reach Ollama at {OLLAMA_URL}. Open the Ollama app first (see README).\n")
    port = int(os.environ.get("PORT", 5050))
    print(f"  Notes Manager running at http://localhost:{port}  (AI: {PROVIDER} / {MODEL}{', DEMO MODE' if DEMO_MODE else ''})")
    app.run(host="0.0.0.0", port=port, debug=False)
