from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from tools.player_aliases import apply_player_aliases
from tools import app_settings
from tools.player_name_localization import (
    apply_installed_player_names,
    clear_installed_player_name_cache,
    load_installed_player_names,
    parse_player_name_lnc,
    parse_player_name_xml_parts,
    parse_player_name_xml,
)


def test_parse_player_name_lnc_uses_uid_scoped_chinese_common_names(tmp_path: Path):
    source = tmp_path / "人员汉化.lnc"
    source.write_text(
        '\n'.join((
            '# "CHANGE_PLAYER_NAME" 1 "Ignored" "忽略" ""',
            '"CHANGE_PLAYER_NAME" 917372 "Didier Digard" "迪加尔" ""',
            '"CHANGE_PLAYER_NAME" 49062287 "Arouna Sangante" "桑甘特" ""',
            '"CHANGE_PLAYER_NAME" 7 "English Only" "English" ""',
        )),
        encoding="utf-8",
    )

    assert parse_player_name_lnc(source) == {
        917372: "迪加尔",
        49062287: "桑甘特",
    }


def test_parse_player_name_lnc_accepts_utf16_little_endian_files(tmp_path: Path):
    source = tmp_path / "人员汉化.lnc"
    source.write_text(
        '"CHANGE_PLAYER_NAME" 7 "Player Seven" "七号球员" ""',
        encoding="utf-16",
    )

    assert parse_player_name_lnc(source) == {7: "七号球员"}


def test_parse_player_name_xml_extracts_type_one_uid_and_prefers_common_name(
    tmp_path: Path,
):
    source = tmp_path / "数据库人员汉化.xml"
    source.write_text(
        """<record><list id=\"db_changes\"><record>
        <integer id=\"database_table_type\" value=\"1\"/>
        <large id=\"db_unique_id\" value=\"5589406015806345\"/>
        <unsigned id=\"property\" value=\"1348889710\"/>
        <string id=\"new_value\" value=\"奥列格·萨连科全名\"/>
        </record><record>
        <integer id=\"database_table_type\" value=\"1\"/>
        <large id=\"db_unique_id\" value=\"5589406015806345\"/>
        <unsigned id=\"property\" value=\"1348693601\"/>
        <string id=\"new_value\" value=\"萨连科\"/>
        </record><record>
        <integer id=\"database_table_type\" value=\"3\"/>
        <large id=\"db_unique_id\" value=\"7\"/>
        <unsigned id=\"property\" value=\"1348693601\"/>
        <string id=\"new_value\" value=\"不应载入\"/>
        </record></list></record>""",
        encoding="utf-8",
    )

    assert parse_player_name_xml(source) == {1301385: "萨连科"}


def test_parse_player_name_xml_parts_keeps_only_unambiguous_components(
    tmp_path: Path,
):
    source = tmp_path / "数据库人员汉化.xml"
    source.write_text(
        """<record><list id=\"db_changes\"><record>
        <integer id=\"database_table_type\" value=\"1\"/>
        <large id=\"db_unique_id\" value=\"1\"/>
        <unsigned id=\"property\" value=\"1348890209\"/>
        <string id=\"new_value\" value=\"特伦斯\"/>
        <string id=\"odvl\" value=\"Terrence\"/>
        </record><record>
        <integer id=\"database_table_type\" value=\"1\"/>
        <large id=\"db_unique_id\" value=\"2\"/>
        <unsigned id=\"property\" value=\"1349742177\"/>
        <string id=\"new_value\" value=\"博伊德\"/>
        <string id=\"odvl\" value=\"Boyd\"/>
        </record><record>
        <integer id=\"database_table_type\" value=\"1\"/>
        <large id=\"db_unique_id\" value=\"3\"/>
        <unsigned id=\"property\" value=\"1348890209\"/>
        <string id=\"new_value\" value=\"另译名\"/>
        <string id=\"odvl\" value=\"Terrence\"/>
        </record></list></record>""",
        encoding="utf-8",
    )

    parts = parse_player_name_xml_parts(source)
    assert parts["first"].get("terrence") is None
    assert parts["surname"] == {"boyd": "博伊德"}


def test_unambiguous_name_parts_are_combined_for_unknown_uid(tmp_path: Path):
    source = tmp_path / "数据库人员汉化.xml"
    source.write_text(
        """<record><list id=\"db_changes\"><record>
        <integer id=\"database_table_type\" value=\"1\"/>
        <large id=\"db_unique_id\" value=\"1\"/>
        <unsigned id=\"property\" value=\"1348890209\"/>
        <string id=\"new_value\" value=\"特伦斯\"/>
        <string id=\"odvl\" value=\"Terrence\"/>
        </record><record>
        <integer id=\"database_table_type\" value=\"1\"/>
        <large id=\"db_unique_id\" value=\"2\"/>
        <unsigned id=\"property\" value=\"1349742177\"/>
        <string id=\"new_value\" value=\"博伊德\"/>
        <string id=\"odvl\" value=\"Boyd\"/>
        </record></list></record>""",
        encoding="utf-8",
    )
    profile = {"players": [{"id": 999, "name": "Terrence Boyd"}]}

    with patch(
        "tools.player_name_localization._player_name_xml_paths",
        return_value=(source,),
    ):
        apply_installed_player_names(profile)

    assert profile["players"][0]["name"] == "特伦斯·博伊德"


def test_exact_name_guess_is_last_resort_and_preserves_game_name():
    profile = {"players": [{"id": 999, "name": "Deinner Ordóñez"}]}
    apply_installed_player_names(profile)
    assert profile["players"][0]["name"] == "戴内尔·奥尔多涅斯"
    assert profile["players"][0]["game_name"] == "Deinner Ordóñez"


def test_generic_phonetic_fallback_covers_unmatched_multisegment_names():
    profile = {
        "players": [
            {"id": 999, "name": "Qyzablor Nembux"},
            {"id": 1000, "name": "Xylophonic Qwertz"},
        ]
    }
    apply_installed_player_names(profile)

    for player in profile["players"]:
        assert player["name"]
        assert any("\u3400" <= char <= "\u9fff" for char in player["name"])
        assert player["game_name"]


def test_authoritative_uid_translation_wins_over_phonetic_fallback():
    profile = {"players": [{"id": 7, "name": "Mawulom Gota-Toudji"}]}
    apply_installed_player_names(profile, {7: "正式译名"})

    assert profile["players"][0]["name"] == "正式译名"
    assert profile["players"][0]["game_name"] == "Mawulom Gota-Toudji"


def test_japanese_name_uses_kanji_surname_first_without_middle_dot():
    profile = {
        "players": [{
            "id": 1001, "name": "Koki Saito",
            "nationality_id": 116, "nationality_code": "JPN",
        }]
    }
    parts = {
        "full": {}, "first": {"koki": "光毅"}, "surname": {"saito": "斋藤"},
    }
    with (
        patch(
            "tools.player_name_localization.load_installed_player_names",
            return_value={},
        ),
        patch(
            "tools.player_name_localization._load_bundled_player_name_parts",
            return_value=parts,
        ),
        patch(
            "tools.player_name_localization._player_name_xml_paths",
            return_value=(),
        ),
    ):
        apply_installed_player_names(profile)

    assert profile["players"][0]["name"] == "斋藤光毅"
    assert profile["players"][0]["game_name"] == "Koki Saito"


def test_unknown_japanese_name_is_not_phonetically_invented():
    profile = {
        "players": [{
            "id": 1002, "name": "Qyzablor Nembux",
            "nationality": "日本", "nationality_code": "JPN",
        }]
    }
    apply_installed_player_names(profile)

    assert profile["players"][0]["name"] == "Qyzablor Nembux"
    assert "game_name" not in profile["players"][0]


def test_installed_dictionary_merges_matching_generation_and_prefers_newer_db(
    tmp_path: Path,
):
    executable = tmp_path / "Football Manager 26" / "fm.exe"
    executable.parent.mkdir()
    executable.touch()
    database = executable.parent / "shared" / "data" / "database" / "db"
    old_lnc = database / "2600" / "lnc" / "人员汉化.lnc"
    new_lnc = database / "2620" / "lnc" / "all" / "players.lnc"
    unrelated = database / "2430" / "lnc" / "人员汉化.lnc"
    for path in (old_lnc, new_lnc, unrelated):
        path.parent.mkdir(parents=True)
    old_lnc.write_text(
        '"CHANGE_PLAYER_NAME" 7 "Player Seven" "旧译名" ""',
        encoding="utf-8",
    )
    new_lnc.write_text(
        '"CHANGE_PLAYER_NAME" 7 "Player Seven" "新译名" ""\n'
        '"CHANGE_PLAYER_NAME" 8 "Player Eight" "八号球员" ""',
        encoding="utf-8",
    )
    unrelated.write_text(
        '"CHANGE_PLAYER_NAME" 9 "FM24 Player" "不应载入" ""',
        encoding="utf-8",
    )

    clear_installed_player_name_cache()
    with patch("tools.player_name_localization._player_name_xml_paths", return_value=()):
        assert load_installed_player_names(str(executable), "fm26_steam") == {
            7: "新译名",
            8: "八号球员",
        }


def test_installed_name_is_hidden_display_data_and_manual_alias_stays_first():
    profile = {"players": [{"id": 7, "name": "Player Seven"}]}
    apply_installed_player_names(profile, {7: "七号球员"})

    assert profile["players"][0] == {
        "id": 7,
        "name": "七号球员",
        "game_name": "Player Seven",
    }

    with (
        patch(
            "tools.player_aliases.apply_installed_player_names",
            side_effect=lambda payload: apply_installed_player_names(
                payload, {7: "七号球员"},
            ),
        ),
        patch("tools.player_aliases._load", return_value={"7": "用户名称"}),
    ):
        apply_player_aliases(profile, "scope-a")

    assert profile["players"][0]["name"] == "用户名称"
    assert profile["players"][0]["game_name"] == "Player Seven"
    assert profile["players"][0]["custom_name"] is True


def test_player_name_localization_can_be_disabled_without_reformatting_game_name(tmp_path: Path):
    profile = {
        "players": [{
            "id": 7, "name": "Erling Haaland", "first_name": "Erling",
            "last_name": "Haaland", "common_name": "Haaland",
        }]
    }
    settings_path = tmp_path / "settings.json"
    with patch.object(app_settings, "SETTINGS_PATH", settings_path), patch(
        "tools.player_aliases.apply_installed_player_names",
        side_effect=AssertionError("dictionary should not be used when disabled"),
    ):
        app_settings.set_player_name_localization(False)
        apply_player_aliases(profile, "scope-a")
    assert profile["players"][0]["name"] == "Erling Haaland"
    assert profile["players"][0]["game_name"] == "Erling Haaland"


def test_player_name_localization_is_only_available_for_simplified_chinese(tmp_path: Path):
    settings_path = tmp_path / "settings.json"
    with patch.object(app_settings, "SETTINGS_PATH", settings_path):
        assert app_settings.load_settings()["player_name_localization"] is False
        assert app_settings.set_player_name_localization(True)["player_name_localization"] is False

        settings = app_settings.set_ui_locale("zh-CN")
        assert settings["player_name_localization"] is False
        assert app_settings.set_player_name_localization(True)["player_name_localization"] is True

        settings = app_settings.set_ui_locale("en-GB")
        assert settings["player_name_localization"] is False
        assert app_settings.load_settings()["player_name_localization"] is False


def test_legacy_chinese_settings_keep_player_name_localization_enabled(tmp_path: Path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text('{"odds_days": 7}', encoding="utf-8")
    with patch.object(app_settings, "SETTINGS_PATH", settings_path):
        settings = app_settings.load_settings()

    assert settings["ui_locale"] == "zh-CN"
    assert settings["player_name_localization"] is True


def test_player_name_localization_toggle_round_trip_restores_dictionary_name(tmp_path: Path):
    profile = {"players": [{"id": 7, "name": "Erling Haaland"}]}
    settings_path = tmp_path / "settings.json"
    with patch.object(app_settings, "SETTINGS_PATH", settings_path), patch(
        "tools.player_aliases.apply_installed_player_names",
        side_effect=lambda payload: apply_installed_player_names(payload, {7: "哈兰德"}),
    ):
        app_settings.set_ui_locale("zh-CN")
        app_settings.set_player_name_localization(False)
        apply_player_aliases(profile, "scope-a")
        assert profile["players"][0]["name"] == "Erling Haaland"
        app_settings.set_player_name_localization(True)
        apply_player_aliases(profile, "scope-a")
    assert profile["players"][0]["name"] == "哈兰德"


def test_disabling_localization_preserves_original_name_when_common_name_is_full(tmp_path: Path):
    profile = {"players": [{
        "id": 8, "name": "Cristiano Ronaldo", "first_name": "Cristiano",
        "last_name": "Ronaldo", "common_name": "Cristiano Ronaldo",
    }]}
    with patch.object(app_settings, "SETTINGS_PATH", tmp_path / "settings.json"):
        app_settings.set_player_name_localization(False)
        apply_player_aliases(profile, "scope-a")
    assert profile["players"][0]["name"] == "Cristiano Ronaldo"
