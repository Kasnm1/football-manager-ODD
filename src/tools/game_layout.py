from __future__ import annotations

from dataclasses import dataclass, replace
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import mmap
from pathlib import Path
import struct
from typing import Any


FM26_EXE_SHA256 = "3653C97F9CCEC2BE28EDC4FAAE67304B5B6C26733F2F07DEA3E7C591D3B9FF73"
# The Steam EXE hash is shared across minor FM26 updates. Keep the loaded
# plugin identity separate so an older game_plugin.dll cannot be paired with
# the 26.3.2 layout (notably its different game-date RVA).
FM26_STEAM_PLUGIN_SHA256 = "EB6C86FAB56E051FE41482C7B93D8CB0AF716A1CC2C6197CE1BABBFF68372BB8"
FM26_GAME_DATE_PATTERN = (
    0x0F, 0xB7, 0x03, 0x25, 0xFF, 0x01, 0x00, 0x00,
    0x66, 0x83, 0xF8, 0x01, 0x75, 0x08, 0x8B, 0x05,
    None, None, None, None, 0x89, 0x03,
)
FM24_EXE_SHA256 = "E1059EEE82FA7832188831521A3FA633EC3260DD98D03DA48B16661147E3AB48"
FM24_240_EXE_SHA256 = "1473E5C3A778CB255A30C7A43D8346E27B031B47C42E93533097E049C110D58E"
FM24_241_EXE_SHA256 = "ED5D0F9C3D06194FA5D19E5A2C14EF8F9C6AB8A9FF7E77220594E8A600A75F5F"
FM24_EPIC_EXE_SHA256 = "33E8A2E48C4986F2A15E11C4C93ACA1199029D66B19BD765403EB22DEA265378"
FM24_XGP_PE_TIMESTAMP = 0x67FF9A0A
FM24_XGP_IMAGE_SIZE = 0x1CD9F000
FM26_XGP_PE_TIMESTAMP = 0x6A229DA9
FM26_XGP_IMAGE_SIZE = 0x1EDF4000


@dataclass(frozen=True)
class GameLayout:
    key: str
    display_name: str
    executable_sha256: str
    module_name: str
    distribution: str
    fixture_vtable_rva: int
    team_vtable_rva: int
    national_team_vtable_rva: int
    nation_vtable_rva: int
    club_vtable_rva: int
    competition_vtable_rva: int
    actual_player_vtable_rvas: tuple[int, ...]
    player_and_non_player_vtable_rvas: tuple[int, ...]
    player_person_offset: int
    player_and_non_player_person_offset: int
    player_positions_offset: int
    player_attributes_offset: int
    player_ca_offset: int
    player_pa_offset: int
    player_ca_bytes: int
    attribute_display_bias: int
    manager_person_offset: int
    human_manager_vtable_rvas: tuple[int, ...]
    human_manager_vtable_on_person: bool = False
    # A national Team points at a generation-specific Nation container.  On
    # verified builds this field links that container to the canonical nation
    # object whose stable UID is used by nation_mappings.
    national_team_nation_offset: int | None = None
    nationality_vtable_rva: int | None = None
    # Generation-specific, read-only database directory probes.  The patterns
    # are resolved inside the selected module and the RIP-relative target is a
    # table of pointers to FM's native database containers.  AOBs remain
    # dynamic across distributions; no session address is persisted.
    database_root_pattern: tuple[int | None, ...] = ()
    database_root_probe_rva: int | None = None
    database_root_rel32_offset: int | None = None
    database_root_instruction_size: int | None = None
    human_manager_root_pattern: tuple[int | None, ...] = ()
    human_manager_root_probe_rva: int | None = None
    human_manager_root_rel32_offset: int | None = None
    human_manager_root_instruction_size: int | None = None
    savegame_root_rvas: tuple[int, ...] = ()
    savegame_id_absolute_address: int | None = None
    savegame_id_requires_legacy_validation: bool = False
    match_engine_phase_rva: int | None = None
    match_engine_mode_rva: int | None = None
    match_engine_active_state_rva: int | None = None
    play_fixture_manager_pointer_rva: int | None = None
    play_fixture_manager_vtable_rva: int | None = None
    match_playback_module_name: str | None = None
    match_playback_module_timestamp: int | None = None
    match_playback_module_image_size: int | None = None
    match_setup_type_info_rva: int | None = None
    redbull_hook_rva: int | None = None
    referee_hook_rva: int | None = None
    ca_growth_hook_rva: int | None = None
    club_policy_departure_pattern_rva: int | None = None
    club_policy_salary_pattern_rva: int | None = None
    club_policy_salary_promise_pattern_rva: int | None = None
    youth_generation_gate_hook_rva: int | None = None
    youth_generation_pa_hook_rva: int | None = None
    youth_generation_state_hook_rva: int | None = None
    youth_son_hook_rva: int | None = None
    result_home_goals_offset: int = 0x64
    result_away_goals_offset: int = 0x69
    result_home_outcome_offset: int = 0x6E
    result_away_outcome_offset: int = 0x6F
    result_event_root_offset: int = 0x78
    result_event_record_size: int = 0x10
    result_event_record_format: str = "fm26"
    fixture_stage_index_offset: int | None = None
    fixture_group_index_offset: int | None = None
    fixture_round_number_offset: int | None = None
    competition_actual_offset: int | None = None
    actual_competition_stages_offset: int | None = None
    competition_stage_type_offset: int | None = None
    competition_stage_index_offset: int | None = None
    cup_stage_teams_offset: int | None = None
    cup_stage_round_ties_offset: int | None = None
    fixture_result_vtable_rva: int | None = None
    fixture_pool_rva: int | None = None
    season_result_vtable_rva: int | None = None
    game_date_rva: int | None = None
    match_session_pointer_rva: int | None = None
    match_session_vtable_rva: int | None = None
    game_match_session_vtable_rva: int | None = None
    player_height_offset: int | None = None
    player_weight_offset: int | None = None
    player_sharpness_offset: int | None = None
    player_fatigue_offset: int | None = None
    player_fitness_offset: int | None = None
    player_morale_offset: int | None = None
    player_injury_list_offset: int | None = None
    player_home_reputation_offset: int | None = None
    player_current_reputation_offset: int | None = None
    player_world_reputation_offset: int | None = None
    # Build-gated current-season aggregate stored on db::PLAYER.  These
    # offsets are intentionally disabled unless the exact distribution/build
    # has been verified against the in-game statistics screen.
    player_season_stats_root_offset: int | None = None
    player_season_stats_total_slot_offset: int | None = None
    player_season_stats_block_size: int | None = None
    player_career_stats_offset: int | None = None
    # Native international appearance/goal counters.  These are separate
    # from club season/career containers and remain disabled unless the
    # exact build profile has a verified mapping.
    player_international_appearances_offset: int | None = None
    player_international_goals_offset: int | None = None
    player_u21_international_appearances_offset: int | None = None
    player_u21_international_goals_offset: int | None = None
    player_international_counter_width: int = 2
    player_u21_international_counter_width: int = 1
    team_reputation_offset: int | None = None
    # Native db::TEAM squad classification (first team, U21, U18, etc.).
    # Keep disabled for builds where the field has not been verified.
    team_type_offset: int | None = None
    competition_reputation_offset: int | None = None
    competition_reputation_bytes: int | None = None
    person_nationality_offset: int | None = None
    person_date_of_birth_offset: int | None = None
    person_date_of_birth_day_year: bool = False
    person_hidden_attributes_offset: int | None = None
    person_relationships_offset: int | None = None
    person_languages_offset: int | None = None
    # Native Person.PreviousClub pointer. Keep build-gated because this is a
    # Person field and is unrelated to db::PLAYER's +0x108 season-stat root.
    person_previous_club_offset: int | None = None
    staff_language_write_verified: bool = False
    # FM26 PersonFlags contains the native Female bit at this verified field.
    # FM24 remains None until an equivalent build-specific mapping is verified.
    person_flags_offset: int | None = None
    person_flags_bytes: int = 8
    person_full_name_offset: int = 0x40
    person_first_name_offset: int = 0x50
    person_last_name_offset: int = 0x58
    person_common_name_offset: int = 0x60
    staff_person_vtable_rva: int | None = None
    staff_complete_object_offset: int = 0x100
    # The coaching-ability block may use a different complete-object base.
    # FM24 stores it at person-0xF0 while chairman/controller fields remain
    # based at person-0x100. FM26 uses person-0x100 for both.
    staff_ability_complete_object_offset: int | None = None
    team_manager_person_offset: int = 0x100
    staff_domestic_reputation_offset: int | None = None
    staff_current_reputation_offset: int | None = None
    staff_world_reputation_offset: int | None = None
    staff_ca_offset: int | None = None
    staff_pa_offset: int | None = None
    staff_coaching_license_offset: int | None = None
    staff_job_type_offset: int | None = None
    staff_contract_type_offset: int | None = None
    staff_wage_offset: int | None = None
    staff_contract_start_offset: int | None = None
    staff_contract_expiry_offset: int | None = None
    staff_abilities_verified: bool = False
    loan_contract_vtable_rva: int | None = None
    player_move_contract_pool_rva: int | None = None
    player_move_loan_contract_pool_rva: int | None = None
    player_move_loan_contract_constructor_rva: int | None = None
    player_move_contract_allocate_rva: int | None = None
    player_move_contract_release_rva: int | None = None
    player_move_contract_factory_global_rva: int | None = None
    player_move_contract_factory_target_rva: int | None = None
    player_move_main_contract_vtable_rva: int | None = None
    player_move_terminate_contract_rva: int | None = None
    player_move_prepare_loan_rva: int | None = None
    player_move_club_method_rva: int | None = None
    player_move_fm24_source_cleanup_rva: int | None = None
    player_move_fm24_transfer_rva: int | None = None
    player_move_fm24_transaction_allocator_rva: int | None = None
    player_move_fm24_transaction_constructor_rva: int | None = None
    player_move_fm24_transaction_initializer_rva: int | None = None
    player_move_fm24_transaction_prepare_rva: int | None = None
    player_move_fm24_transaction_commit_rva: int | None = None
    player_move_fm24_transaction_transition_rva: int | None = None
    player_move_fm24_transaction_outer_submit_rva: int | None = None
    player_move_fm24_transaction_entry_rva: int | None = None
    player_move_fm24_transaction_vtable_rva: int | None = None
    player_move_fm24_context_root_rva: int | None = None
    player_move_fm24_context_vtable_rva: int | None = None
    player_move_fm24_contract_factory_rva: int | None = None
    player_move_fm24_loan_constructor_rva: int | None = None
    future_transfer_manager_vtable_rva: int | None = None
    future_transfer_full_offer_vtable_rva: int | None = None
    future_transfer_loan_offer_vtable_rva: int | None = None
    # Club detail fields recovered from FMRTE 24.4.2/26.3.2 metadata.
    # These are deliberately explicit per layout so unsupported distributions
    # can leave the profile disabled instead of inheriting an unverified map.
    club_detail_offset: int | None = None
    club_detail2_offset: int | None = None
    club_training_ground_offset: int | None = None
    club_year_founded_offset: int | None = None
    club_training_facilities_offset: int | None = None
    club_youth_facilities_offset: int | None = None
    club_junior_coaching_offset: int | None = None
    club_youth_recruitment_offset: int | None = None
    club_average_attendance_offset: int | None = None
    club_minimum_attendance_offset: int | None = None
    club_maximum_attendance_offset: int | None = None
    club_season_ticket_holders_offset: int | None = None
    club_social_media_followers_offset: int | None = None
    club_supporters_profile_offset: int | None = None
    club_supporters_distribution_offset: int | None = None
    club_chairman_status_offset: int | None = None
    club_status_offset: int | None = None
    club_morale_offset: int | None = None
    club_ownership_offset: int | None = None
    club_finance_offset: int | None = None
    finance_balance_offset: int | None = None
    finance_sugar_daddy_offset: int | None = None
    finance_remaining_transfer_budget_offset: int | None = None
    finance_season_transfer_budget_offset: int | None = None
    finance_wage_budget_offset: int | None = None
    finance_wage_used_offset: int | None = None
    finance_max_wage_offset: int | None = None
    finance_average_ticket_price_offset: int | None = None
    finance_transfer_revenue_percentage_offset: int | None = None
    finance_income_statement_period_offsets: tuple[int, int, int, int] | None = None
    finance_monthly_summary_offset: int | None = None
    club_loans_offset: int | None = None
    club_debt_record_size: int | None = None
    club_sponsors_root_offset: int | None = None
    club_sponsor_record_size: int | None = None
    team_details_offset: int | None = None
    team_details_club_vision_offset: int | None = None
    club_vision_culture_offset: int | None = None
    club_culture_record_size: int | None = None
    club_culture_vtable_rva: int | None = None
    club_culture_constructor_rva: int | None = None
    club_culture_initialize_rva: int | None = None
    # Club affiliation evidence recovered from FMRTE 24.4.2 build 47 and
    # 26.3.2 build 40.  The club owns a vector of pointers at +0x118; each
    # pointer targets a 0x38-byte affiliation record shared by both clubs.
    club_affiliations_offset: int | None = None
    club_affiliation_record_size: int | None = None
    # Native affiliation creation is build-specific.  The manager owns the
    # serializer-visible global registry while each Club owns a local vector.
    club_affiliation_manager_rva: int | None = None
    club_affiliation_create_rva: int | None = None
    club_affiliation_global_vector_offset: int | None = None
    club_affiliation_create_pattern: tuple[int | None, ...] = ()
    club_affiliation_manager_call_pattern: tuple[int | None, ...] = ()
    club_affiliation_manager_rel32_offset: int | None = None
    club_affiliation_manager_instruction_size: int | None = None
    club_affiliation_create_call_rel32_offset: int | None = None
    club_affiliation_create_call_instruction_size: int | None = None
    club_nation_offset: int | None = None
    nation_men_container_offset: int | None = None
    nation_youth_rating_offset: int | None = None
    club_director_of_football_offset: int | None = None
    club_foreground_color_offset: int | None = None
    club_background_color_offset: int | None = None
    club_allow_custom_logo_offset: int | None = None
    stadium_vtable_rva: int | None = None
    stadium_capacity_offset: int | None = None
    stadium_seating_capacity_offset: int | None = None
    stadium_used_capacity_offset: int | None = None
    stadium_expansion_capacity_offset: int | None = None
    stadium_pitch_condition_offset: int | None = None
    stadium_pitch_type_offset: int | None = None
    stadium_name_offset: int = 0x40
    stadium_item_id_offset: int | None = None
    stadium_id_offset: int | None = None
    stadium_id2_offset: int | None = None
    stadium_location_offset: int | None = None
    stadium_build_date_offset: int | None = None
    stadium_rebuild_date_offset: int | None = None
    stadium_all_seater_capacity_offset: int | None = None
    stadium_nearby_offset: int | None = None
    stadium_owner_offset: int | None = None
    stadium_latitude_offset: int | None = None
    stadium_longitude_offset: int | None = None
    stadium_state_offset: int | None = None
    stadium_extinct_offset: int | None = None
    stadium_pitch_deterioration_offset: int | None = None
    stadium_pitch_recovery_offset: int | None = None
    stadium_extras_offset: int | None = None
    stadium_national_team_use_offset: int | None = None
    stadium_national_u21_use_offset: int | None = None
    stadium_national_u19_use_offset: int | None = None
    stadium_pitch_relaid_date_offset: int | None = None
    stadium_pitch_relay_required_date_offset: int | None = None
    stadium_seat_color_offset: int | None = None
    chairman_business_offset: int | None = None
    chairman_interference_offset: int | None = None
    chairman_patience_offset: int | None = None
    chairman_resources_offset: int | None = None
    chairman_base_adjustment: int = 0
    game_version: str | None = None

    def module(self, process: Any) -> Any:
        # A connected read session already resolved and verified this module
        # for the same PID/layout generation. Reusing that immutable snapshot
        # avoids another Toolhelp module enumeration for every legacy writer.
        from tools.game_session import active_game_module

        active = active_game_module(process, self)
        if active is not None:
            return active
        from fm_collector.win32 import find_module

        return find_module(process, self.module_name)


FM26_LAYOUT = GameLayout(
    key="fm26",
    display_name="Football Manager 26",
    game_version="26.3.2",
    executable_sha256=FM26_EXE_SHA256,
    module_name="game_plugin.dll",
    distribution="steam",
    nation_men_container_offset=0x108,
    nation_youth_rating_offset=0x864,
    fixture_vtable_rva=0x449EF98,
    fixture_result_vtable_rva=0x4332188,
    fixture_pool_rva=0x4E370F0,
    season_result_vtable_rva=0x4330778,
    team_vtable_rva=0x44C13A8,
    national_team_vtable_rva=0x4592528,
    nation_vtable_rva=0x44CDD68,
    national_team_nation_offset=0x170,
    nationality_vtable_rva=0x44BF9B8,
    club_vtable_rva=0x44BA518,
    competition_vtable_rva=0x44BFE68,
    game_date_rva=0x4DF3C18,
    manager_person_offset=0x450,
    human_manager_vtable_rvas=(0x44A51CC,),
    database_root_pattern=(
        0x48, 0x83, 0xC7, 0x28, 0x4C, 0x8B, 0x0D,
        None, None, None, None,
    ),
    database_root_probe_rva=0x2C033AB,
    database_root_rel32_offset=7,
    database_root_instruction_size=11,
    human_manager_root_pattern=(
        0x48, 0x8B, 0x0D, None, None, None, None,
        0x48, 0x8B, 0x41, 0x18, 0x48, 0x8B, 0x49, 0x20,
        0x48, 0x29, 0xC1, 0x48, 0xC1, 0xF9, 0x03, 0x48, 0x39, 0xCE,
    ),
    human_manager_root_probe_rva=0xD0BFE3,
    human_manager_root_rel32_offset=3,
    human_manager_root_instruction_size=7,
    club_policy_departure_pattern_rva=0x168915B,
    club_policy_salary_pattern_rva=0x219B599,
    club_policy_salary_promise_pattern_rva=0x2DBB791,
    fixture_stage_index_offset=0x38,
    fixture_group_index_offset=0x39,
    fixture_round_number_offset=0x3B,
    competition_actual_offset=0xB0,
    actual_competition_stages_offset=0x1C0,
    competition_stage_type_offset=0x48,
    competition_stage_index_offset=0x4E,
    cup_stage_teams_offset=0x98,
    cup_stage_round_ties_offset=0xA0,
    savegame_root_rvas=(0x04E35F60, 0x04E44950),
    match_engine_phase_rva=0x4E46428,
    match_engine_mode_rva=0x4E47B0C,
    match_engine_active_state_rva=0x4E47B08,
    play_fixture_manager_pointer_rva=0x4E35E28,
    play_fixture_manager_vtable_rva=0x4480808,
    match_playback_module_name="GameAssembly.dll",
    match_playback_module_timestamp=0x6A229ED8,
    match_playback_module_image_size=0x35D9000,
    match_setup_type_info_rva=0x2E4E9F8,
    ca_growth_hook_rva=0x3174E3C,
    youth_generation_gate_hook_rva=0xEE5F2E,
    youth_generation_pa_hook_rva=0x1711767,
    youth_son_hook_rva=0x2D05180,
    actual_player_vtable_rvas=(0x4509828,),
    player_and_non_player_vtable_rvas=(0x4785308,),
    player_person_offset=0x288,
    player_and_non_player_person_offset=0x380,
    player_positions_offset=0x150,
    player_attributes_offset=0x15F,
    player_ca_offset=0x264,
    player_pa_offset=0x266,
    player_ca_bytes=2,
    attribute_display_bias=0,
    player_height_offset=0x22E,
    player_sharpness_offset=0x258,
    player_fatigue_offset=0x25A,
    player_fitness_offset=0x25C,
    player_morale_offset=0x26C,
    player_injury_list_offset=0xF8,
    player_season_stats_root_offset=0x108,
    player_season_stats_total_slot_offset=0x48,
    player_season_stats_block_size=0x78,
    player_career_stats_offset=0x110,
    player_international_appearances_offset=0x3BC,
    player_international_goals_offset=0x3BE,
    player_u21_international_appearances_offset=0x3C0,
    player_u21_international_goals_offset=0x3C1,
    player_international_counter_width=2,
    player_u21_international_counter_width=1,
    player_world_reputation_offset=0x262,
    team_reputation_offset=0xA8,
    team_type_offset=0x28,
    competition_reputation_offset=0x188,
    competition_reputation_bytes=1,
    person_nationality_offset=0x68,
    person_languages_offset=0xF0,
    person_flags_offset=0x18,
    person_date_of_birth_offset=0x88,
    person_hidden_attributes_offset=0x70,
    person_relationships_offset=0x78,
    person_previous_club_offset=0x108,
    staff_person_vtable_rva=0x4565018,
    staff_domestic_reputation_offset=None,
    staff_current_reputation_offset=None,
    staff_world_reputation_offset=None,
    staff_ca_offset=None,
    staff_pa_offset=None,
    staff_coaching_license_offset=0x13C,
    staff_job_type_offset=0x26,
    staff_contract_type_offset=0xC3,
    staff_wage_offset=0x20,
    staff_contract_start_offset=0x44,
    staff_contract_expiry_offset=0x48,
    staff_abilities_verified=True,
    staff_ability_complete_object_offset=0x100,
    loan_contract_vtable_rva=0x45A1268,
    player_move_contract_pool_rva=0x4E373A0,
    player_move_loan_contract_pool_rva=0x4E45C98,
    player_move_loan_contract_constructor_rva=0x1AA7700,
    player_move_contract_allocate_rva=0xDC1CD0,
    player_move_contract_release_rva=0xDC1E30,
    player_move_contract_factory_global_rva=0x4E48470,
    player_move_contract_factory_target_rva=0x346CC50,
    player_move_main_contract_vtable_rva=0x4334DF8,
    player_move_terminate_contract_rva=0x9E83C0,
    player_move_prepare_loan_rva=0x140CAE0,
    player_move_club_method_rva=0xEEF950,
    club_detail_offset=0xB0,
    club_detail2_offset=0x100,
    club_training_ground_offset=0x140,
    club_year_founded_offset=0xD0,
    club_training_facilities_offset=0x118,
    club_youth_facilities_offset=0x123,
    club_junior_coaching_offset=0x124,
    club_youth_recruitment_offset=0x125,
    club_average_attendance_offset=0x68,
    club_minimum_attendance_offset=0x6C,
    club_maximum_attendance_offset=0x70,
    club_season_ticket_holders_offset=0xB8,
    club_social_media_followers_offset=0xF0,
    club_supporters_profile_offset=0x10C,
    club_supporters_distribution_offset=0xFE,
    club_chairman_status_offset=0xD6,
    club_status_offset=0x16A,
    club_morale_offset=0x119,
    club_ownership_offset=0x70,
    club_finance_offset=0x150,
    finance_balance_offset=0x14,
    # FMRTE 26.3.2 build 40 runtime Aft.Aib resolves to +0x3C.
    finance_sugar_daddy_offset=0x3C,
    finance_remaining_transfer_budget_offset=0x7CC,
    finance_season_transfer_budget_offset=0x7D0,
    finance_wage_budget_offset=0x810,
    finance_wage_used_offset=0x81C,
    finance_max_wage_offset=0x814,
    finance_average_ticket_price_offset=0x870,
    finance_transfer_revenue_percentage_offset=0x7D8,
    finance_income_statement_period_offsets=(0x280, 0x398, 0x4B0, 0x5D0),
    finance_monthly_summary_offset=0x40,
    club_loans_offset=0x48,
    club_debt_record_size=0x1C,
    team_details_offset=0xA0,
    team_details_club_vision_offset=0xC8,
    club_vision_culture_offset=0x78,
    club_culture_record_size=0x40,
    club_culture_vtable_rva=0x4342788,
    club_culture_constructor_rva=0x193D240,
    club_affiliations_offset=0x118,
    club_affiliation_record_size=0x38,
    club_affiliation_manager_rva=0x4E49578,
    club_affiliation_create_rva=0xBA2570,
    club_affiliation_global_vector_offset=0x808,
    club_nation_offset=0xD8,
    club_director_of_football_offset=0x148,
    club_foreground_color_offset=0xA0,
    club_background_color_offset=0xA8,
    club_allow_custom_logo_offset=0x162,
    stadium_vtable_rva=0x45C03F8,
    stadium_capacity_offset=0x7C,
    stadium_seating_capacity_offset=0x80,
    stadium_used_capacity_offset=0x84,
    stadium_expansion_capacity_offset=0x8C,
    stadium_pitch_condition_offset=0xA8,
    stadium_pitch_type_offset=0xAD,
    stadium_name_offset=0x40,
    stadium_item_id_offset=0x08,
    stadium_id_offset=0x0C,
    stadium_id2_offset=0x10,
    stadium_location_offset=0x60,
    stadium_build_date_offset=0x30,
    stadium_rebuild_date_offset=0x34,
    stadium_all_seater_capacity_offset=0x88,
    stadium_nearby_offset=0x68,
    stadium_owner_offset=0x90,
    stadium_latitude_offset=0x70,
    stadium_longitude_offset=0x74,
    stadium_state_offset=0xAA,
    stadium_extinct_offset=0xAB,
    stadium_pitch_deterioration_offset=0xAC,
    stadium_pitch_recovery_offset=0xAE,
    stadium_extras_offset=0xB6,
    stadium_national_team_use_offset=0xB1,
    stadium_national_u21_use_offset=0xB2,
    stadium_national_u19_use_offset=0xB3,
    stadium_pitch_relaid_date_offset=0x28,
    stadium_pitch_relay_required_date_offset=0x2C,
    stadium_seat_color_offset=0x78,
    chairman_business_offset=0x11,
    chairman_interference_offset=0x16,
    chairman_patience_offset=0x19,
    chairman_resources_offset=0x1B,
    chairman_base_adjustment=0,
)


# Steam FM2024 24.4.2+2081827. Only fields proven against the live process are
# enabled here. The current-date global is resolved by the trainer-validated
# ``44 8B 1D ?? ?? ?? ?? 48 FF ?? 31 ?? EB`` RIP-relative signature.
# ``sicomps::FIXTURE_RESULT`` is the short-lived detailed result archive.
# ``db::SCORELINE::BASIC_SCORELINE`` persists simulated matches even when the
# user does not watch them; its FM24 field layout was verified against 1200
# completed records in a live save.
FM24_LAYOUT = GameLayout(
    key="fm24",
    display_name="Football Manager 2024",
    executable_sha256=FM24_EXE_SHA256,
    module_name="fm.exe",
    distribution="steam",
    nation_men_container_offset=0x108,
    nation_youth_rating_offset=0x78C,
    fixture_vtable_rva=0x5A25F58,
    team_vtable_rva=0x5A78848,
    national_team_vtable_rva=0x5A6CAB8,
    nation_vtable_rva=0x5A6CD98,
    club_vtable_rva=0x5A51F38,
    competition_vtable_rva=0x5A54A38,
    actual_player_vtable_rvas=(0x5A4E958,),
    player_and_non_player_vtable_rvas=(0x5C7E798,),
    player_person_offset=0x278,
    player_and_non_player_person_offset=0x368,
    player_positions_offset=0x208,
    player_attributes_offset=0x217,
    player_ca_offset=0x200,
    player_pa_offset=0x202,
    player_ca_bytes=2,
    attribute_display_bias=0,
    game_date_rva=0x631D5BC,
    manager_person_offset=0x450,
    human_manager_vtable_rvas=(0x5A6A388,),
    human_manager_vtable_on_person=True,
    club_policy_departure_pattern_rva=0x32668D3,
    club_policy_salary_pattern_rva=0x3262089,
    database_root_pattern=(
        0x48, 0x8D, 0xB9, None, None, None, None,
        0x4C, 0x8B, 0x0D, None, None, None, None,
        0x48, 0x8D, 0x0D,
    ),
    database_root_probe_rva=0x3BDC363,
    database_root_rel32_offset=10,
    database_root_instruction_size=14,
    human_manager_root_pattern=(
        0x48, 0x8B, 0x35, None, None, None, None,
        0x48, 0x8B, 0x56, 0x18, 0x4C, 0x8B, 0x76, 0x20,
        0x49, 0x29, 0xD6, 0xB0, 0x01, 0x49, 0x83, 0xFE, 0x10,
    ),
    human_manager_root_probe_rva=0x3A278B1,
    human_manager_root_rel32_offset=3,
    human_manager_root_instruction_size=7,
    # Retired in V1.9.1c: the value changes during normal FM24 sessions and
    # cannot serve as a permanent save identity. Local saves use the verified
    # GAME_SAVE_PROVIDER_LOCAL path/label pair instead.
    savegame_id_absolute_address=None,
    fixture_result_vtable_rva=0x5608078,
    fixture_pool_rva=0x6429A68,
    season_result_vtable_rva=0x56218F8,
    match_session_pointer_rva=0x6374480,
    match_session_vtable_rva=0x5836A90,
    game_match_session_vtable_rva=0x5604F08,
    redbull_hook_rva=0x43B3FDF,
    referee_hook_rva=0x1A0AC338,
    ca_growth_hook_rva=0x4F7C463,
    youth_generation_gate_hook_rva=0x217CB60,
    youth_generation_pa_hook_rva=0x21271A2,
    youth_generation_state_hook_rva=0x36238D5,
    youth_son_hook_rva=0x3618E59,
    player_height_offset=0x14E,
    player_weight_offset=0x14C,
    player_sharpness_offset=0x1F4,
    player_fatigue_offset=0x1F6,
    player_fitness_offset=0x1F8,
    player_morale_offset=0x25F,
    player_injury_list_offset=0xF8,
    player_home_reputation_offset=0x1FA,
    player_current_reputation_offset=0x1FC,
    player_world_reputation_offset=0x1FE,
    player_season_stats_root_offset=0x108,
    player_season_stats_total_slot_offset=0x48,
    player_season_stats_block_size=0x78,
    player_career_stats_offset=0x110,
    player_international_appearances_offset=0x3DC,
    player_international_goals_offset=0x3DE,
    player_u21_international_appearances_offset=0x3DD,
    player_u21_international_goals_offset=0x3DF,
    player_international_counter_width=1,
    player_u21_international_counter_width=1,
    team_reputation_offset=0xA8,
    team_type_offset=0x28,
    competition_reputation_offset=0x180,
    competition_reputation_bytes=2,
    person_nationality_offset=0x70,
    person_languages_offset=0x120,
    person_date_of_birth_offset=0x44,
    person_date_of_birth_day_year=True,
    person_hidden_attributes_offset=0x78,
    person_relationships_offset=0x80,
    person_previous_club_offset=0x138,
    person_full_name_offset=0x48,
    person_first_name_offset=0x58,
    person_last_name_offset=0x60,
    person_common_name_offset=0x68,
    staff_person_vtable_rva=0x5A4C358,
    team_manager_person_offset=0xF8,
    staff_domestic_reputation_offset=0xD8,
    staff_current_reputation_offset=0xDA,
    staff_world_reputation_offset=0xDC,
    staff_ca_offset=0xDE,
    staff_pa_offset=0xE0,
    staff_coaching_license_offset=0x16A,
    staff_job_type_offset=0x1C,
    staff_contract_type_offset=0xB3,
    staff_wage_offset=0x18,
    staff_contract_start_offset=0x3C,
    staff_contract_expiry_offset=0x40,
    staff_abilities_verified=True,
    staff_ability_complete_object_offset=0xF0,
    result_away_goals_offset=0x68,
    result_home_outcome_offset=0x6C,
    result_away_outcome_offset=0x6D,
    # FM24 stores a compact <scorer, flags, minute> record per goal in the
    # first vector of result+0x70. FM26 instead uses 16-byte inline records
    # behind result+0x78.
    result_event_root_offset=0x70,
    result_event_record_size=0x08,
    result_event_record_format="fm24",
    fixture_stage_index_offset=0x38,
    fixture_group_index_offset=0x39,
    fixture_round_number_offset=0x3A,
    competition_actual_offset=0xB0,
    actual_competition_stages_offset=0x1C0,
    competition_stage_type_offset=0x48,
    competition_stage_index_offset=0x4C,
    cup_stage_teams_offset=0xA0,
    cup_stage_round_ties_offset=0xA8,
    loan_contract_vtable_rva=0x5C78638,
    player_move_contract_pool_rva=0x6378C80,
    player_move_main_contract_vtable_rva=0x5667C88,
    player_move_fm24_source_cleanup_rva=0x224F2A0,
    player_move_fm24_transfer_rva=0x22515B0,
    player_move_fm24_transaction_allocator_rva=0x2A0A420,
    player_move_fm24_transaction_constructor_rva=0x49DAB60,
    player_move_fm24_transaction_initializer_rva=0x49DAD30,
    player_move_fm24_transaction_prepare_rva=0x2A3DBE0,
    player_move_fm24_transaction_commit_rva=0x394D590,
    player_move_fm24_transaction_transition_rva=0x2A3C980,
    player_move_fm24_transaction_outer_submit_rva=0x3954C90,
    player_move_fm24_transaction_entry_rva=0x39D4F90,
    player_move_fm24_transaction_vtable_rva=0x5C85388,
    player_move_fm24_context_root_rva=0x642B960,
    player_move_fm24_context_vtable_rva=0x5B08480,
    player_move_fm24_contract_factory_rva=0x2341640,
    player_move_fm24_loan_constructor_rva=0x48870C0,
    club_detail_offset=0xB0,
    club_detail2_offset=0x100,
    club_training_ground_offset=0x140,
    club_year_founded_offset=0xD8,
    club_training_facilities_offset=0x120,
    club_youth_facilities_offset=0x12B,
    club_junior_coaching_offset=0x12C,
    club_youth_recruitment_offset=0x12D,
    club_average_attendance_offset=0x68,
    club_minimum_attendance_offset=0x6C,
    club_maximum_attendance_offset=0x70,
    club_season_ticket_holders_offset=0xC0,
    club_social_media_followers_offset=0xF8,
    club_supporters_profile_offset=0x114,
    club_supporters_distribution_offset=0x106,
    club_chairman_status_offset=0xDE,
    club_status_offset=0x16A,
    club_morale_offset=0x121,
    club_ownership_offset=0x78,
    club_finance_offset=0x150,
    finance_balance_offset=0x14,
    # FMRTE 24.4.2 BasicFinances.SugarDaddy: Aeo.Ahk resolves lookup key
    # 679 to the byte field at +0x3C. Shared by the exact Steam/Epic layouts.
    finance_sugar_daddy_offset=0x3C,
    finance_remaining_transfer_budget_offset=0x7CC,
    finance_season_transfer_budget_offset=0x7D0,
    finance_wage_budget_offset=0x810,
    finance_wage_used_offset=0x81C,
    finance_max_wage_offset=0x814,
    finance_average_ticket_price_offset=0x870,
    finance_transfer_revenue_percentage_offset=0x7D8,
    finance_income_statement_period_offsets=(0x280, 0x398, 0x4B0, 0x5D0),
    finance_monthly_summary_offset=0x40,
    club_loans_offset=0x50,
    club_debt_record_size=0x1C,
    club_sponsors_root_offset=0x130,
    club_sponsor_record_size=0x18,
    team_details_offset=0xA0,
    team_details_club_vision_offset=0xC0,
    club_vision_culture_offset=0x78,
    club_culture_record_size=0x40,
    club_culture_vtable_rva=0x574AA78,
    club_culture_constructor_rva=0x2E9A6D0,
    club_culture_initialize_rva=0x294D110,
    club_affiliations_offset=0x118,
    club_affiliation_record_size=0x38,
    club_affiliation_manager_rva=0x642ECD0,
    club_affiliation_create_rva=0x23E63A0,
    club_affiliation_global_vector_offset=0x680,
    club_nation_offset=0xD8,
    club_director_of_football_offset=0x148,
    club_foreground_color_offset=0xA0,
    club_background_color_offset=0xA8,
    club_allow_custom_logo_offset=0x162,
    stadium_vtable_rva=0x5A78248,
    stadium_capacity_offset=0x6C,
    stadium_seating_capacity_offset=0x70,
    stadium_used_capacity_offset=0x74,
    stadium_expansion_capacity_offset=0x7C,
    stadium_pitch_condition_offset=0x98,
    stadium_pitch_type_offset=0x9D,
    stadium_name_offset=0x38,
    stadium_item_id_offset=0x08,
    stadium_id_offset=0x0C,
    stadium_id2_offset=0x10,
    stadium_location_offset=0x50,
    stadium_build_date_offset=0x28,
    stadium_rebuild_date_offset=0x2C,
    stadium_all_seater_capacity_offset=0x78,
    stadium_nearby_offset=0x58,
    stadium_owner_offset=0x80,
    stadium_latitude_offset=0x60,
    stadium_longitude_offset=0x64,
    stadium_state_offset=0x9A,
    stadium_extinct_offset=0x9B,
    stadium_pitch_deterioration_offset=0x9C,
    stadium_pitch_recovery_offset=0x9E,
    stadium_extras_offset=0xA6,
    stadium_national_team_use_offset=0xA1,
    stadium_national_u21_use_offset=0xA2,
    stadium_national_u19_use_offset=0xA3,
    stadium_pitch_relaid_date_offset=0x20,
    stadium_pitch_relay_required_date_offset=0x24,
    stadium_seat_color_offset=0x68,
    chairman_business_offset=0x19,
    chairman_interference_offset=0x1E,
    chairman_patience_offset=0x21,
    chairman_resources_offset=0x23,
    chairman_base_adjustment=0x08,
)


FM24_EPIC_LAYOUT = replace(
    FM24_LAYOUT,
    # Epic 24.4.2 (SHA256 33E8...5378) was checked read-only on 2026-09-09.
    # These roots, object fields, pools, vtables, and Hook entries match the
    # Steam build byte-for-byte. Distribution-specific high-level transaction
    # entrypoints remain disabled below.
    person_previous_club_offset=0x138,
    club_policy_departure_pattern_rva=None,
    club_policy_salary_pattern_rva=None,
    team_type_offset=0x28,
    database_root_probe_rva=0x3BDC363,
    human_manager_root_probe_rva=0x3A278B1,
    display_name="Football Manager 2024 (Epic)",
    executable_sha256=FM24_EPIC_EXE_SHA256,
    distribution="epic",
    club_affiliation_manager_rva=None,
    club_affiliation_create_rva=None,
    club_affiliation_global_vector_offset=0x680,
    club_affiliation_create_pattern=(
        0x41, 0x57, 0x41, 0x56, 0x41, 0x55, 0x41, 0x54,
        0x56, 0x57, 0x55, 0x53, 0x48, 0x83, 0xEC, 0x48,
        0x45, 0x89, 0xCE, 0x44, 0x89, 0xC7, 0x48, 0x89,
        0xCB, 0x48, 0x8B, 0x49, 0x10, 0xE8,
        None, None, None, None,
        0x49, 0x89, 0xC4, 0x48, 0x8B, 0x4B, 0x10, 0x89,
        0xFA, 0xE8, None, None, None, None, 0x4D, 0x85, 0xE4,
    ),
    club_affiliation_manager_call_pattern=(
        0xC7, 0x44, 0x24, 0x20, 0x00, 0x00, 0x00, 0x00,
        0x48, 0x8D, 0x0D, None, None, None, None,
        0x41, 0xB8, 0xFF, 0xFF, 0xFF, 0xFF, 0x41, 0xB1, 0x01,
        0xE8, None, None, None, None, 0x48, 0x85, 0xC0,
    ),
    club_affiliation_manager_rel32_offset=11,
    club_affiliation_manager_instruction_size=15,
    club_affiliation_create_call_rel32_offset=25,
    club_affiliation_create_call_instruction_size=29,
    club_culture_vtable_rva=0x574AA78,
    club_culture_constructor_rva=0x2E9A6D0,
    club_culture_initialize_rva=0x294D110,
    stadium_vtable_rva=0x5A78248,
    savegame_id_absolute_address=None,
    savegame_id_requires_legacy_validation=False,
    fixture_pool_rva=0x6429A68,
    referee_hook_rva=0x19EEAA14,
    ca_growth_hook_rva=0x4F7C463,
    youth_generation_gate_hook_rva=0x217CB60,
    youth_generation_pa_hook_rva=0x21271A2,
    youth_generation_state_hook_rva=0x36238D5,
    youth_son_hook_rva=0x3618E59,
    player_move_contract_pool_rva=0x6378C80,
    player_move_main_contract_vtable_rva=0x5667C88,
    player_move_fm24_source_cleanup_rva=None,
    player_move_fm24_transfer_rva=None,
    player_move_fm24_contract_factory_rva=None,
    player_move_fm24_loan_constructor_rva=None,
    player_move_fm24_transaction_allocator_rva=None,
    player_move_fm24_transaction_constructor_rva=None,
    player_move_fm24_transaction_initializer_rva=None,
    player_move_fm24_transaction_prepare_rva=None,
    player_move_fm24_transaction_commit_rva=None,
    player_move_fm24_transaction_transition_rva=None,
    player_move_fm24_transaction_outer_submit_rva=None,
    player_move_fm24_transaction_entry_rva=None,
    player_move_fm24_transaction_vtable_rva=None,
    player_move_fm24_context_root_rva=None,
    player_move_fm24_context_vtable_rva=None,
)


# Steam FM2024 24.4.0.0. RTTI, date and hook RVAs were collected from the
# exact executable above. Record field offsets are shared with 24.4.2; every
# version-dependent address that differs remains explicit in this layout.
FM24_240_LAYOUT = replace(
    FM24_LAYOUT,
    person_previous_club_offset=None,
    club_policy_departure_pattern_rva=None,
    club_policy_salary_pattern_rva=None,
    team_type_offset=None,
    database_root_probe_rva=None,
    human_manager_root_probe_rva=None,
    club_affiliation_manager_rva=None,
    club_affiliation_create_rva=None,
    club_affiliation_global_vector_offset=None,
    club_culture_vtable_rva=None,
    club_culture_constructor_rva=None,
    club_culture_initialize_rva=None,
    display_name="Football Manager 2024 Steam 24.4.0",
    executable_sha256=FM24_240_EXE_SHA256,
    stadium_vtable_rva=None,
    savegame_id_absolute_address=None,
    fixture_pool_rva=None,
    competition_reputation_offset=None,
    competition_reputation_bytes=None,
    competition_actual_offset=None,
    actual_competition_stages_offset=None,
    competition_stage_type_offset=None,
    competition_stage_index_offset=None,
    cup_stage_teams_offset=None,
    cup_stage_round_ties_offset=None,
    finance_sugar_daddy_offset=None,
    finance_season_transfer_budget_offset=None,
    finance_wage_budget_offset=None,
    finance_wage_used_offset=None,
    finance_max_wage_offset=None,
    finance_average_ticket_price_offset=None,
    finance_transfer_revenue_percentage_offset=None,
    finance_monthly_summary_offset=None,
    club_loans_offset=None,
    club_debt_record_size=None,
    club_sponsors_root_offset=None,
    club_sponsor_record_size=None,
    team_vtable_rva=0x5A78828,
    national_team_vtable_rva=0x5A6CA98,
    nation_vtable_rva=0x5A6CD78,
    player_and_non_player_vtable_rvas=(0x5C7E778,),
    human_manager_vtable_rvas=(0x5A6A368,),
    game_match_session_vtable_rva=None,
    redbull_hook_rva=0x43B3C8F,
    referee_hook_rva=0x1A56A559,
    ca_growth_hook_rva=None,
    youth_generation_gate_hook_rva=None,
    youth_generation_pa_hook_rva=None,
    youth_generation_state_hook_rva=None,
    youth_son_hook_rva=None,
    player_move_contract_pool_rva=None,
    player_move_main_contract_vtable_rva=None,
    player_move_fm24_source_cleanup_rva=None,
    player_move_fm24_transfer_rva=None,
    player_move_fm24_contract_factory_rva=None,
    player_move_fm24_loan_constructor_rva=None,
    player_move_fm24_transaction_allocator_rva=None,
    player_move_fm24_transaction_constructor_rva=None,
    player_move_fm24_transaction_initializer_rva=None,
    player_move_fm24_transaction_prepare_rva=None,
    player_move_fm24_transaction_commit_rva=None,
    player_move_fm24_transaction_transition_rva=None,
    player_move_fm24_transaction_outer_submit_rva=None,
    player_move_fm24_transaction_entry_rva=None,
    player_move_fm24_transaction_vtable_rva=None,
    player_move_fm24_context_root_rva=None,
    player_move_fm24_context_vtable_rva=None,
)


# FM2024 24.4.1.0 custom-directory build. Its collected RTTI, date, result,
# player/staff and match-session addresses match the verified 24.4.2 layout.
# The referee hook is build-specific and therefore remains explicit here.
FM24_241_LAYOUT = replace(
    FM24_LAYOUT,
    person_previous_club_offset=None,
    club_policy_departure_pattern_rva=None,
    club_policy_salary_pattern_rva=None,
    team_type_offset=None,
    database_root_probe_rva=None,
    human_manager_root_probe_rva=None,
    club_affiliation_manager_rva=None,
    club_affiliation_create_rva=None,
    club_affiliation_global_vector_offset=None,
    club_culture_vtable_rva=None,
    club_culture_constructor_rva=None,
    club_culture_initialize_rva=None,
    display_name="Football Manager 2024 Steam 24.4.1 (Beta)",
    executable_sha256=FM24_241_EXE_SHA256,
    stadium_vtable_rva=None,
    savegame_id_absolute_address=None,
    fixture_pool_rva=None,
    competition_reputation_offset=None,
    competition_reputation_bytes=None,
    competition_actual_offset=None,
    actual_competition_stages_offset=None,
    competition_stage_type_offset=None,
    competition_stage_index_offset=None,
    cup_stage_teams_offset=None,
    cup_stage_round_ties_offset=None,
    finance_sugar_daddy_offset=None,
    finance_season_transfer_budget_offset=None,
    finance_wage_budget_offset=None,
    finance_wage_used_offset=None,
    finance_max_wage_offset=None,
    finance_average_ticket_price_offset=None,
    finance_transfer_revenue_percentage_offset=None,
    finance_monthly_summary_offset=None,
    club_loans_offset=None,
    club_debt_record_size=None,
    club_sponsors_root_offset=None,
    club_sponsor_record_size=None,
    referee_hook_rva=0x19DA4E2A,
    ca_growth_hook_rva=None,
    youth_generation_gate_hook_rva=None,
    youth_generation_pa_hook_rva=None,
    youth_generation_state_hook_rva=None,
    youth_son_hook_rva=None,
    player_move_contract_pool_rva=None,
    player_move_main_contract_vtable_rva=None,
    player_move_fm24_source_cleanup_rva=None,
    player_move_fm24_transfer_rva=None,
    player_move_fm24_contract_factory_rva=None,
    player_move_fm24_loan_constructor_rva=None,
    player_move_fm24_transaction_allocator_rva=None,
    player_move_fm24_transaction_constructor_rva=None,
    player_move_fm24_transaction_initializer_rva=None,
    player_move_fm24_transaction_prepare_rva=None,
    player_move_fm24_transaction_commit_rva=None,
    player_move_fm24_transaction_transition_rva=None,
    player_move_fm24_transaction_outer_submit_rva=None,
    player_move_fm24_transaction_entry_rva=None,
    player_move_fm24_transaction_vtable_rva=None,
    player_move_fm24_context_root_rva=None,
    player_move_fm24_context_vtable_rva=None,
)


# FM2024 XGP Content build. The protected executable cannot be hashed from
# disk, so selection uses the mapped PE timestamp and SizeOfImage. RTTI and
# hook RVAs were resolved from the live image; record offsets remain the
# FM2024 layout. Match-session state stays disabled until its global pointer
# has been independently located.
FM24_XGP_LAYOUT = replace(
    FM24_LAYOUT,
    person_previous_club_offset=None,
    club_policy_departure_pattern_rva=None,
    club_policy_salary_pattern_rva=None,
    team_type_offset=None,
    database_root_probe_rva=None,
    human_manager_root_probe_rva=None,
    club_affiliation_manager_rva=None,
    club_affiliation_create_rva=None,
    club_affiliation_global_vector_offset=None,
    club_culture_vtable_rva=None,
    club_culture_constructor_rva=None,
    club_culture_initialize_rva=None,
    display_name="Football Manager 2024 (XGP Beta)",
    executable_sha256="",
    distribution="xgp",
    stadium_vtable_rva=None,
    ca_growth_hook_rva=None,
    youth_generation_gate_hook_rva=None,
    youth_generation_pa_hook_rva=None,
    youth_generation_state_hook_rva=None,
    youth_son_hook_rva=None,
    player_move_contract_pool_rva=None,
    player_move_main_contract_vtable_rva=None,
    player_move_fm24_source_cleanup_rva=None,
    player_move_fm24_transfer_rva=None,
    player_move_fm24_contract_factory_rva=None,
    player_move_fm24_loan_constructor_rva=None,
    player_move_fm24_transaction_allocator_rva=None,
    player_move_fm24_transaction_constructor_rva=None,
    player_move_fm24_transaction_initializer_rva=None,
    player_move_fm24_transaction_prepare_rva=None,
    player_move_fm24_transaction_commit_rva=None,
    player_move_fm24_transaction_transition_rva=None,
    player_move_fm24_transaction_outer_submit_rva=None,
    player_move_fm24_transaction_entry_rva=None,
    player_move_fm24_transaction_vtable_rva=None,
    player_move_fm24_context_root_rva=None,
    player_move_fm24_context_vtable_rva=None,
    savegame_id_absolute_address=None,
    fixture_pool_rva=None,
    # No independent BasicFinances.SugarDaddy field evidence for XGP.
    finance_sugar_daddy_offset=None,
    fixture_vtable_rva=0x5A2B288,
    fixture_result_vtable_rva=0x561A1C8,
    # Keep protected XGP persistent results disabled until its RTTI probe is
    # independently collected; never reuse the Steam vtable in another image.
    season_result_vtable_rva=None,
    team_vtable_rva=0x5A7DBC8,
    national_team_vtable_rva=0x5A71E28,
    nation_vtable_rva=0x5A72108,
    club_vtable_rva=0x5A57298,
    competition_vtable_rva=0x5A59D98,
    actual_player_vtable_rvas=(0x5A53CB8,),
    player_and_non_player_vtable_rvas=(0x5C84098,),
    staff_person_vtable_rva=0x5A516B8,
    human_manager_vtable_rvas=(0x5A6F6F8,),
    game_date_rva=0x633D36C,
    match_session_pointer_rva=None,
    match_session_vtable_rva=0x5847280,
    game_match_session_vtable_rva=None,
    redbull_hook_rva=0x430FE9F,
    referee_hook_rva=0x19688732,
    loan_contract_vtable_rva=None,
)


FM26_XGP_TEMPLATE = replace(
    FM26_LAYOUT,
    national_team_nation_offset=None,
    nationality_vtable_rva=None,
    club_policy_departure_pattern_rva=None,
    club_policy_salary_pattern_rva=None,
    club_policy_salary_promise_pattern_rva=None,
    club_affiliation_manager_rva=None,
    club_affiliation_create_rva=None,
    club_affiliation_global_vector_offset=None,
    club_culture_vtable_rva=None,
    club_culture_constructor_rva=None,
    club_culture_initialize_rva=None,
    database_root_probe_rva=None,
    human_manager_root_probe_rva=None,
    display_name="Football Manager 26 (XGP)",
    game_version="0.9.9957.0",
    executable_sha256="",
    distribution="xgp",
    stadium_vtable_rva=None,
    fixture_pool_rva=None,
    finance_sugar_daddy_offset=None,
    fixture_vtable_rva=0,
    fixture_result_vtable_rva=None,
    season_result_vtable_rva=None,
    team_vtable_rva=0,
    national_team_vtable_rva=0,
    nation_vtable_rva=0,
    club_vtable_rva=0,
    competition_vtable_rva=0,
    actual_player_vtable_rvas=(),
    loan_contract_vtable_rva=None,
    player_and_non_player_vtable_rvas=(),
    human_manager_vtable_rvas=(),
    staff_person_vtable_rva=None,
    person_previous_club_offset=None,
    player_season_stats_root_offset=None,
    player_season_stats_total_slot_offset=None,
    player_season_stats_block_size=None,
    game_date_rva=None,
    savegame_root_rvas=(),
    match_engine_phase_rva=None,
    match_engine_mode_rva=0x4D7F99C,
    match_engine_active_state_rva=None,
    play_fixture_manager_pointer_rva=None,
    play_fixture_manager_vtable_rva=None,
    player_move_contract_pool_rva=None,
    player_move_loan_contract_pool_rva=None,
    player_move_loan_contract_constructor_rva=None,
    player_move_contract_allocate_rva=None,
    player_move_contract_release_rva=None,
    player_move_contract_factory_global_rva=None,
    player_move_contract_factory_target_rva=None,
    player_move_main_contract_vtable_rva=None,
    player_move_terminate_contract_rva=None,
    player_move_prepare_loan_rva=None,
    player_move_club_method_rva=None,
    future_transfer_manager_vtable_rva=None,
    future_transfer_full_offer_vtable_rva=None,
    future_transfer_loan_offer_vtable_rva=None,
    ca_growth_hook_rva=None,
    youth_generation_gate_hook_rva=None,
    youth_generation_pa_hook_rva=None,
    youth_generation_state_hook_rva=None,
    youth_son_hook_rva=None,
)


SUPPORTED_LAYOUTS = (
    FM26_LAYOUT, FM24_LAYOUT, FM24_240_LAYOUT, FM24_241_LAYOUT, FM24_EPIC_LAYOUT,
)
LAYOUT_BY_HASH = {layout.executable_sha256: layout for layout in SUPPORTED_LAYOUTS}


@lru_cache(maxsize=8)
def _hash_executable(path: str, size: int, modified_ns: int) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def executable_hash(path: str) -> str:
    details = Path(path).stat()
    return _hash_executable(str(Path(path).resolve()), details.st_size, details.st_mtime_ns)


def layout_for_executable(path: str) -> GameLayout | None:
    if not path:
        return None
    try:
        return LAYOUT_BY_HASH.get(executable_hash(path))
    except OSError:
        return None


def _pe_sections(raw: mmap.mmap) -> list[tuple[int, int, int, int]]:
    pe_offset = struct.unpack_from("<I", raw, 0x3C)[0]
    if raw[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise ValueError("invalid PE image")
    count = struct.unpack_from("<H", raw, pe_offset + 6)[0]
    optional_size = struct.unpack_from("<H", raw, pe_offset + 20)[0]
    table = pe_offset + 24 + optional_size
    return [
        struct.unpack_from("<IIII", raw, table + index * 40 + 8)
        for index in range(count)
    ]


def _pe_image_base(raw: mmap.mmap) -> int:
    pe_offset = struct.unpack_from("<I", raw, 0x3C)[0]
    return struct.unpack_from("<Q", raw, pe_offset + 24 + 24)[0]


def _read_rva(
    raw: mmap.mmap, sections: list[tuple[int, int, int, int]], rva: int, size: int,
) -> bytes | None:
    for virtual_size, virtual_rva, raw_size, raw_offset in sections:
        relative = rva - virtual_rva
        if 0 <= relative and relative + size <= raw_size:
            return raw[raw_offset + relative:raw_offset + relative + size]
    return None


def _occurrences(
    raw: mmap.mmap, sections: list[tuple[int, int, int, int]], needle: bytes,
):
    for _virtual_size, virtual_rva, raw_size, raw_offset in sections:
        cursor, end = raw_offset, min(len(raw), raw_offset + raw_size)
        while True:
            cursor = raw.find(needle, cursor, end)
            if cursor < 0:
                break
            yield virtual_rva + cursor - raw_offset
            cursor += 1


def _pattern_occurrences(
    raw: mmap.mmap, sections: list[tuple[int, int, int, int]],
    pattern: tuple[int | None, ...],
) -> list[int]:
    runs: list[tuple[int, bytes]] = []
    start = 0
    while start < len(pattern):
        while start < len(pattern) and pattern[start] is None:
            start += 1
        end = start
        while end < len(pattern) and pattern[end] is not None:
            end += 1
        if end > start:
            runs.append((start, bytes(int(value) for value in pattern[start:end])))
        start = end
    if not runs:
        return []
    anchor, needle = max(runs, key=lambda row: len(row[1]))
    rows: list[int] = []
    for _virtual_size, virtual_rva, raw_size, raw_offset in sections:
        cursor, end = raw_offset, min(len(raw), raw_offset + raw_size)
        while True:
            cursor = raw.find(needle, cursor, end)
            if cursor < 0:
                break
            start = cursor - anchor
            if raw_offset <= start and start + len(pattern) <= end and all(
                value is None or raw[start + index] == value
                for index, value in enumerate(pattern)
            ):
                rows.append(virtual_rva + start - raw_offset)
            cursor += 1
    return rows


def _resolve_fm26_game_date_rva(
    raw: Any, sections: list[tuple[int, int, int, int]],
) -> int:
    """Resolve FM26's current-date global from its RIP-relative load."""
    hits = _pattern_occurrences(raw, sections, FM26_GAME_DATE_PATTERN)
    if len(hits) != 1:
        raise RuntimeError(
            f"FM26 game-date signature matched {len(hits)} locations"
        )
    instruction_rva = hits[0] + 14
    instruction = _read_rva(raw, sections, instruction_rva, 6)
    if not instruction or len(instruction) != 6:
        raise RuntimeError("FM26 game-date instruction is unreadable")
    return instruction_rva + 6 + struct.unpack_from("<i", instruction, 2)[0]


def _resolve_fm26_game_date_rva_from_path(image_path: str) -> int:
    with Path(image_path).open("rb") as stream, mmap.mmap(
        stream.fileno(), 0, access=mmap.ACCESS_READ,
    ) as raw:
        return _resolve_fm26_game_date_rva(raw, _pe_sections(raw))


@lru_cache(maxsize=8)
def _cached_fm26_game_date_rva(
    image_path: str, file_size: int, modified_ns: int, module_sha256: str,
) -> int:
    # File identity is part of the key. The absolute process address is never
    # cached here; callers add the current session's module base each time.
    del file_size, modified_ns, module_sha256
    return _resolve_fm26_game_date_rva_from_path(image_path)


def resolve_fm26_game_date_rva(
    module_path: str, module_sha256: str = "",
) -> int:
    path = Path(module_path).resolve()
    details = path.stat()
    return _cached_fm26_game_date_rva(
        str(path), details.st_size, details.st_mtime_ns,
        str(module_sha256 or "").upper(),
    )


def _validate_fm26_runtime_game_date(
    process: Any, module_base: int, module_size: int, game_date_rva: int,
) -> None:
    from fm_collector.win32 import read_process_memory

    if not 0 <= game_date_rva <= module_size - 4:
        raise RuntimeError("FM26 game-date target is outside game_plugin.dll")
    address = module_base + game_date_rva
    first = read_process_memory(process, address, 4)
    second = read_process_memory(process, address, 4)
    if not first or len(first) != 4 or not second or len(second) != 4:
        raise RuntimeError("FM26 current game date is unreadable")
    if first != second:
        raise RuntimeError("FM26 current game date changed during validation")
    date_code = struct.unpack("<I", second)[0]
    year, day = date_code >> 16, date_code & 0x1FF
    if (
        not 1900 <= year <= 2200
        or not 1 <= day <= 366
        or (year == 1900 and day == 1)
    ):
        raise RuntimeError("FM26 current game-date signature failed validation")


def resolve_fm26_steam_layout(
    pid: int, module_base: int, module_size: int, module_path: str,
) -> GameLayout:
    """Bind the verified Steam profile to its dynamically resolved date RVA."""
    from fm_collector.win32 import open_process

    try:
        module_hash = executable_hash(module_path)
        game_date_rva = resolve_fm26_game_date_rva(module_path, module_hash)
    except OSError as error:
        raise RuntimeError(
            "FM26 Steam game_plugin.dll cannot be read for identity resolution"
        ) from error
    if module_hash != FM26_STEAM_PLUGIN_SHA256:
        raise RuntimeError(
            "FM26 Steam game_plugin.dll build mismatch; dynamically resolved "
            f"game-date RVA 0x{game_date_rva:X}, but the remaining 26.3.2 "
            f"layout is not verified for plugin SHA256 {module_hash}"
        )
    expected_rva = int(FM26_LAYOUT.game_date_rva or 0)
    if game_date_rva != expected_rva:
        raise RuntimeError(
            "FM26 Steam verified plugin resolved an unexpected game-date RVA: "
            f"0x{game_date_rva:X} (expected 0x{expected_rva:X})"
        )
    with open_process(pid) as process:
        _validate_fm26_runtime_game_date(
            process, module_base, module_size, game_date_rva,
        )
    return replace(FM26_LAYOUT, game_date_rva=game_date_rva)


def _rtti_vtables(
    raw: mmap.mmap, sections: list[tuple[int, int, int, int]], name: str,
) -> list[int]:
    preferred_base = _pe_image_base(raw)
    vtables: set[int] = set()
    # FM's large .rodata section contains assets rather than MSVC RTTI. Avoid
    # rescanning hundreds of megabytes for every required class name.
    rtti_sections = [section for section in sections if section[2] <= 128 * 1024 * 1024]
    for name_rva in _occurrences(raw, rtti_sections, name.encode("ascii") + b"\0"):
        type_rva = name_rva - 16
        for reference_rva in _occurrences(raw, rtti_sections, struct.pack("<I", type_rva)):
            locator_rva = reference_rva - 12
            locator = _read_rva(raw, sections, locator_rva, 24)
            if not locator:
                continue
            signature, _offset, _cd, found_type, _class, self_rva = struct.unpack(
                "<IIIIII", locator,
            )
            if signature != 1 or found_type != type_rva or self_rva != locator_rva:
                continue
            pointer = struct.pack("<Q", preferred_base + locator_rva)
            for pointer_rva in _occurrences(raw, rtti_sections, pointer):
                vtables.add(pointer_rva + 8)
    return sorted(vtables)


def _validate_fm26_xgp_core_rtti(resolved: dict[str, list[int]]) -> None:
    required_single = (
        "fixture", "fixture_result", "season_result", "team", "national_team",
        "nation", "club", "competition",
    )
    if any(len(resolved[key]) != 1 for key in required_single):
        raise RuntimeError("XGP RTTI layout is incomplete or ambiguous")


def _alternate_plugin_path(process_path: str, module_path: str) -> Path | None:
    candidates = [
        Path(module_path),
        Path(process_path).parent / "fm_Data" / "Plugins" / "x86_64" / "game_plugin.dll",
    ]
    for candidate in candidates:
        try:
            with candidate.open("rb") as source:
                if source.read(2) == b"MZ":
                    return candidate
        except OSError:
            continue
    return None


def module_pe_identity(process: Any, module: Any) -> tuple[int, int] | None:
    from fm_collector.win32 import read_process_memory

    header = read_process_memory(process, module.base_address, 0x1000)
    if not header or header[:2] != b"MZ":
        return None
    pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
    if header[pe_offset:pe_offset + 4] != b"PE\0\0":
        return None
    timestamp = struct.unpack_from("<I", header, pe_offset + 8)[0]
    image_size = struct.unpack_from("<I", header, pe_offset + 24 + 56)[0]
    return timestamp, image_size


def is_fm26_xgp_module(process: Any, module: Any) -> bool:
    return module_pe_identity(process, module) == (
        FM26_XGP_PE_TIMESTAMP, FM26_XGP_IMAGE_SIZE,
    )


def is_fm24_xgp_module(process: Any, module: Any) -> bool:
    return module_pe_identity(process, module) == (
        FM24_XGP_PE_TIMESTAMP, FM24_XGP_IMAGE_SIZE,
    )


def _scan_process_span_for_bytes(
    process: Any, start: int, size: int, needle: bytes, *, chunk_size: int = 4 * 1024 * 1024,
):
    from fm_collector.win32 import read_process_memory

    carry = b""
    for address in range(start, start + size, chunk_size):
        data = read_process_memory(process, address, min(chunk_size, start + size - address))
        if not data:
            carry = b""
            continue
        combined = carry + data
        combined_base = address - len(carry)
        cursor = 0
        while True:
            cursor = combined.find(needle, cursor)
            if cursor < 0:
                break
            yield combined_base + cursor
            cursor += 1
        carry = combined[-(len(needle) - 1):] if len(needle) > 1 else b""


def _resolve_match_session_pointer_rva(
    process: Any, module_base: int, module_size: int, vtable_rva: int,
) -> int | None:
    from fm_collector.win32 import (
        MEM_IMAGE, MEM_PRIVATE, PAGE_READWRITE, PAGE_WRITECOPY,
        PAGE_EXECUTE_READWRITE, PAGE_EXECUTE_WRITECOPY,
        iter_readable_regions, read_process_memory,
    )

    vtable = module_base + vtable_rva
    object_hits: list[int] = []
    needle = struct.pack("<Q", vtable)
    for region in iter_readable_regions(process):
        if region.type != MEM_PRIVATE:
            continue
        for address in _scan_process_span_for_bytes(process, region.base_address, region.size, needle):
            vector = read_process_memory(process, address + 0x08, 24)
            if not vector or len(vector) != 24:
                continue
            begin, end, capacity = struct.unpack("<QQQ", vector)
            if begin <= end <= capacity and (not begin or (end - begin) % 8 == 0):
                object_hits.append(address)
                if len(object_hits) >= 16:
                    break
        if len(object_hits) >= 16:
            break
    if not object_hits:
        return None

    writable = {
        PAGE_READWRITE, PAGE_WRITECOPY,
        PAGE_EXECUTE_READWRITE, PAGE_EXECUTE_WRITECOPY,
    }
    candidates: set[int] = set()
    module_end = module_base + module_size
    for object_address in object_hits:
        pointer = struct.pack("<Q", object_address)
        for region in iter_readable_regions(process):
            if region.type != MEM_IMAGE or region.protect not in writable:
                continue
            start = max(module_base, region.base_address)
            end = min(module_end, region.base_address + region.size)
            if start >= end:
                continue
            for address in _scan_process_span_for_bytes(process, start, end - start, pointer):
                candidates.add(address - module_base)
    return next(iter(candidates)) if len(candidates) == 1 else None


@lru_cache(maxsize=4)
def resolve_fm24_xgp_layout(pid: int, module_base: int, module_size: int) -> GameLayout:
    from fm_collector.win32 import open_process, read_process_memory

    with open_process(pid) as process:
        date_raw = read_process_memory(
            process, module_base + int(FM24_XGP_LAYOUT.game_date_rva or 0), 4,
        )
        if not date_raw or len(date_raw) != 4:
            raise RuntimeError("FM24 XGP current game date is unreadable")
        date_code = struct.unpack("<I", date_raw)[0]
        year, day = date_code >> 16, date_code & 0x1FF
        if not 1900 <= year <= 2200 or not 1 <= day <= 366:
            raise RuntimeError("FM24 XGP current game-date validation failed")
        for rva in (
            FM24_XGP_LAYOUT.fixture_vtable_rva,
            FM24_XGP_LAYOUT.team_vtable_rva,
            FM24_XGP_LAYOUT.club_vtable_rva,
            FM24_XGP_LAYOUT.competition_vtable_rva,
            FM24_XGP_LAYOUT.actual_player_vtable_rvas[0],
        ):
            method = read_process_memory(process, module_base + rva, 8)
            if not method or len(method) != 8:
                raise RuntimeError("FM24 XGP vtable validation failed")
            target = struct.unpack("<Q", method)[0]
            if not module_base <= target < module_base + module_size:
                raise RuntimeError("FM24 XGP vtable method is outside fm.exe")
        match_session_pointer_rva = _resolve_match_session_pointer_rva(
            process, module_base, module_size,
            int(FM24_XGP_LAYOUT.match_session_vtable_rva or 0),
        )
    return replace(
        FM24_XGP_LAYOUT,
        match_session_pointer_rva=match_session_pointer_rva,
    )


@lru_cache(maxsize=4)
def resolve_fm26_xgp_layout(
    pid: int, process_path: str, module_base: int, module_size: int, module_path: str,
) -> GameLayout:
    from fm_collector.win32 import open_process, read_process_memory

    image_path = _alternate_plugin_path(process_path, module_path)
    if image_path is None:
        raise RuntimeError("unable to read the XGP game_plugin.dll through its install path")
    names = {
        "fixture": ".?AVFIXTURE@sicomps@@",
        "fixture_result": ".?AVFIXTURE_RESULT@sicomps@@",
        "season_result": ".?AVBASIC_SCORELINE@SCORELINE@db@@",
        "team": ".?AVTEAM@db@@",
        "national_team": ".?AVNATIONAL_TEAM@db@@",
        "nation": ".?AVNATIONAL_TEAM_CONTAINER@db@@",
        "club": ".?AVCLUB@db@@",
        "competition": ".?AVCOMP@db@@",
        "player": ".?AVACTUAL_PLAYER@db@@",
        "player_non_player": ".?AVACTUAL_PLAYER_AND_NON_PLAYER@db@@",
        "staff": ".?AVACTUAL_NON_PLAYER@db@@",
        "future_transfer_manager": ".?AVTRANSFER_MANAGER@@",
        "future_transfer_full_offer": ".?AVFULL_TRANSFER_OFFER@@",
        "future_transfer_loan_offer": ".?AVLOAN_OFFER@@",
    }
    with image_path.open("rb") as stream, mmap.mmap(
        stream.fileno(), 0, access=mmap.ACCESS_READ,
    ) as raw:
        sections = _pe_sections(raw)
        with ThreadPoolExecutor(max_workers=4) as executor:
            resolved = dict(zip(
                names,
                executor.map(
                    lambda name: _rtti_vtables(raw, sections, name),
                    names.values(),
                ),
            ))
        _validate_fm26_xgp_core_rtti(resolved)
        if not resolved["player"] or not resolved["player_non_player"] or len(resolved["staff"]) < 2:
            raise RuntimeError("XGP player RTTI layout is incomplete")

        human_markers = list(_occurrences(
            raw, sections, b"_number_news_item_to_human\0",
        ))
        if len(human_markers) != 1:
            raise RuntimeError("XGP human-manager marker is ambiguous")
        human_manager_rva = human_markers[0] + 0x20

        game_date_rva = _resolve_fm26_game_date_rva(raw, sections)

        save_pattern = (
            0x48, 0x8B, 0x35, None, None, None, None, 0x48, 0x85, 0xF6,
            0x74, None, 0x48, 0x8D, 0x05, None, None, None, None,
            0x48, 0x89, 0x86, 0x00, 0x02, 0x00, 0x00,
        )
        save_hits = _pattern_occurrences(raw, sections, save_pattern)
        if len(save_hits) != 1:
            raise RuntimeError(f"XGP save-root signature matched {len(save_hits)} locations")
        save_raw = _read_rva(raw, sections, save_hits[0], 7)
        if not save_raw:
            raise RuntimeError("XGP save-root instruction is unreadable")
        save_root_rva = save_hits[0] + 7 + struct.unpack_from("<i", save_raw, 3)[0]

    layout = replace(
        FM26_XGP_TEMPLATE,
        fixture_vtable_rva=resolved["fixture"][0],
        fixture_result_vtable_rva=resolved["fixture_result"][0],
        season_result_vtable_rva=resolved["season_result"][0],
        team_vtable_rva=resolved["team"][0],
        national_team_vtable_rva=resolved["national_team"][0],
        nation_vtable_rva=resolved["nation"][0],
        club_vtable_rva=resolved["club"][0],
        competition_vtable_rva=resolved["competition"][0],
        actual_player_vtable_rvas=(resolved["player"][0],),
        player_and_non_player_vtable_rvas=(resolved["player_non_player"][0],),
        staff_person_vtable_rva=resolved["staff"][1],
        human_manager_vtable_rvas=(human_manager_rva,),
        future_transfer_manager_vtable_rva=(
            resolved["future_transfer_manager"][0]
            if len(resolved["future_transfer_manager"]) == 1 else None
        ),
        future_transfer_full_offer_vtable_rva=(
            resolved["future_transfer_full_offer"][0]
            if len(resolved["future_transfer_full_offer"]) == 1 else None
        ),
        future_transfer_loan_offer_vtable_rva=(
            resolved["future_transfer_loan_offer"][0]
            if len(resolved["future_transfer_loan_offer"]) == 1 else None
        ),
        game_date_rva=game_date_rva,
        savegame_root_rvas=(save_root_rva,),
    )
    with open_process(pid) as process:
        _validate_fm26_runtime_game_date(
            process, module_base, module_size, game_date_rva,
        )
        for rva in (
            layout.fixture_vtable_rva, layout.team_vtable_rva, layout.club_vtable_rva,
            layout.competition_vtable_rva, layout.actual_player_vtable_rvas[0],
        ):
            method = read_process_memory(process, module_base + rva, 8)
            if not method or len(method) != 8:
                raise RuntimeError("XGP vtable validation failed")
            target = struct.unpack("<Q", method)[0]
            if not module_base <= target < module_base + module_size:
                raise RuntimeError("XGP vtable method is outside game_plugin.dll")
    return layout
