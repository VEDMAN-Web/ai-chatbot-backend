"""Unknown names must not trigger a dozen sequential Firestore queries."""
from unittest.mock import MagicMock, patch

import app.services.firestore_service as fs
from app.services.property_name_index import PropertyNameIndex


def _index():
    idx = PropertyNameIndex()
    idx.load_from_rows([
        {"doc_id": "anava-condominium", "name": {"en": "Anava Condominium"}, "cityId": "samui",
         "slug": {"en": "anava-condominium"}},
    ])
    idx._loaded_at = 10**12
    return idx


def _client(exists_ids):
    client = MagicMock()
    col = client.collection.return_value

    def doc(i):
        d = MagicMock()
        snap = MagicMock(exists=i in exists_ids, id=i)
        snap.to_dict.return_value = {"name": {"en": "Anava Condominium"}}
        d.get.return_value = snap
        return d

    col.document.side_effect = doc
    return client, col


def test_unknown_name_makes_no_firestore_calls():
    idx = _index()
    client, col = _client(set())
    with patch.object(fs, "get_firestore_client", return_value=client), \
         patch("app.services.property_name_index.get_property_name_index", return_value=idx), \
         patch("app.services.property_name_index.ensure_name_index_from_firestore"), \
         patch("app.services.property_name_index.match_property_name", side_effect=idx.match):
        assert fs.get_property_by_id_from_firestore("Atlantis Bay Villas") is None
    col.where.assert_not_called()
    col.document.assert_not_called()


def test_known_name_is_one_document_read():
    idx = _index()
    client, col = _client({"anava-condominium"})
    with patch.object(fs, "get_firestore_client", return_value=client), \
         patch.object(fs, "normalize_firestore_project", side_effect=lambda i, d: {"property_id": i}), \
         patch("app.services.property_name_index.get_property_name_index", return_value=idx), \
         patch("app.services.property_name_index.ensure_name_index_from_firestore"), \
         patch("app.services.property_name_index.match_property_name", side_effect=idx.match):
        assert fs.get_property_by_id_from_firestore("Anava")["property_id"] == "anava-condominium"
    col.where.assert_not_called()
    assert col.document.call_count == 1
