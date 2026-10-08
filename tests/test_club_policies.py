from __future__ import annotations

import struct
from types import SimpleNamespace
from unittest.mock import patch

from conftest import legacy_web_source

from tools import club_economy, club_policies, training_ground
from tools.club_policy_hooks import (
    DEPARTURE_PATTERN, FM24_DEPARTURE_PATTERN,
    FM24_INTERVIEW_MULTIPLIER_PATTERN, FM24_INTERVIEW_SCORE_PATTERN,
    FM24_SALARY_PATTERN,
    PROMISE_SIGNATURE, SALARY_CALL_PATTERN, ClubPolicyHookController,
    INTERVIEW_PATTERN, _departure_code, _fm24_departure_code,
    _fm24_interview_multiplier_code, _fm24_interview_score_code,
    _fm24_salary_code, _interview_code, _matches,
    _salary_budget_code, _salary_promise_code,
)
from tools.game_layout import (
    FM24_LAYOUT, FM24_EPIC_LAYOUT, FM26_LAYOUT, FM26_XGP_TEMPLATE,
)


def test_policy_activation_uses_game_days_and_is_bound_to_club_context():
    stored = {"policies": {}, "history": []}

    def save(payload):
        stored.clear()
        stored.update(payload)

    with (
        patch.object(club_policies, "_load", side_effect=lambda: {
            "policies": {key: dict(value) for key, value in stored["policies"].items()},
            "history": [dict(row) for row in stored["history"]],
        }),
        patch.object(club_policies, "_save", side_effect=save),
    ):
        activated = club_policies.set_club_policy(
            "departure_mediation", True, "2030-01-10",
            manager_id=7, team_id=42, club_id=9,
        )
        active = club_policies.public_club_policies(
            "2030-01-16", manager_id=7, team_id=42, club_id=9,
        )["departure_mediation"]
        expired = club_policies.public_club_policies(
            "2030-01-17", manager_id=7, team_id=42, club_id=9,
        )["departure_mediation"]
        changed_club = club_policies.public_club_policies(
            "2030-01-11", manager_id=7, team_id=99, club_id=10,
        )["departure_mediation"]

    assert activated["expires_on"] == "2030-01-17"
    assert active["enabled"] is True
    assert active["remaining_days"] == 1
    assert expired["enabled"] is False
    assert expired["inactive_reason"] == "授权已到期"
    assert changed_club["enabled"] is False
    assert "俱乐部" in changed_club["inactive_reason"]


def test_salary_authorization_lasts_fifteen_game_days_and_can_be_disabled():
    stored = {"policies": {}, "history": []}
    with (
        patch.object(club_policies, "_load", side_effect=lambda: stored),
        patch.object(club_policies, "_save", side_effect=lambda payload: stored.update(payload)),
    ):
        active = club_policies.set_club_policy(
            "salary_authorization", True, "2030-02-01",
            manager_id=5, team_id=6,
        )
        disabled = club_policies.set_club_policy(
            "salary_authorization", False, "2030-02-04",
            manager_id=5, team_id=6,
        )

    assert active["expires_on"] == "2030-02-16"
    assert active["remaining_days"] == 15
    assert disabled["enabled"] is False
    assert disabled["requested_enabled"] is False


def test_interview_perfume_lasts_fifteen_game_days():
    stored = {"policies": {}, "history": []}
    with (
        patch.object(club_policies, "_load", side_effect=lambda: stored),
        patch.object(club_policies, "_save", side_effect=lambda payload: stored.update(payload)),
    ):
        active = club_policies.set_club_policy(
            "interview_perfume", True, "2030-03-01",
            manager_id=7, team_id=8, club_id=9,
        )
        current = club_policies.public_club_policies(
            "2030-03-15", manager_id=7, team_id=8, club_id=9,
        )["interview_perfume"]
        expired = club_policies.public_club_policies(
            "2030-03-16", manager_id=7, team_id=8, club_id=9,
        )["interview_perfume"]
    assert active["expires_on"] == "2030-03-16"
    assert current["enabled"] is True
    assert expired["enabled"] is False


def test_policy_rooms_are_exposed_in_existing_facility_catalogs():
    departure = club_economy.ACTIVITY_FACILITIES["departure_mediation"]
    salary = club_economy.ACTIVITY_FACILITIES["salary_committee_room"]

    assert departure == {
        "name": "劝离室", "centre": "talk_room", "price": 1_000_000.0,
    }
    assert salary == {
        "name": "薪酬谈判室", "centre": "talk_room", "price": 0.0,
        "included_with_centre": True,
    }
    assert "salary_committee_room" not in training_ground.FACILITIES


def test_upstream_signatures_are_unique_in_synthetic_image_and_code_returns():
    departure = bytes(
        0x11 if value is None else value for value in DEPARTURE_PATTERN
    )
    salary = bytes(
        0x22 if value is None else value for value in SALARY_CALL_PATTERN
    )
    image = b"A" * 17 + departure + b"B" * 13 + salary + b"C" * 9

    assert _matches(image, DEPARTURE_PATTERN) == [17]
    assert _matches(image, SALARY_CALL_PATTERN) == [17 + len(departure) + 13]
    assert PROMISE_SIGNATURE == bytes.fromhex("44 0F B7 89 F8 01 00 00")
    interview = bytes(
        0x33 if value is None else value for value in INTERVIEW_PATTERN
    )
    assert _matches(interview, INTERVIEW_PATTERN) == [0]

    departure_code = _departure_code(
        0x10000000, departure[:6], 0x10000100, 0x20000000, 0x30000000,
    )
    budget_code = _salary_budget_code(
        0x11000000, b"\x55\x48\x89\xE5\x53", 0x11000100, 0x20000000,
    )
    promise_code = _salary_promise_code(
        0x12000000, PROMISE_SIGNATURE, 0x12000100,
    )
    interview_code = _interview_code(
        0x13000000, interview[5:11], 0x13000100, 0x20000000,
    )

    assert b"\xB8\xFF\xFF\xFF\x00" in departure_code
    assert b"\xB8\xFF\xFF\xFF\x00" in budget_code
    assert b"\x66\xC7\x81\xF8\x01\x00\x00\x14\x00" in promise_code
    assert b"\xB8\x40\x42\x0F\x00" in interview_code


def test_fm24_policy_signatures_and_code_preserve_separate_generation_contracts():
    departure = bytes(
        0x33 if value is None else value for value in FM24_DEPARTURE_PATTERN
    )
    salary = bytes(
        0x44 if value is None else value for value in FM24_SALARY_PATTERN
    )
    interview_score = bytes(FM24_INTERVIEW_SCORE_PATTERN)
    interview_multiplier = bytes(
        0x55 if value is None else value
        for value in FM24_INTERVIEW_MULTIPLIER_PATTERN
    )
    image = (
        b"A" * 9 + departure + b"B" * 11 + salary + b"C" * 7
        + interview_score + b"D" * 5 + interview_multiplier
    )

    assert _matches(image, FM24_DEPARTURE_PATTERN) == [9]
    assert _matches(image, FM24_SALARY_PATTERN) == [9 + len(departure) + 11]
    assert _matches(image, FM24_INTERVIEW_SCORE_PATTERN) == [
        9 + len(departure) + 11 + len(salary) + 7
    ]
    assert _matches(image, FM24_INTERVIEW_MULTIPLIER_PATTERN) == [
        9 + len(departure) + 11 + len(salary) + 7
        + len(interview_score) + 5
    ]

    departure_code = _fm24_departure_code(
        0x14000000, departure[:6], 0x14000100, 0x20000000, 0x30000000,
    )
    salary_code = _fm24_salary_code(
        0x15000000, salary[7:13], 0x15000100, 0x40000000,
    )
    interview_score_code = _fm24_interview_score_code(
        0x16000000, interview_score[:6], 0x16000100, 0x50000000,
    )
    interview_multiplier_code = _fm24_interview_multiplier_code(
        0x17000000, interview_multiplier[7:12], 0x17000100,
    )

    assert b"\xB8\xFF\xFF\xFF\x7F" in departure_code
    assert b"\xB9\xFF\xFF\xFF\x00" in salary_code
    assert struct.pack("<Q", 0x20000000) in departure_code
    assert struct.pack("<Q", 0x30000000) in departure_code
    assert struct.pack("<Q", 0x40000000) in salary_code
    assert b"\xBB\x40\x42\x0F\x00" in interview_score_code
    assert struct.pack("<Q", 0x50000000) in interview_score_code
    assert struct.pack("<d", 1_000_000.0) in interview_multiplier_code


def test_interview_installer_selects_generation_specific_hook_set():
    controller = ClubPolicyHookController()
    module = SimpleNamespace(base_address=0x10000000)

    fm24_score = bytes(FM24_INTERVIEW_SCORE_PATTERN)
    fm24_multiplier = bytes(
        0x66 if value is None else value
        for value in FM24_INTERVIEW_MULTIPLIER_PATTERN
    )
    fm24_image = fm24_score + b"X" * 13 + fm24_multiplier
    with patch.object(controller, "_install") as install:
        controller._enable_interview(
            None, module, fm24_image, SimpleNamespace(key="fm24"),
            manager_address=0x20000000,
        )
    assert [call.args[1] for call in install.call_args_list] == [
        "interview_perfume", "interview_perfume_multiplier",
    ]

    fm26_image = bytes(0x77 if value is None else value for value in INTERVIEW_PATTERN)
    with patch.object(controller, "_install") as install:
        controller._enable_interview(
            None, module, fm26_image, SimpleNamespace(key="fm26"),
            manager_address=0x20000000,
        )
    install.assert_called_once()
    assert install.call_args.args[1] == "interview_perfume"


def test_fm24_interview_requires_both_hook_components_to_be_active():
    controller = ClubPolicyHookController()
    controller.layout_key = "fm24"
    controller.installed = {"interview_perfume": {}}
    assert controller.status()["interview_active"] is False

    controller.installed["interview_perfume_multiplier"] = {}
    assert controller.status()["interview_active"] is True


def test_fm24_interview_rolls_back_first_hook_when_second_install_fails():
    controller = ClubPolicyHookController()
    module = SimpleNamespace(base_address=0x10000000)
    score = bytes(FM24_INTERVIEW_SCORE_PATTERN)
    multiplier = bytes(
        0x66 if value is None else value
        for value in FM24_INTERVIEW_MULTIPLIER_PATTERN
    )
    image = score + b"X" * 13 + multiplier

    def install(_process, key, *_args):
        if key == "interview_perfume_multiplier":
            raise RuntimeError("synthetic second-hook failure")
        controller.installed[key] = {}

    def disable(key):
        controller.installed.pop(key, None)

    with (
        patch.object(controller, "_install", side_effect=install),
        patch.object(controller, "_disable", side_effect=disable) as rollback,
    ):
        try:
            controller._enable_interview(
                None, module, image, SimpleNamespace(key="fm24"),
                manager_address=0x20000000,
            )
        except RuntimeError as error:
            assert str(error) == "synthetic second-hook failure"
        else:
            raise AssertionError("second Hook failure was not propagated")

    rollback.assert_called_once_with("interview_perfume")
    assert not controller.installed


def test_departure_installer_selects_a_machine_code_builder_for_each_generation():
    controller = ClubPolicyHookController()
    module = SimpleNamespace(base_address=0x10000000)

    for layout_key, pattern in (
        ("fm24", FM24_DEPARTURE_PATTERN),
        ("fm26", DEPARTURE_PATTERN),
    ):
        image = bytes(0x55 if value is None else value for value in pattern)
        with patch.object(controller, "_install") as install:
            controller._enable_departure(
                None, module, image, SimpleNamespace(key=layout_key),
                manager_address=0x20000000, team_address=0x30000000,
                club_address=0x40000000,
            )
        builder = install.call_args.args[4]
        code = builder(0x11000000, image[:6], 0x11000100)
        assert isinstance(code, bytes)
        assert code


def test_salary_policy_requires_generation_specific_hook_set():
    controller = ClubPolicyHookController()
    controller.layout_key = "fm24"
    controller.installed = {"salary_budget": {}}
    assert controller.status()["salary_active"] is True

    controller.layout_key = "fm26"
    assert controller.status()["salary_active"] is False
    controller.installed["salary_promises"] = {}
    assert controller.status()["salary_active"] is True


def test_first_fm24_salary_sync_does_not_keep_requesting_fm26_promise_hook():
    controller = ClubPolicyHookController()

    def enable_fm24(_manager_address):
        controller.layout_key = "fm24"
        controller.installed["salary_budget"] = {}

    with patch.object(controller, "_enable_salary", side_effect=enable_fm24) as enable:
        first = controller.sync(
            departure_active=False, salary_active=True,
            manager_address=0x1000, team_address=0x2000,
        )
        second = controller.sync(
            departure_active=False, salary_active=True,
            manager_address=0x1000, team_address=0x2000,
        )

    assert first["salary_active"] is True
    assert second["salary_active"] is True
    enable.assert_called_once_with(0x1000)


def test_verified_steam_policy_rvas_are_version_specific_and_other_builds_scan():
    assert FM24_LAYOUT.club_policy_departure_pattern_rva == 0x32668D3
    assert FM24_LAYOUT.club_policy_salary_pattern_rva == 0x3262089
    assert FM26_LAYOUT.club_policy_departure_pattern_rva == 0x168915B
    assert FM26_LAYOUT.club_policy_salary_pattern_rva == 0x219B599
    assert FM26_LAYOUT.club_policy_salary_promise_pattern_rva == 0x2DBB791
    assert FM24_EPIC_LAYOUT.club_policy_departure_pattern_rva is None
    assert FM24_EPIC_LAYOUT.club_policy_salary_pattern_rva is None
    assert FM26_XGP_TEMPLATE.club_policy_departure_pattern_rva is None
    assert FM26_XGP_TEMPLATE.club_policy_salary_pattern_rva is None
    assert FM26_XGP_TEMPLATE.club_policy_salary_promise_pattern_rva is None


def test_verified_policy_rva_reads_only_the_target_signature():
    controller = ClubPolicyHookController()
    process = object()
    module = SimpleNamespace(name="fm.exe", base_address=0x10000000, size=0x40000000)
    signature = bytes(
        0x44 if value is None else value for value in FM24_SALARY_PATTERN
    )
    with (
        patch("tools.club_policy_hooks.read_process_memory", return_value=signature) as read,
        patch("tools.club_policy_hooks.iter_readable_regions") as regions,
    ):
        offsets = controller._pattern_offsets(
            process, module, None, {"salary": FM24_SALARY_PATTERN},
            {"salary": 0x3262089},
        )

    assert offsets == {"salary": [0x3262089]}
    read.assert_called_once_with(
        process, module.base_address + 0x3262089, len(FM24_SALARY_PATTERN),
    )
    regions.assert_not_called()


def test_activity_centre_exposes_salary_policy_without_departure_entry():
    script = legacy_web_source("web/app.js")
    app_script = open("web/app.js", encoding="utf-8").read()
    server = open("fm_odds_web.py", encoding="utf-8").read()
    catalogue = open("web/i18n.facilities.js", encoding="utf-8").read()
    salary_effect = (
        "会议结束后，获得持续 15 个游戏日的特别授权："
        "任何球员都不会拒绝薪资谈判时的任何要求。"
    )

    assert "data-departure-mediation" not in app_script
    assert "data-salary-authorization" in script
    assert "/api/activity/departure-mediation" in server
    assert 'centre:"talk_room"' in script
    assert 'request("/api/activity/salary-authorization"' in script
    assert "/api/activity/salary-authorization" in server
    assert '"activity.hold_meeting"' in script
    assert 'uiText("activity.salary_description")' in app_script
    assert 'uiText("activity.confirm_salary")' in app_script
    assert salary_effect in catalogue
    assert f"召开薪酬谈判会议？\\n\\n{salary_effect}" in catalogue
    assert "薪酬委员会会议室" not in script
    assert "会议授权仅绑定当前经理与俱乐部，不随经理换队转移。" not in script
    assert "期限按游戏日期计算；切换经理、俱乐部、存档或版本时会立即卸载原生效果。" not in script
    assert "授权将在 15 个游戏日内开放工资预算转入和续约限制。" not in script
    assert '"activity.club_players"' in script
    assert "离队调解室" not in script
    assert "会议纪要仅绑定当前经理与俱乐部，不随经理换队转移。" not in script
    assert "谈话结束后自动生效 7 个游戏日；关闭、到期、换队或换档会卸载原生效果。" not in script
    assert "谈话效果持续 7 个游戏日，本队球员转出时不回绝。" not in script
