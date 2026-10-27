"""The provider catalogue fails loudly when a field is missing or mistyped.

``providers.json`` is the source of truth for which features exist, so a catalogue
that says less than it means must not load quietly: every check in
``validate_catalogue`` is exercised here once on the packaged file (which must pass)
and once on a mutated copy (which must name the fault).
"""

from __future__ import annotations

import copy
import json
from typing import Any

from nanomuse.llm.catalogue import CATALOGUE_FILE, validate_catalogue


def _data() -> dict[str, Any]:
    return json.loads(CATALOGUE_FILE.read_text("utf-8"))


def _first(data: dict[str, Any]) -> dict[str, Any]:
    return data["providers"][0]


def test_the_packaged_catalogue_passes_validation() -> None:
    assert validate_catalogue(_data()) == []


def test_a_catalogue_without_providers_fails() -> None:
    data = _data()
    data["providers"] = []
    assert validate_catalogue(data) == ["'providers' must be a non-empty list"]


def test_a_non_object_catalogue_fails() -> None:
    assert validate_catalogue(["bailian"]) == ["catalogue must be a JSON object"]


def test_a_bad_version_fails() -> None:
    data = _data()
    data["version"] = "1"
    assert any("'version'" in e for e in validate_catalogue(data))


def test_a_missing_id_fails() -> None:
    data = _data()
    del _first(data)["id"]
    assert any("'id'" in e for e in validate_catalogue(data))


def test_a_duplicate_id_fails() -> None:
    data = _data()
    data["providers"][1]["id"] = data["providers"][0]["id"]
    assert any("duplicate id" in e for e in validate_catalogue(data))


def test_a_missing_name_fails() -> None:
    data = _data()
    _first(data)["name"] = ""
    assert any("'name'" in e for e in validate_catalogue(data))


def test_an_unknown_protocol_fails() -> None:
    data = _data()
    _first(data)["protocol"] = "grpc"
    assert any("'protocol'" in e for e in validate_catalogue(data))


def test_empty_capabilities_fail() -> None:
    data = _data()
    _first(data)["capabilities"] = []
    assert any("'capabilities'" in e for e in validate_catalogue(data))


def test_an_unknown_capability_fails() -> None:
    data = _data()
    _first(data)["capabilities"] = ["chat", "audio"]
    assert any("unknown capability 'audio'" in e for e in validate_catalogue(data))


def test_an_unknown_auth_kind_fails() -> None:
    data = _data()
    _first(data)["auth"] = ["key", "oauth-magic"]
    assert any("unknown auth kind 'oauth-magic'" in e for e in validate_catalogue(data))


def test_empty_regions_fail() -> None:
    data = _data()
    _first(data)["regions"] = []
    assert any("'regions'" in e for e in validate_catalogue(data))


def test_a_missing_base_url_fails() -> None:
    data = _data()
    _first(data)["base_url"] = ""
    assert any("'base_url'" in e for e in validate_catalogue(data))


def test_a_base_url_that_is_not_a_url_fails() -> None:
    data = _data()
    _first(data)["base_url"] = "dashscope.aliyuncs.com/v1"
    assert any("'base_url'" in e for e in validate_catalogue(data))


def test_a_key_auth_provider_without_a_key_page_fails() -> None:
    data = _data()
    _first(data)["key_url"] = ""
    assert any("'key_url' is required" in e for e in validate_catalogue(data))


def test_an_unknown_defaults_key_fails() -> None:
    data = _data()
    _first(data)["defaults"]["audio"] = "some-model"
    assert any("unknown defaults key 'audio'" in e for e in validate_catalogue(data))


def test_an_empty_default_model_fails() -> None:
    data = _data()
    _first(data)["defaults"]["chat"] = "  "
    assert any("defaults['chat']" in e for e in validate_catalogue(data))


def test_vision_without_a_hands_default_fails() -> None:
    data = _data()
    del _first(data)["defaults"]["hands"]
    assert any("defaults['hands']" in e for e in validate_catalogue(data))


def test_image_without_an_image_default_fails() -> None:
    data = _data()
    del _first(data)["defaults"]["image"]
    assert any("defaults['image']" in e for e in validate_catalogue(data))


def test_a_missing_verified_date_fails() -> None:
    data = _data()
    del _first(data)["verified"]
    assert any("'verified'" in e for e in validate_catalogue(data))


def test_a_verified_date_in_words_fails() -> None:
    data = _data()
    _first(data)["verified"] = "October 2026"
    assert any("'verified'" in e for e in validate_catalogue(data))


def test_an_unknown_field_fails() -> None:
    data = _data()
    _first(data)["capability"] = ["chat"]
    assert any("unknown field 'capability'" in e for e in validate_catalogue(data))


def test_auth_capabilities_for_an_auth_the_entry_lacks_fails() -> None:
    data = _data()
    _first(data)["auth_capabilities"] = {"oauth-chatgpt": ["chat"]}
    assert any("not in 'auth'" in e for e in validate_catalogue(data))


def test_a_reasoning_block_with_a_bad_regex_fails() -> None:
    data = _data()
    _first(data)["reasoning"] = {
        "doc": "https://example.com/docs",
        "wire": "reasoning_effort",
        "rules": [{"models": "^deepseek-(", "levels": ["low"]}],
    }
    assert any("not a valid regex" in e for e in validate_catalogue(data))


def test_a_reasoning_block_without_a_doc_url_fails() -> None:
    data = _data()
    _first(data)["reasoning"] = {"wire": "reasoning_effort", "rules": []}
    assert any("'reasoning.doc'" in e for e in validate_catalogue(data))


def test_the_custom_row_needs_no_endpoint_key_page_or_defaults() -> None:
    data = _data()
    custom = next(p for p in data["providers"] if p["id"] == "custom")
    assert custom["base_url"] in (None, "")
    assert validate_catalogue(data) == []
    mutated = copy.deepcopy(data)
    custom_mutated = next(p for p in mutated["providers"] if p["id"] == "custom")
    custom_mutated["base_url"] = "not a url"
    assert any("'base_url'" in e for e in validate_catalogue(mutated))
