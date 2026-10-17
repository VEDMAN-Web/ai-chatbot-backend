"""Chat history + property_context must persist via Firestore, not Postgres."""

from unittest.mock import MagicMock, patch

from app.services.conversation_service import (
    append_conversation_turn,
    clear_in_session_conversation,
    get_or_create_conversation,
)
from app.services.firestore_service import FirestoreConversation


def test_get_or_create_firestore_not_postgres():
    clear_in_session_conversation("sess-fs-2", "public")
    fake = FirestoreConversation(
        "public__sess-fs-2",
        {
            "sessionId": "sess-fs-2",
            "siteOrigin": "varsoviaestate.com",
            "agentType": "public",
            "messageHistory": [{"role": "user", "content": "hi"}],
            "preferences": {"property_context": {"active_property_id": "PROP-ALPHA"}},
            "leadScore": 0.0,
        },
    )
    db = MagicMock()
    with patch(
        "app.services.conversation_service._firestore_sync_enabled", return_value=True
    ), patch(
        "app.services.firestore_service.get_or_create_conversation_firestore",
        return_value=fake,
    ):
        conv = get_or_create_conversation(db=db, session_id="sess-fs-2", agent_type="public")

    assert conv.session_id == "sess-fs-2"
    assert conv.preferences["property_context"]["active_property_id"] == "PROP-ALPHA"
    db.query.assert_not_called()


def test_append_writes_firestore_not_postgres():
    clear_in_session_conversation("sess-fs-3", "public")
    conv = FirestoreConversation(
        "public__sess-fs-3",
        {
            "sessionId": "sess-fs-3",
            "siteOrigin": "varsoviaestate.com",
            "agentType": "public",
            "messageHistory": [],
            "preferences": {},
            "leadScore": 0.0,
        },
    )
    db = MagicMock()
    updated = FirestoreConversation(
        "public__sess-fs-3",
        {
            "sessionId": "sess-fs-3",
            "siteOrigin": "varsoviaestate.com",
            "agentType": "public",
            "messageHistory": [
                {"role": "user", "content": "its price?"},
                {"role": "assistant", "content": "18.5M THB"},
            ],
            "preferences": {"property_context": {"active_property_id": "PROP-ALPHA"}},
            "leadScore": 0.0,
        },
    )
    with patch(
        "app.services.conversation_service._firestore_sync_enabled", return_value=True
    ), patch(
        "app.services.firestore_service.append_conversation_turn_firestore",
        return_value=updated,
    ) as m:
        out = append_conversation_turn(
            db=db,
            conversation=conv,
            user_message="its price?",
            assistant_message="18.5M THB",
            new_preferences={"property_context": {"active_property_id": "PROP-ALPHA"}},
        )

    m.assert_called_once()
    db.commit.assert_not_called()
    assert out.preferences["property_context"]["active_property_id"] == "PROP-ALPHA"
    assert len(out.message_history) == 2
