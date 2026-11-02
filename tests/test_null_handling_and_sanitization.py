"""
Tests for Data Sanitizer and Null Fallback Coercion.
Ensures that:
1. Missing/null values trigger validation failure and warn admin without saving invalid records.
2. Approved/persisted data replaces nulls with typed defaults:
   - string -> ""
   - numeric -> 0
   - boolean -> False
   - list -> []
   - dict -> {}
"""
import pytest
from unittest.mock import MagicMock

from app.services.data_sanitizer import sanitize_null_value, sanitize_db_record
from app.services.action_service import validate_property_data
from app.services.firestore_service import save_or_update_property_firestore


class TestDataSanitizerUnit:
    def test_sanitize_string_fallback(self):
        assert sanitize_null_value(None, "description") == ""
        assert sanitize_null_value(None, "address") == ""
        assert sanitize_null_value(None, "coverImage") == ""
        assert sanitize_null_value(None, "title") == ""

    def test_sanitize_numeric_fallback(self):
        assert sanitize_null_value(None, "priceFrom") == 0
        assert sanitize_null_value(None, "price_numeric_min") == 0
        assert sanitize_null_value(None, "bedrooms") == 0
        assert sanitize_null_value(None, "bathrooms") == 0
        assert sanitize_null_value(None, "areaFrom") == 0

    def test_sanitize_boolean_fallback(self):
        assert sanitize_null_value(None, "isActive") is False
        assert sanitize_null_value(None, "has_pool") is False
        assert sanitize_null_value(None, "beach_access") is False

    def test_sanitize_list_fallback(self):
        assert sanitize_null_value(None, "images") == []
        assert sanitize_null_value(None, "amenities") == []
        assert sanitize_null_value(None, "websites") == []

    def test_sanitize_dict_fallback(self):
        assert sanitize_null_value(None, "raw_metadata") == {}
        assert sanitize_null_value(None, "metadata") == {}

    def test_sanitize_nested_record(self):
        record = {
            "name": "Luxury Villa",
            "description": None,
            "priceFrom": None,
            "has_pool": None,
            "images": None,
            "raw_metadata": None,
            "nested_info": {
                "sub_description": None,
                "amount": None,
            }
        }
        cleaned = sanitize_db_record(record)
        assert cleaned["name"] == "Luxury Villa"
        assert cleaned["description"] == ""
        assert cleaned["priceFrom"] == 0
        assert cleaned["has_pool"] is False
        assert cleaned["images"] == []
        assert cleaned["raw_metadata"] == {}
        assert cleaned["nested_info"]["sub_description"] == ""
        assert cleaned["nested_info"]["amount"] == 0

    def test_preserves_valid_values(self):
        record = {
            "name": "Ocean View",
            "description": "Stunning sea view",
            "priceFrom": 15000000.0,
            "has_pool": True,
            "images": ["https://example.com/1.jpg"],
        }
        cleaned = sanitize_db_record(record)
        assert cleaned == record


class TestNullHandlingValidationAndPersistenceFlow:
    """End-to-end integration tests for Path A (Admin warning & reject) and Path B (Sanitized save)."""

    def test_path_a_null_values_warn_admin_and_block_save(self):
        """Path A: If incoming data contains required null/empty fields, warn admin and do NOT save."""
        incomplete_payload = {
            "property_id": "prop-test-null-rejection",
            "mode": "create",
            "fields": {
                "property_name": "Incomplete Penthouse",
                "property_type": "Penthouse",
                "location": None,       # null critical field
                "price_display": None,  # null critical field
                "status": "Available"
            }
        }
        is_valid, missing, err_msg = validate_property_data(incomplete_payload)
        assert is_valid is False
        assert "location" in missing
        assert "price" in missing
        assert "Without proper information I am not able to complete this task" in err_msg

    def test_path_b_approved_record_sanitizes_nulls_to_defaults_on_db_save(self):
        """Path B: When saved to database, any non-critical null is replaced with typed defaults."""
        mock_doc_ref = MagicMock()
        mock_doc = MagicMock()
        mock_doc.exists = True
        mock_doc.to_dict.return_value = {"name": "Existing Villa"}
        mock_doc_ref.get.return_value = mock_doc

        mock_client = MagicMock()
        mock_client.collection.return_value.document.return_value = mock_doc_ref

        # Call save_or_update_property_firestore with some null non-critical fields
        fields_with_nulls = {
            "property_name": "Sanitized Villa",
            "description": None,           # string -> ""
            "price_numeric_max": None,     # numeric -> 0
            "has_pool": None,              # boolean -> False
        }

        from unittest.mock import patch
        with patch("app.services.firestore_service.get_firestore_client", return_value=mock_client):
            success = save_or_update_property_firestore(
                property_id="sanitized-villa-01",
                fields=fields_with_nulls,
                images=None,
                cover_image=None,
                target="admin",
            )
            assert success is True

            # Verify that doc_ref.set was called with sanitized payload
            mock_doc_ref.set.assert_called_once()
            args, kwargs = mock_doc_ref.set.call_args
            written_payload = args[0]

            assert written_payload.get("name") == "Sanitized Villa"
            assert written_payload.get("description") == ""
            # Verify no None values exist in the written payload
            for k, v in written_payload.items():
                assert v is not None, f"Key '{k}' was saved with None value to database!"

