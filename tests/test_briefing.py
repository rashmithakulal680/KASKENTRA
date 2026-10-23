"""Briefing length and tidiness: short template, AI replies trimmed to plain prose."""
from app import summary
from tests.test_final import konaje_facts


def test_template_briefing_is_short():
    out = summary.summarise(konaje_facts())
    assert out["text"] == " ".join(out["lines"])
    assert len(out["lines"]) <= 6
    assert len(out["text"].split()) < 120
    # headline facts stay, per-action timings and "combinations checked" noise are gone
    assert "combinations" not in out["text"] and "crew starts at" not in out["text"]


def test_long_markdown_ai_reply_is_trimmed(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    wordy = ("**Briefing**\n\n- A flood stopped Konaje. Everything is back at hour 41 with no action. "
             "The plan costs 58,000 rupees and brings recovery to hour 27. Estimates; the duty officer "
             "approves. Extra sentence five. Extra sentence six with more words.")
    monkeypatch.setattr(summary, "call_groq", lambda *a: wordy)
    out = summary.summarise(konaje_facts())
    assert out["source"] == "ai"
    assert "*" not in out["text"] and "\n" not in out["text"] and "- " not in out["text"]
    assert len(out["lines"]) == summary.MAX_SENTENCES
    assert out["text"].endswith("approves.")
