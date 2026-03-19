"""Tests for Fibery integration: webhook parsing, repo field parsing,
thread ID generation, auth routing, and utility functions."""

from __future__ import annotations

import asyncio
import hashlib
from unittest.mock import AsyncMock, patch

import pytest

from agent.utils import auth
from agent.webapp import (
    generate_thread_id_from_fibery_entity,
    parse_repo_field,
)


# ---------------------------------------------------------------------------
# parse_repo_field
# ---------------------------------------------------------------------------


class TestParseRepoField:
    def test_single_repo(self) -> None:
        result = parse_repo_field("langchain-ai/open-swe")
        assert result == [{"owner": "langchain-ai", "name": "open-swe"}]

    def test_multi_repo(self) -> None:
        result = parse_repo_field("org/repo1, org/repo2")
        assert result == [
            {"owner": "org", "name": "repo1"},
            {"owner": "org", "name": "repo2"},
        ]

    def test_empty_string(self) -> None:
        assert parse_repo_field("") == []

    def test_none_like(self) -> None:
        assert parse_repo_field("   ") == []

    def test_invalid_no_slash(self) -> None:
        assert parse_repo_field("just-a-name") == []

    def test_mixed_valid_invalid(self) -> None:
        result = parse_repo_field("org/repo1, bad-entry, org/repo2")
        assert result == [
            {"owner": "org", "name": "repo1"},
            {"owner": "org", "name": "repo2"},
        ]

    def test_whitespace_handling(self) -> None:
        result = parse_repo_field("  org / repo  ")
        assert result == [{"owner": "org", "name": "repo"}]


# ---------------------------------------------------------------------------
# generate_thread_id_from_fibery_entity
# ---------------------------------------------------------------------------


class TestGenerateThreadId:
    def test_deterministic(self) -> None:
        id1 = generate_thread_id_from_fibery_entity("abc-123")
        id2 = generate_thread_id_from_fibery_entity("abc-123")
        assert id1 == id2

    def test_different_ids_produce_different_threads(self) -> None:
        id1 = generate_thread_id_from_fibery_entity("entity-1")
        id2 = generate_thread_id_from_fibery_entity("entity-2")
        assert id1 != id2

    def test_format_is_uuid_like(self) -> None:
        result = generate_thread_id_from_fibery_entity("test-entity")
        parts = result.split("-")
        assert len(parts) == 5
        assert len(parts[0]) == 8
        assert len(parts[1]) == 4
        assert len(parts[2]) == 4
        assert len(parts[3]) == 4
        assert len(parts[4]) == 12

    def test_uses_fibery_prefix(self) -> None:
        entity_id = "test-id"
        expected_hash = hashlib.sha256(f"fibery-entity:{entity_id}".encode()).hexdigest()
        result = generate_thread_id_from_fibery_entity(entity_id)
        assert result.replace("-", "")[:32] == expected_hash[:32]


# ---------------------------------------------------------------------------
# leave_failure_comment — Fibery source
# ---------------------------------------------------------------------------


class TestLeaveFailureCommentFibery:
    def test_posts_comment_to_fibery_entity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called: dict[str, str] = {}

        async def fake_fibery_create_comment(
            database_type: str, entity_id: str, message: str
        ) -> bool:
            called["database_type"] = database_type
            called["entity_id"] = entity_id
            called["message"] = message
            return True

        monkeypatch.setattr(auth, "fibery_create_comment", fake_fibery_create_comment)
        monkeypatch.setattr(
            auth,
            "get_config",
            lambda: {
                "configurable": {
                    "fibery_entity": {
                        "id": "entity-uuid",
                        "database_type": "App/Task",
                    }
                }
            },
        )

        asyncio.run(auth.leave_failure_comment("fibery", "auth failed"))

        assert called == {
            "database_type": "App/Task",
            "entity_id": "entity-uuid",
            "message": "auth failed",
        }

    def test_no_op_when_fibery_entity_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Should not raise when fibery_entity is missing from config."""

        async def fake_fibery_create_comment(
            database_type: str, entity_id: str, message: str
        ) -> bool:
            raise AssertionError("Should not be called")

        monkeypatch.setattr(auth, "fibery_create_comment", fake_fibery_create_comment)
        monkeypatch.setattr(
            auth,
            "get_config",
            lambda: {"configurable": {}},
        )

        # Should not raise
        asyncio.run(auth.leave_failure_comment("fibery", "auth failed"))


# ---------------------------------------------------------------------------
# Auth helper functions — Fibery source
# ---------------------------------------------------------------------------


class TestAuthHelpersFibery:
    def test_retry_instruction(self) -> None:
        result = auth._retry_instruction("fibery")
        assert "@openswe" in result

    def test_source_account_label(self) -> None:
        assert auth._source_account_label("fibery") == "Fibery"

    def test_work_item_label(self) -> None:
        assert auth._work_item_label("fibery") == "entity"


# ---------------------------------------------------------------------------
# Fibery utility functions (mocked HTTP)
# ---------------------------------------------------------------------------


class TestFiberyUtils:
    def test_create_comment_returns_false_without_credentials(self) -> None:
        from agent.utils import fibery

        with patch.object(fibery, "FIBERY_API_TOKEN", ""), patch.object(
            fibery, "FIBERY_WORKSPACE_URL", ""
        ):
            result = asyncio.run(
                fibery.create_comment("App/Task", "entity-id", "Hello")
            )
            assert result is False

    def test_update_entity_state_returns_false_without_credentials(self) -> None:
        from agent.utils import fibery

        with patch.object(fibery, "FIBERY_API_TOKEN", ""), patch.object(
            fibery, "FIBERY_WORKSPACE_URL", ""
        ):
            result = asyncio.run(
                fibery.update_entity_state("App/Task", "entity-id", "Done")
            )
            assert result is False

    def test_fetch_entity_returns_none_without_credentials(self) -> None:
        from agent.utils import fibery

        with patch.object(fibery, "FIBERY_API_TOKEN", ""), patch.object(
            fibery, "FIBERY_WORKSPACE_URL", ""
        ):
            result = asyncio.run(fibery.fetch_entity("App/Task", "entity-id"))
            assert result is None

    def test_fetch_user_email_returns_none_without_credentials(self) -> None:
        from agent.utils import fibery

        with patch.object(fibery, "FIBERY_API_TOKEN", ""), patch.object(
            fibery, "FIBERY_WORKSPACE_URL", ""
        ):
            result = asyncio.run(fibery.fetch_user_email("user-id"))
            assert result is None
