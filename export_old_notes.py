"""
One-off helper: turn the notes.json file from the old version of the app into
plain .md files in a folder you choose, so the new version can read them.

    python export_old_notes.py ~/Documents/My notes

The new version never reads or writes notes.json. After exporting, you can
delete notes.json.
"""

import json
import re
import sys
from pathlib import Path

if len(sys.argv) != 2:
    sys.exit("Usage: python export_old_notes.py <folder to put the notes in>")

source = Path(__file__).resolve().parent / "notes.json"
if not source.exists():
    sys.exit("No notes.json found next to this file, so there's nothing to export.")

target = Path(sys.argv[1]).expanduser()
target.mkdir(parents=True, exist_ok=True)
notes = json.loads(source.read_text(encoding="utf-8")).get("notes", [])
for n in notes:
    day = n.get("date") or n.get("created_at", "")[:10]
    name = re.sub(r"[^\w\- ]+", "", n.get("title", "note")).strip()[:60] or "note"
    path = target / f"{day} {name}.md"
    i = 2
    while path.exists():
        path = target / f"{day} {name} ({i}).md"
        i += 1
    path.write_text(n.get("text", "") + "\n", encoding="utf-8")
print(f"Exported {len(notes)} notes to {target}")
print(f"Now start the app with NOTES_FOLDERS={target} in your .env file (or add the folder in the app).")
