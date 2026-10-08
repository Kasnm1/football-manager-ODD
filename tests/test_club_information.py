from pathlib import Path
import struct

from tools.club_reader import STAFF_JOB_TYPES, _club_information, _limited_liability_ownership_block
from tools.game_layout import (
    FM24_240_LAYOUT, FM24_241_LAYOUT, FM24_EPIC_LAYOUT, FM24_LAYOUT, FM24_XGP_LAYOUT,
    FM26_LAYOUT, FM26_XGP_TEMPLATE,
)


ROOT = Path(__file__).resolve().parents[1]


def test_staff_job_type_8_is_managing_director() -> None:
    assert STAFF_JOB_TYPES[8] == "常务总监"


def test_limited_liability_ownership_preserves_promises_and_clears_elections() -> None:
    original = bytes.fromhex("785634122a030507021e3201")
    updated = _limited_liability_ownership_block(original)
    assert updated == bytes.fromhex("000000002a01000000000000")


def test_sugar_daddy_write_uses_verified_version_layouts():
    assert FM26_LAYOUT.finance_sugar_daddy_offset == 0x3C
    assert FM24_LAYOUT.finance_sugar_daddy_offset == 0x3C
    assert FM24_EPIC_LAYOUT.finance_sugar_daddy_offset == 0x3C
    assert FM24_240_LAYOUT.finance_sugar_daddy_offset is None
    assert FM24_241_LAYOUT.finance_sugar_daddy_offset is None
    assert FM24_XGP_LAYOUT.finance_sugar_daddy_offset is None
    assert FM26_XGP_TEMPLATE.finance_sugar_daddy_offset is None


class FakeReader:
    def __init__(self, layout):
        self.layout = layout
        self.module_base = 0x10000000
        self.pointers = {
            0x1000 + 0x30: 0x2000,
            0x2000: self.module_base + layout.club_vtable_rva,
            0x2000 + 0xB0: 0x3000,
            0x2000 + 0x100: 0x4000,
            0x1000 + 0x78: 0x5000,
            0x2000 + 0x140: 0x6000,
            0x2000 + layout.club_finance_offset: 0x7000,
            0x7000 + 0x08: 0x2000,
        }
        self.u32_values = {
            0x1000 + 0x0C: 42,
            0x2000 + 0x0C: 42,
            0x3000 + 0x68: 32100,
            0x3000 + 0x6C: 28000,
            0x3000 + 0x70: 38000,
            0x4000 + 0xB8: 12000,
            0x4000 + 0xC0: 12000,
            0x4000 + 0xF0: 550000,
            0x4000 + 0xF8: 550000,
            0x5000 + 0x6C: 40000,
            0x5000 + 0x70: 39000,
            0x5000 + 0x74: 38000,
            0x5000 + 0x7C: 52000,
            0x5000 + 0x80: 39000,
            0x5000 + 0x84: 38000,
            0x5000 + 0x8C: 52000,
            0x6000 + 0x6C: 0,
            0x6000 + 0x70: 0,
            0x6000 + 0x74: 0,
            0x6000 + 0x7C: 0,
            0x6000 + 0x80: 0,
            0x6000 + 0x84: 0,
            0x6000 + 0x8C: 0,
        }
        self.u8_values = {
            0x4000 + layout.club_ownership_offset + 0x10: 0x2A,
            0x4000 + layout.club_ownership_offset + 0x11: 3,
            0x4000 + 0x120: 18,
            0x4000 + 0x12B: 16,
            0x4000 + 0x12C: 17,
            0x4000 + 0x12D: 15,
            0x4000 + 0x118: 18,
            0x4000 + 0x123: 16,
            0x4000 + 0x124: 17,
            0x4000 + 0x125: 15,
            0x5000 + 0x98: 92,
            0x5000 + 0x9D: 2,
            0x6000 + 0x98: 80,
            0x6000 + 0x9D: 1,
            0x5000 + 0xA8: 92,
            0x5000 + 0xAD: 2,
            0x6000 + 0xA8: 80,
            0x6000 + 0xAD: 1,
        }
        for base, capacity, seating, used, expansion in (
            (0x5000, 40000, 39000, 38000, 52000),
            (0x6000, 0, 0, 0, 0),
        ):
            self.u32_values[base + layout.stadium_capacity_offset] = capacity
            self.u32_values[base + layout.stadium_seating_capacity_offset] = seating
            self.u32_values[base + layout.stadium_used_capacity_offset] = used
            self.u32_values[base + layout.stadium_expansion_capacity_offset] = expansion
        self.u8_values[0x5000 + layout.stadium_state_offset] = 5
        self.u8_values[0x5000 + layout.stadium_extinct_offset] = 64
        self.u8_values[0x5000 + layout.stadium_national_team_use_offset] = 2
        self.byte_values = {
            0x4000 + layout.club_ownership_offset + 0x0C:
                bytes.fromhex("785634122a030507021e3201"),
            0x4000 + layout.club_supporters_profile_offset: bytes((12, 19, 13, 10, 17, 10)),
            0x4000 + layout.club_supporters_distribution_offset: bytes((11, 30, 28, 10, 3, 18)),
            0x7000 + layout.finance_balance_offset: struct.pack("<i", -20_134_098),
            0x7000 + layout.finance_remaining_transfer_budget_offset: struct.pack("<i", 29_645_734),
        }
        if layout.finance_season_transfer_budget_offset is not None:
            self.byte_values.update({
                0x7000 + layout.finance_season_transfer_budget_offset: struct.pack("<i", 31_766_152),
                0x7000 + layout.finance_wage_budget_offset: struct.pack("<i", 4_414_338),
                0x7000 + layout.finance_wage_used_offset: struct.pack("<i", 4_715_251),
                0x7000 + layout.finance_max_wage_offset: struct.pack("<i", 595_935),
                0x7000 + layout.finance_average_ticket_price_offset: struct.pack("<f", 40.0),
            })
            self.u8_values[0x7000 + layout.finance_transfer_revenue_percentage_offset] = 25
        if layout.finance_sugar_daddy_offset is not None:
            self.u8_values[0x7000 + layout.finance_sugar_daddy_offset] = 2
        if layout.finance_income_statement_period_offsets is not None:
            for index, period_offset in enumerate(layout.finance_income_statement_period_offsets):
                statement = bytearray(0xE0)
                struct.pack_into("<I", statement, 0x00, 500 + index)
                struct.pack_into("<I", statement, 0x04, 1_225_707 + index)
                struct.pack_into("<I", statement, 0x9C, 1_496_430 + index)
                struct.pack_into("<I", statement, 0xB0, 3_149_477 + index)
                self.byte_values[0x7000 + period_offset] = bytes(statement)
        if layout.finance_monthly_summary_offset is not None:
            summary_offset = layout.finance_monthly_summary_offset
            self.byte_values.update({
                0x7000 + summary_offset: struct.pack("<II", 2, 0),
                0x7000 + summary_offset + 8: struct.pack("<QQ", 0xA000, 0xA030),
                0xA000: struct.pack("<i28xi", -131_890_427, 227_483_750),
                0xA030: struct.pack("<i28xi", 100_499_612, 4_906_289),
            })
        if layout.club_loans_offset is not None:
            self.byte_values.update({
                0x4000 + layout.club_loans_offset: struct.pack("<QQQ", 0x7800, 0x7808, 0x7808),
                0x7800: struct.pack("<Q", 0x8000),
                0x8000: struct.pack(
                    "<IIIIII4B", 218_212_390, 1_144_288, 416_914, 0,
                    0, 0, 1, 1, 0, 0,
                ),
            })
        if layout.club_sponsors_root_offset is not None:
            self.pointers[0x2000 + layout.club_sponsors_root_offset] = 0x9000
            self.byte_values.update({
                0x9000: struct.pack("<QQQ", 0x9800, 0x9808, 0x9808),
                0x9800: struct.pack("<Q", 0x9900),
                0x9900: struct.pack(
                    "<IIII2x3B3x", 0, 0, 900_000_000, 0x055D4A80, 22, 0, 0,
                ),
            })
        if layout.club_vision_culture_offset is not None:
            self.pointers.update({
                0x1000 + layout.team_details_offset: 0xB000,
                0xB000 + layout.team_details_club_vision_offset: 0xB100,
                0xB100 + layout.club_vision_culture_offset: 0xB200,
                0xB200 + 0x30: 0x2000,
            })
            culture = bytearray(layout.club_culture_record_size)
            struct.pack_into("<h", culture, 0x08, -1)
            struct.pack_into("<b", culture, 0x0B, -1)
            struct.pack_into("<i", culture, 0x28, -1)
            culture[0x2C] = 155
            culture[0x2D] = 5
            culture[0x30] = 2
            struct.pack_into("<I", culture, 0x3C, 0x200)
            self.byte_values.update({
                0xB200: struct.pack("<QQQ", 0xB300, 0xB308, 0xB308),
                0xB300: struct.pack("<Q", 0xB400),
                0xB400: bytes(culture),
            })

    def ptr(self, address):
        return self.pointers.get(address, 0)

    def u32(self, address):
        return self.u32_values.get(address)

    def u8(self, address):
        return self.u8_values.get(address)

    def u16(self, address):
        return {0x4000 + 0xD8: 1905, 0x4000 + 0xD0: 1905}.get(address)

    def bytes(self, address, size):
        value = self.byte_values.get(address)
        return value if value is not None and len(value) == size else None

    def team(self, _address):
        return {"name": "Test Club"}

    def fm_string_at(self, address):
        return {
            0x5000 + 0x38: "Main Ground", 0x6000 + 0x38: "Training Centre",
            0x5000 + 0x40: "Main Ground", 0x6000 + 0x40: "Training Centre",
        }.get(address)


def test_club_information_reads_sugar_daddy_once_for_a_consistent_snapshot():
    reader = FakeReader(FM24_LAYOUT)
    address = 0x7000 + FM24_LAYOUT.finance_sugar_daddy_offset
    original_u8 = reader.u8
    reads = 0

    def changing_u8(target):
        nonlocal reads
        if target == address:
            reads += 1
            return 2 if reads == 1 else 0
        return original_u8(target)

    reader.u8 = changing_u8
    result = _club_information(reader, 0x1000)

    assert reads == 1
    assert result["finances"]["sugar_daddy"] == 2
    assert result["finances"]["sugar_daddy_name"] == "背景"


def test_club_information_reads_fmrte_mapped_fields_for_fm24_and_fm26():
    for layout in (FM24_LAYOUT, FM26_LAYOUT):
        result = _club_information(FakeReader(layout), 0x1000)
        assert result["year_founded"] == 1905
        assert result["facilities"] == {
            "training": 18,
            "youth": 16,
            "junior_coaching": 17,
            "youth_recruitment": 15,
        }
        assert result["attendance"] == {"average": 32100, "minimum": 28000, "maximum": 38000}
        assert result["supporters"] == {
            "season_ticket_holders": 12000,
            "social_media_followers": 550000,
            "profile": {
                "loyalty": 12, "passion": 19, "patience": 13,
                "affluence": 10, "temperament": 17, "expectations": 10,
            },
            "distribution": {
                "hardcore": 11, "core": 30, "family": 28,
                "fair_weather": 10, "corporate": 3, "casual": 18,
            },
        }
        assert result["ownership"]["address"] == hex(0x4000 + layout.club_ownership_offset)
        assert result["ownership"]["type"] == 3
        assert result["stadium"]["name"] == "Main Ground"
        assert result["stadium"]["capacity"] == 40000
        assert result["stadium"]["expansion_capacity"] == 52000
        assert result["stadium"]["pitch_type_name"] == "人造草皮（软）"
        assert result["stadium"]["state_name"] == "良好"
        assert result["stadium"]["extinct"] is False
        assert result["stadium"]["national_team_use_name"] == "重大比赛"
        assert result["finances"]["balance"] == -20_134_098
        assert result["finances"]["remaining_transfer_budget"] == 29_645_734
        assert result["finances"]["season_transfer_budget"] == 31_766_152
        assert result["finances"]["wage_budget"] == 4_414_338
        assert result["finances"]["wage_used"] == 4_715_251
        assert result["finances"]["max_wage"] == 595_935
        assert result["finances"]["average_match_ticket_price"] == 40.0
        assert result["finances"]["transfer_revenue_percentage"] == 25
        if layout.finance_sugar_daddy_offset is not None:
            assert result["finances"]["sugar_daddy"] == 2
            assert result["finances"]["sugar_daddy_name"] == "背景"
        assert result["finances"]["income_statement"]["income"][0] == {
            "key": "gate_receipts", "name": "门票收入",
            "this_month": 1_496_430, "last_month": 1_496_431,
            "this_season": 1_496_432, "last_season": 1_496_433,
        }
        assert result["finances"]["income_statement"]["expenditure"][1] == {
            "key": "loan_repayments_and_interest", "name": "贷款还款及利息",
            "this_month": 1_225_707, "last_month": 1_225_708,
            "this_season": 1_225_709, "last_season": 1_225_710,
        }
        assert result["finances"]["monthly_summary"] == [
            {"month": None, "balance": -131_890_427, "profit": 227_483_750},
            {"month": None, "balance": 100_499_612, "profit": 4_906_289},
        ]
        assert result["finances"]["debts"] == [{
            "address": "0x8000",
            "original_debt": 218_212_390,
            "monthly_repayment": 1_144_288,
            "monthly_interest_repayment": 416_914,
            "conditional_repayment": 0,
            "end_date": None,
            "start_date": None,
            "source": 1,
            "source_name": "银行",
            "include_in_fpp": True,
            "interest_only": False,
        }]
        if layout is FM24_LAYOUT:
            assert result["finances"]["sponsors"] == [{
                "address": "0x9900",
                "type": 22,
                "type_name": "球衣制造商",
                "total_value": 900_000_000,
                "start_date": None,
                "end_date": None,
                "renew_income": False,
                "fixed_value": False,
            }]
            assert result["culture"] == [{
                "address": "0xb400", "type": 155, "type_name": "踢赏心悦目的足球",
                "source_type": 2, "source_name": "球迷", "importance": 5,
                "value": None, "value_raw": -1, "start_date": None, "end_date": None,
                "reference_id": None, "reference_type": -1,
                "reference_type_name": None, "reference_name": None,
                "reference_unknown_byte": 0, "for_competition": False,
                "flags_raw": 0x200,
            }]
        assert result["training_ground"]["name"] == "Training Centre"


def test_club_information_skips_inactive_zero_importance_culture_records():
    reader = FakeReader(FM24_EPIC_LAYOUT)
    inactive = bytearray(FM24_EPIC_LAYOUT.club_culture_record_size)
    struct.pack_into("<h", inactive, 0x08, 261)
    struct.pack_into("<b", inactive, 0x0B, 25)
    struct.pack_into("<i", inactive, 0x28, 7)
    inactive[0x2C] = 16
    inactive[0x2D] = 0
    inactive[0x30] = 1
    reader.byte_values.update({
        0xB200: struct.pack("<QQQ", 0xB300, 0xB310, 0xB310),
        0xB300: struct.pack("<QQ", 0xB500, 0xB400),
        0xB500: bytes(inactive),
    })

    result = _club_information(reader, 0x1000)

    assert result["culture"] is not None
    assert [row["address"] for row in result["culture"]] == ["0xb400"]


def test_managed_club_overview_frontend_entry_is_removed():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert 'data-club-tab="overview"' not in html
    assert "function renderClubOverview" not in script
    assert "function renderManagedClubOverview" not in script
    assert 'app.clubTab === "overview"' not in script
    assert 'data-club-tab="players"' in html
    assert "function clubIncomeStatementValue" in script
    assert "function renderClubIncomeReport" in script
    assert "收入来源报表" in script
    assert "本赛季占比" in script
    assert 'row.name || row.key || "未知收入"' in script
    assert '<details class="club-income-report club-income-report-collapsible">' in script
    assert "club-income-report-collapse-label" in script


def test_all_recognized_builds_expose_core_club_profile_offsets():
    for layout in (FM24_240_LAYOUT, FM24_241_LAYOUT):
        assert layout.club_detail_offset is not None
        assert layout.club_detail2_offset is not None
        assert layout.club_training_ground_offset is not None
        assert (
            layout.finance_income_statement_period_offsets
            == FM24_LAYOUT.finance_income_statement_period_offsets
        )
        assert layout.finance_monthly_summary_offset is None
        assert layout.club_vision_culture_offset is not None

    for layout in (FM24_EPIC_LAYOUT, FM24_XGP_LAYOUT):
        assert layout.club_detail_offset is not None
        assert layout.club_detail2_offset is not None
        assert layout.club_training_ground_offset is not None
        assert layout.finance_income_statement_period_offsets is not None
        assert layout.finance_monthly_summary_offset is not None
        assert layout.club_vision_culture_offset is not None
    for layout in (FM26_XGP_TEMPLATE,):
        assert layout.club_detail_offset is not None
        assert layout.club_detail2_offset is not None
        assert layout.club_training_ground_offset is not None
        assert layout.finance_income_statement_period_offsets is not None
        assert layout.finance_monthly_summary_offset is not None
        assert layout.club_vision_culture_offset is not None
    assert FM26_LAYOUT.club_vision_culture_offset is not None


def test_team_reputation_offset_is_shared_by_fm_generation():
    for layout in (
        FM24_LAYOUT, FM24_240_LAYOUT, FM24_241_LAYOUT,
        FM24_EPIC_LAYOUT, FM24_XGP_LAYOUT,
        FM26_LAYOUT, FM26_XGP_TEMPLATE,
    ):
        assert layout.team_reputation_offset == 0xA8
