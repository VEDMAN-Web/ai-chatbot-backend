"""Taught answers with audience=admin are served by the admin bot; public ones are not."""
from __future__ import annotations

import pytest

from app.services import taught_answers
from tests.fake_firestore import FakeFirestore, patch_firestore
from tests.test_admin_replay import _say, world  # noqa: F401  (world is a pytest fixture)


@pytest.fixture
def fs(world, monkeypatch):
    fake = patch_firestore(monkeypatch, FakeFirestore())
    taught_answers.invalidate_cache()
    yield fake
    taught_answers.invalidate_cache()


def test_admin_bot_serves_admin_audience_answer(world, fs):
    taught_answers.create(
        question="How do I archive a listing?",
        answer="Say: archive <property name>. I will ask you to confirm, and you can undo it for 6 days.",
        audience="admin",
    )
    out = _say(world, "How do I archive a listing?")
    assert out["text"].startswith("Say: archive")
    assert out["requires_confirmation"] is False


def test_public_only_answer_is_not_served_to_admins(world, fs):
    taught_answers.create(question="Do you arrange viewings?", answer="Yes, by appointment.", audience="public")
    out = _say(world, "Do you arrange viewings?")
    assert "by appointment" not in (out.get("text") or "")


def test_both_audience_and_public_find_match(fs):
    taught_answers.create(question="Do you arrange viewings?", answer="Yes, by appointment.", audience="both")
    assert taught_answers.find_match("Do you arrange viewings?", audience="admin")
    assert taught_answers.find_match("Do you arrange viewings?", audience="public")
    taught_answers.create(question="Who can undo?", answer="Any admin within six days.", audience="admin")
    assert taught_answers.find_match("Who can undo?", audience="public") is None


def test_legacy_docs_without_audience_are_public(fs):
    fs.collection(taught_answers.COLLECTION).document("old").set({
        "question": "Do you arrange viewings?", "answer": "Yes.", "status": "active", "sites": [], "language": "any",
    })
    taught_answers.invalidate_cache()
    assert taught_answers.find_match("Do you arrange viewings?", audience="public")
    assert taught_answers.find_match("Do you arrange viewings?", audience="admin") is None


def test_invalid_audience_rejected(fs):
    with pytest.raises(taught_answers.TaughtAnswerError):
        taught_answers.create(question="q q", answer="a", audience="everyone")
