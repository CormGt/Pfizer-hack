"""Automated checks for Second Brain.

Run with:  python -m unittest discover tests

They use a pretend Ollama running on this computer, so no real AI is needed.
"""

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# A copy of the sample notes, so the tests never touch the real folder.
TMP = Path(tempfile.mkdtemp())
NOTES = TMP / "notes"
shutil.copytree(ROOT / "sample-notes", NOTES)

# What the pretend Ollama should answer next, and what it was sent.
NEXT_REPLY = {}
SEEN = []


class FakeOllama(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append(body)
        if body["messages"][0].get("images"):
            reply = {"text": "Marcus Hale (Finance): approved the agency nurse budget, £16k (not 18).", "unclear_words": []}
        else:
            reply = NEXT_REPLY
        out = json.dumps({"message": {"content": json.dumps(reply)}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


server = HTTPServer(("127.0.0.1", 0), FakeOllama)
threading.Thread(target=server.serve_forever, daemon=True).start()

os.environ.update({
    "OLLAMA_URL": f"http://127.0.0.1:{server.server_port}",
    "NOTES_FOLDERS": str(NOTES),
    "DEMO_MODE": "",
})
import app as second_brain  # noqa: E402

client = second_brain.app.test_client()


def find_id(sources_or_notes, part):
    for n in sources_or_notes:
        if part in n["path"]:
            return n["id"]
    raise AssertionError(part)


class NetworkLock(unittest.TestCase):
    def test_outside_connection_is_blocked(self):
        with self.assertRaises((PermissionError, socket.gaierror)):
            socket.create_connection(("example.com", 443), timeout=2)

    def test_outside_ip_is_blocked(self):
        s = socket.socket()
        with self.assertRaises(PermissionError):
            s.connect(("93.184.216.34", 80))
        s.close()

    def test_privacy_test_endpoint(self):
        r = client.post("/api/privacy/test").get_json()
        self.assertTrue(r["ok"])
        self.assertTrue(r["report"]["blocked"])

    def test_remote_ollama_refused(self):
        self.assertFalse(second_brain._is_local_host("ollama.example.com"))
        self.assertTrue(second_brain._is_local_host("127.0.0.1"))
        self.assertTrue(second_brain._is_local_host("::1"))

    def test_browser_locked_down(self):
        r = client.get("/")
        self.assertIn("connect-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["Cache-Control"], "no-store")


class NoCopies(unittest.TestCase):
    def test_brief_writes_nothing(self):
        before = {p: p.stat().st_mtime for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts}
        NEXT_REPLY.clear()
        NEXT_REPLY.update({"relevant": False, "headline": "", "key_points": [], "decisions": [], "actions": [], "changed": [], "gaps": []})
        client.post("/api/brief", json={"meeting": "anything"})
        after = {p: p.stat().st_mtime for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts}
        self.assertEqual(before, after)
        self.assertFalse((ROOT / "notes.json").exists())

    def test_forget_clears_memory(self):
        second_brain.load_visible_notes()
        self.assertTrue(second_brain.MEMORY["image_text"])
        client.post("/api/forget")
        self.assertFalse(second_brain.MEMORY["image_text"])


class Access(unittest.TestCase):
    def tearDown(self):
        second_brain.ACCESS["hidden"].clear()
        second_brain.ACCESS["paused"] = False

    def test_hidden_folder_is_never_sent_to_the_ai(self):
        client.post("/api/access/toggle", json={"key": "0#personal", "visible": False})
        SEEN.clear()
        NEXT_REPLY.clear()
        NEXT_REPLY.update({"found": False, "answer": "", "points": [], "gaps": []})
        client.post("/api/ask", json={"question": "What did Sam say about the dashboard?"})
        prompt = SEEN[-1]["messages"][0]["content"]
        self.assertNotIn("development conversation", prompt)
        self.assertIn("supplier", prompt)

    def test_hidden_note_cannot_be_opened(self):
        client.post("/api/access/toggle", json={"key": "0#personal", "visible": False})
        r = client.get("/api/note?key=" + "0%23personal/2026-11-05%20Sam%20-%20development%20chat.md")
        self.assertEqual(r.status_code, 403)

    def test_show_one_file_in_hidden_folder(self):
        client.post("/api/access/toggle", json={"key": "0#inbox", "visible": False})
        client.post("/api/access/toggle", json={"key": "0#inbox/ideas.txt", "visible": True})
        notes, _ = second_brain.load_visible_notes(read_images=False)
        paths = [n["path"] for n in notes]
        self.assertTrue(any("ideas.txt" in p for p in paths))
        self.assertFalse(any("supplier" in p for p in paths))

    def test_pause_sees_nothing(self):
        client.post("/api/access/pause", json={"paused": True})
        notes, meta = second_brain.load_visible_notes()
        self.assertEqual(notes, [])
        r = client.post("/api/ask", json={"question": "anything"}).get_json()
        self.assertFalse(r["found"])

    def test_path_escape_refused(self):
        r = client.get("/api/note?key=0%23../../etc/passwd")
        self.assertIn(r.status_code, (403, 404))


class Honesty(unittest.TestCase):
    def setUp(self):
        self.notes, _ = second_brain.load_visible_notes()

    def test_reads_every_kind(self):
        kinds = {n["kind"] for n in self.notes}
        self.assertEqual(kinds, {"note", "photo", "screenshot", "link", "reminder"})

    def test_made_up_quote_is_flagged(self):
        call = find_id(self.notes, "Leeds site call")
        NEXT_REPLY.clear()
        NEXT_REPLY.update({"found": True, "answer": "Bristol opens in March.", "gaps": [], "points": [
            {"text": "Bristol opens in March", "source": call, "quote": "Bristol will open in March 2027", "confidence": "high"}]})
        r = client.post("/api/ask", json={"question": "When does Bristol open?"}).get_json()
        self.assertTrue(r["unsure"])
        self.assertFalse(r["points"][0]["verified"])
        self.assertEqual(r["points"][0]["confidence"], "low")

    def test_made_up_number_is_flagged(self):
        call = find_id(self.notes, "Leeds site call")
        NEXT_REPLY.clear()
        NEXT_REPLY.update({"found": True, "answer": "x", "gaps": [], "points": [
            {"text": "Recruitment is at 25", "source": call, "quote": "recruitment now 12/40", "confidence": "high"}]})
        r = client.post("/api/ask", json={"question": "numbers?"}).get_json()
        self.assertFalse(r["points"][0]["verified"])
        self.assertTrue(any("25" in w for w in r["points"][0]["warnings"]))

    def test_invented_note_id_is_dropped_and_answer_replaced(self):
        NEXT_REPLY.clear()
        NEXT_REPLY.update({"found": True, "answer": "Parking is free.", "gaps": [], "points": [
            {"text": "Parking is free", "source": "N999", "quote": "parking is free", "confidence": "high"}]})
        r = client.post("/api/ask", json={"question": "parking policy?"}).get_json()
        self.assertFalse(r["found"])
        self.assertNotIn("free", r["answer"])

    def test_real_quote_is_checked(self):
        call = find_id(self.notes, "Leeds site call")
        NEXT_REPLY.clear()
        NEXT_REPLY.update({"relevant": True, "headline": "h", "key_points": [
            {"text": "Leeds is at 12 of 40", "source": call, "quote": "recruitment now 12/40", "confidence": "high"}],
            "decisions": [], "actions": [
            {"text": "Tell Sam", "source": call, "quote": "me: tell Sam new target is 30", "confidence": "high", "owner": "me", "due": "2026-11-01", "status": "open"}],
            "changed": [], "gaps": ["What is the latest count?"]})
        r = client.post("/api/brief", json={"meeting": "Leeds review"}).get_json()
        self.assertTrue(r["key_points"][0]["verified"])
        self.assertTrue(r["actions"][0]["overdue"])
        self.assertIn("Built from", r["explain"])
        self.assertEqual(len(r["sources"]), 1)

    def test_nothing_relevant(self):
        NEXT_REPLY.clear()
        NEXT_REPLY.update({"relevant": False, "headline": "", "key_points": [], "decisions": [], "actions": [], "changed": [], "gaps": []})
        r = client.post("/api/brief", json={"meeting": "Quarterly budget planning"}).get_json()
        self.assertTrue(r["nothing_found"])
        self.assertIn("couldn't find", r["headline"])


class OffTopic(unittest.TestCase):
    def test_unrelated_meeting_is_not_answered_with_old_brief(self):
        # A small model might repeat the Leeds brief for any meeting. The app must not show it.
        notes, _ = second_brain.load_visible_notes()
        call = find_id(notes, "Leeds site call")
        NEXT_REPLY.clear()
        NEXT_REPLY.update({"relevant": True, "headline": "Leeds is at 12 of 40", "key_points": [
            {"text": "Leeds is at 12 of 40", "source": call, "quote": "recruitment now 12/40", "confidence": "high"}],
            "decisions": [], "actions": [], "changed": [], "gaps": []})
        SEEN.clear()
        r = client.post("/api/brief", json={"meeting": "Office move planning"}).get_json()
        self.assertTrue(r["nothing_found"])
        self.assertEqual(r["key_points"], [])
        self.assertNotIn("Leeds", r["headline"])
        self.assertEqual(SEEN, [])  # the AI wasn't even asked

    def test_unrelated_question_not_answered(self):
        SEEN.clear()
        r = client.post("/api/ask", json={"question": "What is the parking policy at the Leeds office?"}).get_json()
        self.assertFalse(r["found"])
        self.assertEqual(SEEN, [])

    def test_related_meeting_still_goes_to_ai(self):
        NEXT_REPLY.clear()
        NEXT_REPLY.update({"relevant": False, "headline": "", "key_points": [], "decisions": [], "actions": [], "changed": [], "gaps": []})
        SEEN.clear()
        client.post("/api/brief", json={"meeting": "Leeds site review with Dr Okafor and Priya"})
        briefs = [b for b in SEEN if not b["messages"][0].get("images")]
        self.assertEqual(len(briefs), 1)


class Capture(unittest.TestCase):
    def test_saved_note_goes_into_your_folder(self):
        r = client.post("/api/capture/save", json={"text": "Call Priya re rota", "date": "2026-11-21", "root": 0}).get_json()
        self.assertTrue((NOTES / "inbox" / "2026-11-21 Call Priya re rota.md").exists())
        self.assertIn("notes/inbox", r["saved"])
        self.assertFalse(list(ROOT.glob("*.json")))


if __name__ == "__main__":
    unittest.main()
