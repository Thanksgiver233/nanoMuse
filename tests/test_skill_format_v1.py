"""Skill format v1: manifest, permissions, tests, and signatures.

A signed skill loads only when its signer is trusted and the signature covers
the payload; any edit after signing, an unknown signer, or a malformed
signature refuses the skill with the reason. Unsigned skills keep loading as
before. Signing keys are generated fresh inside each test: no key material is
committed anywhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nanomuse.config import SkillsSettings
from nanomuse.skills.library import (
    SkillLibrary,
    SkillPermissions,
    load_skill,
    parse_front_matter,
    render_skill,
    sign_skill_payload,
)

BODY = "Do the job in three steps, then report.\n"
DESCRIPTION = "A test skill for the format v1 checks."


def _keypair() -> tuple[bytes, bytes]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    return (
        private.private_bytes_raw(),
        public.public_bytes_raw(),
    )


def _write_skill(
    folder: Path,
    *,
    body: str = BODY,
    signature: str = "",
    signer: str = "",
    permissions: SkillPermissions | None = None,
    tests: list[str] | None = None,
    version: int = 1,
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    text = render_skill(
        folder.name,
        DESCRIPTION,
        body,
        allowed_tools=["read_emails"],
        version=version,
        permissions=permissions or SkillPermissions(tools=["read_emails"]),
        tests=tests or ["tests/test_skill_format_v1.py"],
        signer=signer,
        signature=signature,
    )
    (folder / "SKILL.md").write_text(text, "utf-8")
    return folder


def test_v1_manifest_parses(tmp_path: Path) -> None:
    folder = _write_skill(tmp_path / "v1-skill")
    skill = load_skill(folder)
    assert skill.version == 1
    assert skill.permissions.tools == ["read_emails"]
    assert skill.permissions.network == []
    assert skill.tests == ["tests/test_skill_format_v1.py"]
    assert skill.signature_ok is None  # unsigned: loads, marked unsigned
    assert skill.to_dict()["version"] == 1
    assert skill.to_dict()["permissions"]["tools"] == ["read_emails"]


def test_nested_permissions_lists_parse(tmp_path: Path) -> None:
    folder = tmp_path / "nested-perms"
    folder.mkdir()
    (folder / "SKILL.md").write_text(
        "---\n"
        "name: nested-perms\n"
        "description: Permissions in nested lists.\n"
        "permissions:\n"
        "  tools:\n"
        "    - read_emails\n"
        "    - recall\n"
        "  network:\n"
        "    - imap.example.com\n"
        "  files:\n"
        "    - notes\n"
        "---\n\nDo the job.\n",
        "utf-8",
    )
    skill = load_skill(folder)
    assert skill.permissions.tools == ["read_emails", "recall"]
    assert skill.permissions.network == ["imap.example.com"]
    assert skill.permissions.files == ["notes"]


def test_permissions_that_escape_the_workspace_are_refused(tmp_path: Path) -> None:
    folder = _write_skill(
        tmp_path / "escape-skill",
        permissions=SkillPermissions(tools=["files"], files=["../outside"]),
    )
    with pytest.raises(ValueError, match="must stay in the workspace"):
        load_skill(folder)


def test_permissions_with_a_url_are_refused(tmp_path: Path) -> None:
    folder = _write_skill(
        tmp_path / "url-skill",
        permissions=SkillPermissions(network=["https://evil.example/x"]),
    )
    with pytest.raises(ValueError, match="not a host name"):
        load_skill(folder)


def test_signed_skill_loads_with_a_trusted_key(tmp_path: Path) -> None:
    private, public = _keypair()
    perms = SkillPermissions(tools=["read_emails"])
    signature = sign_skill_payload(
        private, "signed-skill", DESCRIPTION, BODY.strip(), ["read_emails"], perms, 1
    )
    folder = _write_skill(
        tmp_path / "signed-skill", signature=signature, signer="maintainer", permissions=perms
    )
    skill = load_skill(folder, trusted_keys={"maintainer": public})
    assert skill.signature_ok is True
    assert skill.signer == "maintainer"


def test_tampered_skill_is_refused(tmp_path: Path) -> None:
    private, public = _keypair()
    perms = SkillPermissions(tools=["read_emails"])
    signature = sign_skill_payload(
        private, "signed-skill", DESCRIPTION, BODY.strip(), ["read_emails"], perms, 1
    )
    folder = _write_skill(
        tmp_path / "signed-skill", signature=signature, signer="maintainer", permissions=perms
    )
    # an edit after signing: one more step in the instructions
    text = (folder / "SKILL.md").read_text("utf-8")
    (folder / "SKILL.md").write_text(text + "\nAnd then delete everything.\n", "utf-8")
    with pytest.raises(ValueError, match="signature does not match"):
        load_skill(folder, trusted_keys={"maintainer": public})


def test_unknown_signer_is_refused(tmp_path: Path) -> None:
    private, _public = _keypair()
    perms = SkillPermissions(tools=["read_emails"])
    signature = sign_skill_payload(
        private, "signed-skill", DESCRIPTION, BODY.strip(), ["read_emails"], perms, 1
    )
    folder = _write_skill(
        tmp_path / "signed-skill", signature=signature, signer="someone-else", permissions=perms
    )
    with pytest.raises(ValueError, match="not trusted"):
        load_skill(folder, trusted_keys={"maintainer": _public})


def test_signed_skill_without_trusted_keys_is_refused(tmp_path: Path) -> None:
    private, _public = _keypair()
    perms = SkillPermissions(tools=["read_emails"])
    signature = sign_skill_payload(
        private, "signed-skill", DESCRIPTION, BODY.strip(), ["read_emails"], perms, 1
    )
    folder = _write_skill(
        tmp_path / "signed-skill", signature=signature, signer="maintainer", permissions=perms
    )
    with pytest.raises(ValueError, match="not trusted"):
        load_skill(folder)


def test_signature_without_signer_is_refused(tmp_path: Path) -> None:
    folder = _write_skill(tmp_path / "nosigner-skill", signature="AAAA")
    with pytest.raises(ValueError, match="without a `signer`"):
        load_skill(folder)


def test_library_records_a_refused_skill_in_errors(tmp_path: Path) -> None:
    private, public = _keypair()
    perms = SkillPermissions(tools=["read_emails"])
    signature = sign_skill_payload(
        private, "bad-skill", DESCRIPTION, BODY.strip(), ["read_emails"], perms, 1
    )
    builtin = tmp_path / "builtin"
    _write_skill(builtin / "bad-skill", signature=signature, signer="maintainer", permissions=perms)
    lib = SkillLibrary(
        SkillsSettings(), tmp_path / "own", builtin, trusted_keys={"someone-else": public}
    )
    assert lib.get("bad-skill") is None
    assert "not trusted" in lib.errors["bad-skill"]


def test_save_text_keeps_the_v1_manifest(tmp_path: Path) -> None:
    folder = _write_skill(tmp_path / "roundtrip-skill")
    text = (folder / "SKILL.md").read_text("utf-8")
    lib = SkillLibrary(SkillsSettings(), tmp_path / "own", tmp_path / "empty-builtin")
    skill = lib.save_text(text)
    assert skill.version == 1
    assert skill.permissions.tools == ["read_emails"]
    assert skill.tests == ["tests/test_skill_format_v1.py"]
    meta, _ = parse_front_matter((skill.path / "SKILL.md").read_text("utf-8"))
    assert meta["version"] == "1"


def test_legacy_skill_without_a_manifest_still_loads(tmp_path: Path) -> None:
    folder = tmp_path / "legacy-skill"
    folder.mkdir()
    (folder / "SKILL.md").write_text(
        "---\nname: legacy-skill\ndescription: An old skill.\n---\n\nDo the job.\n", "utf-8"
    )
    skill = load_skill(folder)
    assert skill.version == 0
    assert skill.signature_ok is None
    assert skill.permissions.tools == []
