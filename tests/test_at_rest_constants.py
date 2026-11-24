"""Pins identifiers that are baked into data which already exists off-box.

These are NOT brand strings. Each one is either authenticated input to a
cipher, a lookup key in an OS keychain, marker text sitting in files on a
deployed host, or a label on a container that is running right now. Renaming
any of them does not "rebrand" anything — it makes the running system stop
recognising its own data, usually silently.

This file exists because the lionclaw → olano rename did exactly that: a sweep
rewrote the vault's AES-GCM associated data, and every secret on every deployed
instance became undecryptable while reporting "wrong passphrase or corrupted
data" for a passphrase that was entirely correct.

If a change here fails, the fix is almost never to update the expected value.
It is to leave the constant alone. Changing one of these for real requires a
migration that rewrites the existing data first.
"""

from __future__ import annotations


def test_vault_associated_data_is_unchanged() -> None:
    """AES-GCM AAD — authenticated input to every secret already on disk."""
    from olano.vault.store import _VAULT_ASSOCIATED_DATA

    assert _VAULT_ASSOCIATED_DATA == b"lionclaw.vault.v2"


def test_vault_keyring_service_is_unchanged() -> None:
    """OS keychain service name — the lookup key for stored master keys."""
    from olano.vault.keyring_backend import _VAULT_KEYRING_SERVICE

    assert _VAULT_KEYRING_SERVICE == "lionclaw"


def test_vault_store_uses_the_shared_keyring_service_name() -> None:
    """store.py had its own copy of the service name, and the copy drifted.

    Two literals meant the rename hit one and missed the other, so the master
    key would be written under one service name and looked up under another —
    silently minting a fresh ephemeral key instead of finding the real one.
    Assert the call site actually reaches for the shared constant.
    """
    import inspect

    from olano.vault.store import VaultStore

    src = inspect.getsource(VaultStore._get_or_create_keyring_key)
    assert "_VAULT_KEYRING_SERVICE" in src, (
        "vault/store.py must use the shared keyring service constant, not a literal"
    )
    assert '"olano"' not in src and "'olano'" not in src, (
        "vault/store.py still holds a hardcoded keyring service name"
    )


def test_checkpoint_aes_key_env_is_unchanged() -> None:
    """Env var naming the checkpoint at-rest key — langgraph's own name.

    Every encrypted checkpoint row carries ``+aes`` in its type string and can
    only be decoded through the serializer this variable configures. A box
    whose compose env or unit file still sets it under the old name comes up
    with no key, and every encrypted thread on it is unreadable — silently,
    on the next boot.
    """
    from olano.core.checkpoints import CHECKPOINT_AES_KEY_ENV

    assert CHECKPOINT_AES_KEY_ENV == "LANGGRAPH_AES_KEY"


def test_persistent_sandbox_container_name_is_unchanged() -> None:
    """Names containers already running on deployed hosts.

    _start_persistent finds an existing container with `docker inspect <name>`
    and reuses it, so a renamed prefix orphans the live container and builds a
    duplicate beside it.
    """
    from pathlib import Path

    from olano.backends.lionclaw_docker import _persistent_container_name

    assert _persistent_container_name("merli", Path("/tmp/ws")).startswith("lionclaw-")


def test_workspace_first_run_markers_are_unchanged() -> None:
    """Marker text already written into workspace files on every install."""
    from olano.archetypes.installer import _BLANK_PERSONA_MARKER, _FIRST_RUN_SENTINEL
    from olano.config.workspace import _FIRST_RUN_MARKER

    assert _FIRST_RUN_SENTINEL == "<!-- lionclaw:first-run -->"
    assert _BLANK_PERSONA_MARKER == "<!-- lionclaw:blank-persona -->"
    assert _FIRST_RUN_MARKER == "<!-- lionclaw:first-run -->\n"
    # The installer and the workspace module must agree, or a workspace written
    # by one is invisible to the other.
    assert _FIRST_RUN_MARKER.strip() == _FIRST_RUN_SENTINEL


def test_sandbox_container_label_is_unchanged() -> None:
    """Docker label on sandbox containers running on deployed hosts."""
    from olano.backends.lionclaw_docker import _LABEL_KEY

    assert _LABEL_KEY == "lionclaw.signature"


def test_vault_roundtrip_survives_the_rename() -> None:
    """End-to-end proof, not just a string compare.

    Encrypts with the pinned AAD and decrypts it back. This is what actually
    broke: the constant compare above would pass a careless "update the
    expected value" fix, but this fails the moment ciphertext written by one
    build cannot be read by another.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from olano.vault.store import _VAULT_ASSOCIATED_DATA

    key = AESGCM.generate_key(bit_length=256)
    nonce = b"\x00" * 12
    secret = b"super-secret-token"

    ciphertext = AESGCM(key).encrypt(nonce, secret, _VAULT_ASSOCIATED_DATA)
    assert AESGCM(key).decrypt(nonce, ciphertext, _VAULT_ASSOCIATED_DATA) == secret

    # And prove the failure mode: a different AAD must NOT decrypt, which is
    # exactly why renaming the constant orphaned every existing vault.
    import pytest
    from cryptography.exceptions import InvalidTag

    with pytest.raises(InvalidTag):
        AESGCM(key).decrypt(nonce, ciphertext, b"olano.vault.v2")


def test_no_inline_copies_of_the_on_disk_markers(tmp_path) -> None:
    """The readers must match the writer, and match it by reference.

    A module constant scan does not catch an inline literal, and that is
    exactly how the blank-persona check ended up looking for
    "olano:blank-persona" while the installer kept writing
    "lionclaw:blank-persona" — a check that silently never fires on a real
    deployment. Assert on behaviour, not on grep: write the marker the way the
    installer does, and confirm each consumer still recognises it.
    """
    from olano.archetypes.installer import _BLANK_PERSONA_MARKER, _FIRST_RUN_SENTINEL
    from olano.core.system_middleware import _is_blank_persona_identity

    identity = f"# Identity\n\n{_BLANK_PERSONA_MARKER}\n**Name:** *Not chosen yet*\n"
    assert _is_blank_persona_identity(identity), (
        "the blank-persona check no longer recognises the marker the installer writes"
    )

    from olano.config.workspace import BOOTSTRAP_DEFAULTS, is_scaffold_default

    seeded = _FIRST_RUN_SENTINEL + "\n" + BOOTSTRAP_DEFAULTS["IDENTITY.md"]
    assert is_scaffold_default("IDENTITY.md", seeded), (
        "a freshly seeded workspace file is no longer detected as a scaffold"
    )


def test_builtin_skill_names_match_their_directories() -> None:
    """deepagents requires SKILL.md `name:` == directory name.

    The rename sweep rewrote the frontmatter of `lionclaw-admin/SKILL.md` to
    `olano-admin` but left the directory, which made deepagents log a spec
    violation on every agent boot. Half-renaming a skill is easy to miss
    because nothing fails loudly — it only warns.
    """
    from pathlib import Path

    import olano.skills as skills_pkg

    built_in = Path(skills_pkg.__file__).parent / "built_in"
    mismatches = []
    for d in sorted(p for p in built_in.iterdir() if p.is_dir()):
        skill_md = d / "SKILL.md"
        if not skill_md.exists():
            continue
        for line in skill_md.read_text(encoding="utf-8").splitlines():
            if line.startswith("name:"):
                declared = line.split(":", 1)[1].strip()
                if declared != d.name:
                    mismatches.append(f"{d.name}/SKILL.md declares name: {declared}")
                break
    assert not mismatches, "skill name/directory mismatch: " + "; ".join(mismatches)


def test_checkpoint_require_encrypted_env_is_unchanged() -> None:
    """Env var that makes the checkpoint serializer refuse plaintext rows.

    A sealed box sets it beside ``LANGGRAPH_AES_KEY`` in its runtime env. A
    build that reads it under another name comes up permissive, and every
    plaintext row on the box decodes again as if nothing were wrong. Nothing
    fails when that happens; the guarantee is simply gone.
    """
    from olano.core.checkpoints import CHECKPOINT_REQUIRE_ENCRYPTED_ENV

    assert CHECKPOINT_REQUIRE_ENCRYPTED_ENV == "LIONCLAW_CHECKPOINT_REQUIRE_ENCRYPTED"
