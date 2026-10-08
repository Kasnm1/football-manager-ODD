from pathlib import Path

from tools import player_languages


def test_player_language_api_remains_a_compatibility_wrapper(monkeypatch):
    calls = []
    monkeypatch.setattr(player_languages, "apply_person_language_level", lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True})
    assert player_languages.apply_player_language_level(1, "0x10", 2, 3) == {"ok": True}
    assert calls == [((1, "0x10", 2, 3), {"person_kind": "player"})]


def test_person_language_writer_has_staff_identity_and_rollback_contract():
    source = Path(player_languages.__file__).read_text(encoding="utf-8")
    assert 'person_kind not in {"player", "staff"}' in source
    assert "resolve_public_person_address" in source
    assert '"person_kind": person_kind' in source
    assert "restore_person_language_level" in source
    assert "layout.staff_language_write_verified" in source
    assert "def read_person_languages(" in source
