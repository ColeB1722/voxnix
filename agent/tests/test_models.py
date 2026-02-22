"""Tests for container spec Pydantic models.

TDD — these tests define the contract for the agent-to-Nix boundary.
The ContainerSpec model must produce JSON that mkContainer.nix can consume.
"""

import json

import pytest
from pydantic import ValidationError

from agent.nix_gen.models import ContainerSpec


class TestContainerSpecValid:
    """Happy path — valid specs that mkContainer should accept."""

    def test_minimal_spec(self):
        spec = ContainerSpec(name="dev-abc", owner="chat_123", modules=["git"])
        assert spec.name == "dev-abc"
        assert spec.owner == "chat_123"
        assert spec.modules == ["git"]

    def test_all_current_modules(self):
        spec = ContainerSpec(
            name="dev-full",
            owner="chat_456",
            modules=["git", "fish", "workspace"],
        )
        assert len(spec.modules) == 3

    def test_single_module(self):
        spec = ContainerSpec(name="minimal", owner="chat_1", modules=["fish"])
        assert spec.modules == ["fish"]

    def test_hyphenated_name(self):
        spec = ContainerSpec(name="my-dev", owner="chat_1", modules=["git"])
        assert spec.name == "my-dev"

    def test_max_length_name(self):
        """11 characters is the maximum allowed."""
        spec = ContainerSpec(name="abcde-fghij", owner="chat_1", modules=["git"])
        assert spec.name == "abcde-fghij"

    def test_numeric_owner(self):
        """Telegram chat IDs are numeric strings."""
        spec = ContainerSpec(name="dev", owner="123456789", modules=["git"])
        assert spec.owner == "123456789"


class TestContainerSpecNameValidation:
    """Container names must be valid for systemd-nspawn / NixOS containers."""

    def test_name_too_long_rejected(self):
        """Names over 11 chars are rejected — privateNetwork interface name limit."""
        with pytest.raises(ValidationError, match="too long"):
            ContainerSpec(name="my-dev-container", owner="chat_1", modules=["git"])

    def test_exactly_12_chars_rejected(self):
        with pytest.raises(ValidationError, match="too long"):
            ContainerSpec(name="abcde-fghijk", owner="chat_1", modules=["git"])

    def test_empty_name_rejected(self):
        with pytest.raises(ValidationError, match="name"):
            ContainerSpec(name="", owner="chat_1", modules=["git"])

    def test_uppercase_rejected(self):
        with pytest.raises(ValidationError, match="name"):
            ContainerSpec(name="MyContainer", owner="chat_1", modules=["git"])

    def test_spaces_rejected(self):
        with pytest.raises(ValidationError, match="name"):
            ContainerSpec(name="my container", owner="chat_1", modules=["git"])

    def test_leading_hyphen_rejected(self):
        with pytest.raises(ValidationError, match="name"):
            ContainerSpec(name="-bad", owner="chat_1", modules=["git"])

    def test_trailing_hyphen_rejected(self):
        with pytest.raises(ValidationError, match="name"):
            ContainerSpec(name="bad-", owner="chat_1", modules=["git"])

    def test_special_characters_rejected(self):
        with pytest.raises(ValidationError, match="name"):
            ContainerSpec(name="bad@name!", owner="chat_1", modules=["git"])

    def test_dots_rejected(self):
        with pytest.raises(ValidationError, match="name"):
            ContainerSpec(name="bad.name", owner="chat_1", modules=["git"])


class TestContainerSpecOwnerValidation:
    """Owner field must be a non-empty string."""

    def test_empty_owner_rejected(self):
        with pytest.raises(ValidationError, match="owner"):
            ContainerSpec(name="dev", owner="", modules=["git"])


class TestContainerSpecModulesValidation:
    """Module list validation."""

    def test_empty_modules_rejected(self):
        with pytest.raises(ValidationError, match="modules"):
            ContainerSpec(name="dev", owner="chat_1", modules=[])

    def test_duplicate_modules_rejected(self):
        with pytest.raises(ValidationError, match="modules"):
            ContainerSpec(name="dev", owner="chat_1", modules=["git", "git"])


class TestContainerSpecSerialization:
    """JSON output must match what mkContainer.nix expects."""

    def test_json_has_required_keys(self):
        spec = ContainerSpec(name="dev-abc", owner="chat_123", modules=["git", "fish"])
        data = json.loads(spec.model_dump_json())
        assert set(data.keys()) >= {"name", "owner", "modules"}

    def test_json_name_is_string(self):
        spec = ContainerSpec(name="dev", owner="chat_1", modules=["git"])
        data = json.loads(spec.model_dump_json())
        assert isinstance(data["name"], str)

    def test_json_modules_is_list_of_strings(self):
        spec = ContainerSpec(name="dev", owner="chat_1", modules=["git", "fish"])
        data = json.loads(spec.model_dump_json())
        assert isinstance(data["modules"], list)
        assert all(isinstance(m, str) for m in data["modules"])

    def test_json_roundtrip(self):
        """Spec can be serialized and deserialized without loss."""
        original = ContainerSpec(
            name="dev-abc",
            owner="chat_123",
            modules=["git", "fish", "workspace"],
        )
        json_str = original.model_dump_json()
        restored = ContainerSpec.model_validate_json(json_str)
        assert original == restored

    def test_json_no_extra_fields(self):
        """Serialized JSON should not contain unexpected fields."""
        spec = ContainerSpec(name="dev", owner="chat_1", modules=["git"])
        data = json.loads(spec.model_dump_json())
        expected_keys = {"name", "owner", "modules"}
        assert set(data.keys()) == expected_keys
