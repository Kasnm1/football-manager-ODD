from __future__ import annotations

import json
import threading
import math
import random
import uuid
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from tools.account_store import load_document, save_document, update_document
from tools.app_paths import save_data_root
from tools.app_settings import load_settings, local_purchase_price
from tools.betting_account import (
    available_balance, commit_documents_with_wallet_adjustments,
    grant_bankruptcy_relief as grant_wallet_bankruptcy_relief,
    load_wallet,
)
from tools.money import (
    from_minor, migrate_money_fields, migrate_money_records, multiply_minor, read_minor, round_money,
    to_minor, write_minor,
)


ENERGY_DRINK_PRICE = 20_000.0
TEAM_ENERGY_DRINK_PRICE = ENERGY_DRINK_PRICE * 20
CLUB_DIVIDEND_RATE = 0.05
SALARY_TAX_RATE = 0.30
SALARY_NET_RATE = 0.70
SALARY_NET_RATE_DECIMAL = "0.70"
SALARY_NET_MONTHLY_MULTIPLIER = "3.033333333333333333333333333333333333"
PLAYER_ACTIVITIES = {
    "drinks": {"name": "请球员喝酒", "price": 0.0, "intimacy": 0.2, "centre": "entertainment"},
    "hot_spring": {"name": "和球员泡温泉", "price": 0.0, "intimacy": 0.4, "centre": "entertainment"},
    "football_game": {"name": "玩电子足球游戏", "price": 0.0, "intimacy": 0.2, "centre": "entertainment"},
    "massage": {"name": "请球员按摩", "price": 0.0, "intimacy": 1.0, "centre": "entertainment"},
    "media_interview": {"name": "球员媒体采访", "price": 0.0, "intimacy": 0.0, "centre": "media"},
}
ACTIVITY_CENTRES = {
    "entertainment": {"name": "娱乐中心", "floor": "1F", "price": 2_000_000.0, "available": True},
    "talk_room": {"name": "人员谈话室", "floor": "2F", "price": 2_000_000.0, "available": True},
    "media": {
        "name": "媒体中心", "floor": "3F", "price": 0.0,
        "available": False, "unavailable_reason": "暂未开启",
    },
    "international": {
        "name": "国际事务中心", "floor": "4F", "price": 10_000_000.0,
        "available": True,
    },
    "intelligence": {
        "name": "情报中心", "floor": "5F", "price": 50_000_000.0,
        "available": True,
    },
}
DEFAULT_ACTIVITY_CENTRES: tuple[str, ...] = ()
ACTIVITY_CENTRE_RULES_VERSION = 2
ACTIVITY_FLOOR_COOLDOWN_DAYS = 2
_DEVELOPMENT_ACTIVITY_UNLOCKS = False


def configure_activity_centre_development_unlocks(enabled: bool) -> None:
    """Expose every activity floor in source runs without changing saved ownership."""
    global _DEVELOPMENT_ACTIVITY_UNLOCKS
    _DEVELOPMENT_ACTIVITY_UNLOCKS = bool(enabled)


def _activity_centre_available(option: dict[str, Any]) -> bool:
    return _DEVELOPMENT_ACTIVITY_UNLOCKS or bool(option.get("available"))


def _activity_centre_owned(payload: dict[str, Any], centre: str) -> bool:
    return _DEVELOPMENT_ACTIVITY_UNLOCKS or str(centre) in (
        payload.get("owned_activity_centres") or DEFAULT_ACTIVITY_CENTRES
    )


def _activity_facility_owned(payload: dict[str, Any], facility: str) -> bool:
    option = ACTIVITY_FACILITIES.get(str(facility or ""))
    if not option or not _activity_centre_owned(payload, str(option["centre"])):
        return False
    return (
        _DEVELOPMENT_ACTIVITY_UNLOCKS
        or bool(option.get("included_with_centre"))
        or str(facility) in set(payload.get("owned_activity_facilities") or [])
    )


NATIONALITY_PROCESSING_PRICES = {
    "club": 1_000_000.0,
    "world": 100_000_000.0,
}
LANGUAGE_LEVEL_DAYS = (7, 10, 15, 20, 25, 30, 35, 43, 52, 63)
LANGUAGE_INSTANT_MAX_PRICE = 200_000.0
ACTIVITY_FACILITIES = {
    "drinks": {
        "name": "社交吧台", "centre": "entertainment", "price": 0.0,
        "included_with_centre": True,
    },
    "hot_spring": {"name": "恢复温泉", "centre": "entertainment", "price": 200_000.0},
    "football_game": {"name": "电竞室", "centre": "entertainment", "price": 200_000.0},
    "massage": {"name": "按摩室", "centre": "entertainment", "price": 500_000.0},
    "psychological_counseling": {
        "name": "心理辅导室", "centre": "talk_room", "price": 0.0,
        "included_with_centre": True,
    },
    "retirement": {"name": "退役计划交流", "centre": "talk_room", "price": 200_000.0},
    "black_room": {"name": "小黑屋", "centre": "talk_room", "price": 1_000_000.0},
    "language_classroom": {"name": "语言教室", "centre": "talk_room", "price": 1_000_000.0},
    "departure_mediation": {"name": "劝离室", "centre": "talk_room", "price": 1_000_000.0},
    "naturalization": {
        "name": "国籍处理中心", "centre": "international", "price": 0.0,
        "included_with_centre": True,
    },
    "match_intelligence": {
        "name": "线人网络", "centre": "intelligence", "price": 0.0,
        "included_with_centre": True,
    },
    "salary_committee_room": {
        "name": "薪酬谈判室", "centre": "talk_room", "price": 0.0,
        "included_with_centre": True,
    },
}
MATCH_INTELLIGENCE_TIERS = {
    "outcome": {
        "name": "胜负情报", "basis_points": 4_000,
        "random_basis_points": 3_500,
        "description": "今日金额40%",
        "random_description": "今日金额35%",
    },
    "total_goals": {
        "name": "总进球数情报", "basis_points": 4_000,
        "random_basis_points": 3_500,
        "description": "今日金额40%",
        "random_description": "今日金额35%",
    },
    "score": {
        "name": "比分情报", "basis_points": 8_800,
        "random_basis_points": 8_500,
        "description": "今日锁价基准88% · 并包含前两项",
        "random_description": "今日锁价基准85% · 并包含前两项",
    },
}
MATCH_INTELLIGENCE_TIER_ORDER = tuple(MATCH_INTELLIGENCE_TIERS)
MEDIA_ACTIVITIES = {
    "club_press_conference": {"name": "俱乐部新闻发布会", "reputation_delta": 50},
    "player_feature": {"name": "球员专题报道", "reputation_delta": 50},
}
PSYCHOLOGICAL_COUNSELING_OUTCOMES = {
    "unlocked": {
        "name": "付费解锁", "probability": 0.0, "intimacy": 0.0,
        "dialogues": (
            "专业团队已经处理完这次困扰，我现在愿意放下不满，重新专注于球队。",
        ),
    },
    "relieved": {
        "name": "彻底释怀", "probability": 0.40, "intimacy": 1.0,
        "dialogues": (
            "谢谢你愿意认真听我说完。压在心里的东西终于放下了，我愿意重新相信你，也愿意为球队再拼一次。",
            "我原以为这件事永远都过不去了。今天谈完，我终于觉得自己又能抬起头往前走了。",
            "这些话我憋了很久。你没有回避，也没有敷衍我，这对我很重要。过去的事，就让它过去吧。",
            "我现在明白，你并不是不在乎我的感受。谢谢你来找我，我会把心重新放回球队。",
            "能把一切说开，比我想象中更让人轻松。教练，我愿意重新和你站在一起。",
        ),
    },
    "opened_up": {
        "name": "打开心结", "probability": 0.25, "intimacy": 0.5,
        "dialogues": (
            "我明白你的意思了。虽然还需要一点时间，但这件事就到这里吧。",
            "谈过以后，我确实想通了一些。接下来我会把精力放回比赛。",
            "我不敢说自己已经完全释怀，但我愿意不再追究。",
            "至少现在，我们彼此的想法都说清楚了。我会继续做好自己的工作。",
            "这次交流有些帮助。我愿意向前看，不再让这件事影响球队。",
        ),
    },
    "no_effect": {
        "name": "毫无效果", "probability": 0.30, "intimacy": 0.0,
        "dialogues": (
            "我听到了，但这些话并没有改变我的感受。现在我还不想继续谈。",
            "也许你是认真来解决问题的，可我暂时没办法接受这个解释。",
            "我知道你想让我放下，可事情没有那么简单。请再给我一些空间。",
            "该说的我们都说了，但问题还在那里。我现在无法假装一切已经过去。",
            "这次谈话没有让我好受一些。至少今天，我们先到这里吧。",
        ),
    },
    "rupture": {
        "name": "谈话破裂", "probability": 0.05, "intimacy": -1.0,
        "dialogues": (
            "够了。我本来还愿意相信你，现在看来，那只是我一厢情愿。",
            "你不是来听我说话的，你只是想让我变得听话。我们之间没什么可谈的了。",
            "我已经把真实想法告诉你了，换来的却还是敷衍。从现在开始，我不会再轻易相信你。",
            "这次谈话让我彻底看清了自己的处境。问题没有解决，反而比以前更糟。",
            "我会履行合同，但我们之间的关系已经不可能像以前一样了。",
            "今天的谈话到此为止。下一次见面，我们可能就不是在讨论如何修复关系了。",
        ),
    },
}
PSYCHOLOGICAL_COUNSELING_UNLOCK_PRICE = 100_000.0
MEDICAL_TREATMENTS = {
    "conservative": {"name": "保守治疗", "factor": 1.5, "daily_cost": 10_000.0},
    "aggressive": {"name": "激进治疗", "factor": 3.0, "daily_cost": 30_000.0},
}
MEDICAL_DEFAULT_TREATMENTS = {"manual", "none", *MEDICAL_TREATMENTS}
MEDICAL_TREATMENT_PRICING_VERSION = 2
_LOCK = threading.RLock()

PRODUCTS: dict[str, dict[str, Any]] = {
    "interview_perfume": {
        "name": "伯乐香水", "price": 5_000_000.0, "category": "职场",
        "family": "career", "duration": "policy", "tier": "仙品",
        "description": "使用后15个游戏日内，当前经理应聘俱乐部职位时必定获得面试机会。",
    },
    "red_bull": {"name": "功能饮料", "price": ENERGY_DRINK_PRICE, "category": "比赛", "family": "match", "duration": "fixture", "description": "使指定球员的体能在指定比赛中永远充沛。"},
    "team_red_bull": {"name": "全队功能饮料", "price": TEAM_ENERGY_DRINK_PRICE, "category": "比赛", "family": "match", "tier": "中品", "duration": "fixture", "description": "使全队球员的体能在指定比赛中永远充沛。"},
    "referee": {"name": "黑哨（1级）", "price": 50_000.0, "category": "比赛", "family": "match", "duration": "fixture", "description": "指定比赛中，裁判无视本队任何犯规行为，对手犯规正常判罚。"},
    "referee_level2": {"name": "黑哨（2级）", "price": 500_000.0, "category": "比赛", "family": "match", "duration": "fixture", "description": "指定比赛中，裁判无视本队任何犯规行为，对手犯规从重处罚。"},
    "referee_level3": {"name": "黑哨（3级）", "price": 5_000_000.0, "category": "比赛", "family": "match", "duration": "fixture", "description": "指定比赛中，裁判无视本队任何犯规行为，对手犯规直接红牌。"},
    "fake_marrow_pill": {"name": "洗髓丹（赝品）", "price": 20_000.0, "category": "球员", "family": "reallocation", "tier": "赝品", "duration": "instant", "description": "使指定球员的CA减半，其他属性及PA不变。"},
    "martial_manual_fragment": {"name": "洗髓丹（凡品）", "price": 20_000.0, "category": "球员", "family": "reallocation", "tier": "凡品", "attribute_limit": 12, "duration": "instant", "description": "允许指定球员在不改变 CA/PA 的情况下，重新分配最多3点原值低于12的可见属性。"},
    "martial_manual_mid": {"name": "洗髓丹（中品）", "price": 200_000.0, "category": "球员", "family": "reallocation", "tier": "中品", "attribute_limit": 15, "duration": "instant", "description": "允许指定球员在不改变 CA/PA 的情况下，重新分配最多3点原值低于15的可见属性。"},
    "martial_manual_high": {"name": "洗髓丹（上品）", "price": 2_000_000.0, "category": "球员", "family": "reallocation", "tier": "上品", "attribute_limit": 18, "duration": "instant", "description": "允许指定球员在不改变 CA/PA 的情况下，重新分配最多3点原值低于18的可见属性。"},
    "martial_manual_immortal": {"name": "洗髓丹（仙品）", "price": 10_000_000.0, "category": "球员", "family": "reallocation", "tier": "仙品", "attribute_limit": 20, "duration": "instant", "description": "允许指定球员在不改变 CA/PA 的情况下，重新分配最多3点原值低于20的可见属性。"},
    "enlightenment": {"name": "培元丹", "price": 5_000_000.0, "category": "球员", "family": "growth", "tier": "仙品", "duration": "instant", "description": "仅可对 CA 低于 PA 的球员使用，使 CA 提升1点并自行指定强化的一项可见属性，PA不变。"},
    "rejuvenation_pill": {"name": "回春丹", "price": 1_000_000.0, "category": "球员", "family": "growth", "tier": "中品", "duration": "instant", "description": "清除指定球员当前的全部伤病。"},
    "age_reversal_pill": {"name": "返老还童丹", "price": 88_000_000.0, "category": "球员", "family": "growth", "tier": "神品", "duration": "instant", "description": "指定球员返老还童，CA降低15，其他属性及PA不变，并清除已有退役计划。"},
    "opponent_flu": {"name": "病毒包裹", "price": 500_000.0, "category": "比赛", "family": "match", "tier": "上品", "duration": "fixture", "description": "使指定比赛的一名对手一线队球员感染病毒3天。"},
    "random_enlightenment": {"name": "不稳定培元丹", "price": 500_000.0, "category": "球员", "family": "growth", "tier": "上品", "duration": "instant", "description": "仅可对 CA 低于 PA 的球员使用，使 CA 提升1点并随机强化一项可见属性，PA不变。"},
    "extremely_unstable_enlightenment": {"name": "极不稳定培元丹", "price": 500_000.0, "category": "球员", "family": "growth", "tier": "凡品", "duration": "instant", "description": "仅可对 CA 低于 PA 的球员使用，使 CA 随机提升1点或降低1点，并随机强化或削弱一项可见属性，PA不变。"},
    "marrow_cleansing_basic": {"name": "破境丹（凡品）", "price": 50_000.0, "category": "球员", "family": "breakthrough", "tier": "凡品", "pa_limit": 130, "duration": "instant", "description": "仅可对 PA 低于130的球员使用，使 CA和PA各提升1点。"},
    "marrow_cleansing_mid": {"name": "破境丹（中品）", "price": 500_000.0, "category": "球员", "family": "breakthrough", "tier": "中品", "pa_limit": 145, "duration": "instant", "description": "仅可对 PA 低于145的球员使用，使 CA和PA各提升1点。"},
    "marrow_cleansing_high": {"name": "破境丹（上品）", "price": 5_000_000.0, "category": "球员", "family": "breakthrough", "tier": "上品", "pa_limit": 160, "duration": "instant", "description": "仅可对 PA 低于160的球员使用，使 CA和PA各提升1点。"},
    "marrow_cleansing_immortal": {"name": "破境丹（仙品）", "price": 50_000_000.0, "category": "球员", "family": "breakthrough", "tier": "仙品", "pa_limit": 180, "duration": "instant", "description": "仅可对 PA 低于180的球员使用，使 CA和PA各提升1点。"},
    "marrow_cleansing_divine": {"name": "破境丹（神品）", "price": 500_000_000.0, "category": "球员", "family": "breakthrough", "tier": "神品", "pa_limit": 200, "duration": "instant", "description": "仅可对 PA 低于200的球员使用，使 CA和PA各提升1点。"},
    "latent_dragon_basic": {"name": "潜龙丹（凡品）", "price": 10_000.0, "category": "球员", "family": "latent", "tier": "凡品", "pa_limit": 130, "duration": "instant", "description": "仅可对 PA 低于130的球员使用，使 PA 提升1点，CA和可见属性不变。"},
    "latent_dragon_mid": {"name": "潜龙丹（中品）", "price": 100_000.0, "category": "球员", "family": "latent", "tier": "中品", "pa_limit": 145, "duration": "instant", "description": "仅可对 PA 低于145的球员使用，使 PA 提升1点，CA和可见属性不变。"},
    "latent_dragon_high": {"name": "潜龙丹（上品）", "price": 1_000_000.0, "category": "球员", "family": "latent", "tier": "上品", "pa_limit": 160, "duration": "instant", "description": "仅可对 PA 低于160的球员使用，使 PA 提升1点，CA和可见属性不变。"},
    "latent_dragon_immortal": {"name": "潜龙丹（仙品）", "price": 10_000_000.0, "category": "球员", "family": "latent", "tier": "仙品", "pa_limit": 180, "duration": "instant", "description": "仅可对 PA 低于180的球员使用，使 PA 提升1点，CA和可见属性不变。"},
    "latent_dragon_divine": {"name": "潜龙丹（神品）", "price": 100_000_000.0, "category": "球员", "family": "latent", "tier": "神品", "pa_limit": 200, "duration": "instant", "description": "仅可对 PA 低于200的球员使用，使 PA 提升1点，CA和可见属性不变。"},
    "bribed_goalkeeper": {
        "name": "买通对方门将", "price": 1_000_000.0, "category": "比赛",
        "family": "match", "tier": "凡品", "duration": "fixture",
        "description": "指定比赛中，对面门将是你的内鬼。",
    },
    "doping": {
        "name": "兴奋剂", "price": 200_000.0, "category": "比赛",
        "family": "match", "tier": "仙品", "duration": "fixture",
        "description": "使指定球员的属性在指定比赛中最大。",
    },
    "team_doping": {
        "name": "全体兴奋剂", "price": 4_000_000.0, "category": "比赛",
        "family": "match", "tier": "神品", "duration": "fixture",
        "description": "使全队一线队球员的属性在指定比赛中最大。",
    },
    "club_brochure": {
        "name": "俱乐部宣传册", "price": 20_000_000.0, "category": "声望",
        "family": "reputation", "duration": "instant",
        "description": "使指定的执教俱乐部或已购买俱乐部声望提升200点。",
    },
    "player_brochure": {
        "name": "球员宣传册", "price": 20_000_000.0, "category": "声望",
        "family": "reputation", "duration": "instant",
        "description": "使执教俱乐部的指定一线队球员世界声望提升100点。",
    },
    "league_brochure": {
        "name": "联赛宣传册", "price": 40_000_000.0, "category": "声望",
        "family": "reputation", "duration": "instant",
        "description": "使指定联赛的原生声望提升10点。",
    },
    "league_point_plus_one": {
        "name": "联赛积分 +1", "price": 20_000_000.0, "category": "积分",
        "family": "league_points", "duration": "instant",
        "description": "为当前玩家执教俱乐部增加1点联赛积分，可批量使用。",
    },
}

# Retired product definitions remain available so existing inventory stays usable.
SHOP_SKUS = (
    "interview_perfume",
    "red_bull", "team_red_bull", "doping", "team_doping", "bribed_goalkeeper", "opponent_flu",
    "referee", "referee_level2", "referee_level3",
    "fake_marrow_pill", "martial_manual_fragment", "martial_manual_mid", "martial_manual_high", "martial_manual_immortal",
    "extremely_unstable_enlightenment", "enlightenment", "rejuvenation_pill", "age_reversal_pill",
    "marrow_cleansing_basic", "marrow_cleansing_mid", "marrow_cleansing_high",
    "marrow_cleansing_immortal", "marrow_cleansing_divine",
    "latent_dragon_basic", "latent_dragon_mid", "latent_dragon_high",
    "latent_dragon_immortal", "latent_dragon_divine",
    "player_brochure", "league_brochure", "league_point_plus_one",
)


def economy_path() -> Path:
    return save_data_root() / "economy" / "club_economy.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


TRANSACTION_LABELS = {
    "manager_salary": "工资收入", "club_dividend": "俱乐部分红",
    "bank_to_casino": "银行转入钱包", "casino_to_bank": "钱包转回银行",
    "credit_borrowed": "贷款到账", "credit_repaid": "贷款还款",
    "credit_defaulted": "贷款逾期扣款", "credit_repaid_at_due": "贷款到期还款",
    "credit_cheat_cleared": "清除贷款", "shop_purchase": "商店购买",
    "canteen_plan_changed": "食堂方案调整", "salary_schedule_changed": "工资发放设置",
    "match_intelligence_purchase": "比赛情报获取",
    "item_destroyed": "道具销毁", "instant_item_used": "道具立即使用",
    "item_use_cancelled": "取消道具使用", "item_scheduled": "安排球员道具",
    "team_item_scheduled": "安排团队道具", "fixture_item_scheduled": "安排比赛道具",
    "item_activated": "道具启用", "item_consumed": "道具消耗",
    "bet_placed": "投注扣款", "bet_settlement": "投注结算",
    "manual_bet_refund": "投注退款", "bet_schedule_refund": "赛程变更退款",
    "premature_settlement_reversal": "结算冲正",
    "premature_cancellation_reversal": "取消结算冲正",
    "version_update_reward": "版本更新奖励", "funding": "钱包资金调整",
    "cheat_funding": "开发资金调整", "bankruptcy_relief": "破产救济",
    "invalid_clock_rollback_refund": "异常日期回滚退款",
}

LEDGER_CATEGORY_LABELS = {
    "income": "收入",
    "betting": "投注",
    "transfer": "转账",
    "credit": "贷款",
    "shop": "商店与道具",
    "club": "俱乐部",
    "activity": "活动与训练",
    "youth": "青训",
    "refund": "退款与冲正",
    "other": "其他",
}
LEDGER_ACCOUNT_LABELS = {
    "bank": "银行",
    "wallet": "钱包",
    "combined": "银行 + 钱包",
    "transfer": "内部转账",
}
_INTERNAL_TRANSFER_TYPES = {
    "bank_to_casino", "casino_to_bank", "bank_recharge", "casino_withdrawal",
}
_MIRRORED_WALLET_TYPE_ALIASES = {
    "credit_repaid": {"credit_repayment"},
}


def transaction_label(kind: Any, details: dict[str, Any] | None = None) -> str:
    key = str(kind or "")
    if key in TRANSACTION_LABELS:
        return TRANSACTION_LABELS[key]
    if key.startswith("bet_") or "betting" in key:
        return "投注结算"
    if key.startswith("refund"):
        return "投注退款"
    if "transfer_budget" in key:
        return "转会预算操作"
    if "club_balance" in key:
        return "俱乐部结余操作"
    if "facility" in key or "stadium" in key:
        return "俱乐部设施操作"
    if "youth" in key or "academy" in key:
        return "青训投资"
    if "activity" in key or "language" in key:
        return "活动中心支出"
    if "loan" in key or "credit" in key:
        return "贷款操作"
    if "item" in key or "pill" in key:
        return "道具操作"
    if "salary" in key or "wage" in key:
        return "工资操作"
    amount = read_minor(details or {}, "amount") if details else 0
    return "银行收入" if amount > 0 else "银行支出"


def transaction_category(kind: Any) -> str:
    key = str(kind or "").lower()
    if key in _INTERNAL_TRANSFER_TYPES:
        return "transfer"
    if key in {"manager_salary", "club_dividend", "version_update_reward"}:
        return "income"
    if key.startswith("bet_") or "betting" in key or "bet_" in key:
        return "betting"
    if "refund" in key or "reversal" in key or "rollback" in key:
        return "refund"
    if "credit" in key or "loan" in key or "bankruptcy" in key:
        return "credit"
    if any(token in key for token in ("shop", "item", "pill")):
        return "shop"
    if any(token in key for token in (
        "club", "stadium", "facility", "transfer_budget", "player_transfer",
        "player_loan", "dividend",
    )):
        return "club"
    if any(token in key for token in (
        "activity", "language", "canteen", "medical", "salary_schedule",
        "training", "intelligence",
    )):
        return "activity"
    if "youth" in key or "academy" in key:
        return "youth"
    return "other"


def _ledger_description(row: dict[str, Any]) -> str:
    for key in ("team_name", "club_name", "player_name", "item_name", "name"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    sku = str(row.get("sku") or "").strip()
    if sku and sku in PRODUCTS:
        return str(PRODUCTS[sku].get("name") or sku)
    bet_ids = row.get("bet_ids")
    if isinstance(bet_ids, list) and bet_ids:
        return f"{len(bet_ids)} 笔注单"
    return ""


def _ledger_entry(row: dict[str, Any], account: str) -> dict[str, Any] | None:
    amount_minor = read_minor(row, "amount")
    if not amount_minor:
        return None
    kind = str(row.get("type") or "account_adjustment")
    internal = kind in _INTERNAL_TRANSFER_TYPES
    if account == "bank" and internal:
        account = "transfer"
    elif account == "bank" and any(
        read_minor(row, field)
        for field in (
            "wallet_used", "wallet_refund", "wallet_credit", "wallet_deducted",
        )
    ):
        account = "combined"
    raw_date = str(row.get("game_date") or row.get("at") or "")
    date_text = raw_date[:10]
    try:
        month = date.fromisoformat(date_text).strftime("%Y-%m")
    except ValueError:
        month = date.today().strftime("%Y-%m")
    balance_field = "balance_after" if account == "wallet" else "general_balance_after"
    balance_after = (
        from_minor(read_minor(row, balance_field))
        if balance_field in row or f"{balance_field}_minor" in row else None
    )
    category = transaction_category(kind)
    amount = from_minor(amount_minor)
    source_id = str(row.get("id") or "")
    stable_id = source_id or f"{kind}:{str(row.get('at') or raw_date)}:{amount_minor}"
    sku = str(row.get("sku") or "").strip()
    return {
        "id": f"{account}:{stable_id}",
        "source_id": str(row.get("id") or ""),
        "type": kind,
        "label": transaction_label(kind, row),
        "description": _ledger_description(row),
        "item_sku": sku if sku in PRODUCTS else "",
        "account": account,
        "account_label": LEDGER_ACCOUNT_LABELS[account],
        "category": category,
        "category_label": LEDGER_CATEGORY_LABELS[category],
        "direction": "transfer" if internal else ("income" if amount_minor > 0 else "expense"),
        "is_internal_transfer": internal,
        "amount": amount,
        "balance_after": balance_after,
        "occurred_at": str(row.get("at") or row.get("recorded_at") or raw_date),
        "date": date_text,
        "month": month,
    }


def _mirrored_wallet_types(bank_transactions: list[dict[str, Any]]) -> set[str]:
    """Find wallet legs already represented by a combined bank transaction."""
    kinds = {
        str(row.get("type") or "")
        for row in bank_transactions
        if isinstance(row, dict) and any(
            read_minor(row, field)
            for field in (
                "wallet_used", "wallet_refund", "wallet_credit",
                "wallet_deducted",
            )
        )
    }
    return kinds | {
        alias
        for kind in kinds
        for alias in _MIRRORED_WALLET_TYPE_ALIASES.get(kind, set())
    }


def _build_ledger_entries(
    bank_transactions: list[dict[str, Any]],
    wallet_transactions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    mirrored_wallet_types = _mirrored_wallet_types(bank_transactions)
    entries = [
        entry for row in bank_transactions if isinstance(row, dict)
        if (entry := _ledger_entry(row, "bank")) is not None
    ]
    for row in wallet_transactions or []:
        if not isinstance(row, dict) or str(row.get("type") or "") in {
            *_INTERNAL_TRANSFER_TYPES, *mirrored_wallet_types,
        }:
            continue
        entry = _ledger_entry(row, "wallet")
        if entry is not None:
            entries.append(entry)
    entries.sort(
        key=lambda entry: (str(entry.get("occurred_at") or ""), str(entry["id"])),
        reverse=True,
    )
    return entries


def _ledger_summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    income_minor = expense_minor = transfer_minor = 0
    by_account: dict[str, dict[str, int]] = {}
    for entry in entries:
        amount_minor = to_minor(entry.get("amount") or 0)
        account = str(entry.get("account") or "bank")
        bucket = by_account.setdefault(account, {
            "income_minor": 0, "expense_minor": 0, "net_minor": 0, "count": 0,
        })
        bucket["count"] += 1
        if entry.get("is_internal_transfer"):
            transfer_minor += abs(amount_minor)
            continue
        income_minor += max(0, amount_minor)
        expense_minor += max(0, -amount_minor)
        bucket["income_minor"] += max(0, amount_minor)
        bucket["expense_minor"] += max(0, -amount_minor)
        bucket["net_minor"] += amount_minor
    return {
        "income": from_minor(income_minor),
        "expense": from_minor(expense_minor),
        "net": from_minor(income_minor - expense_minor),
        "transfers": from_minor(transfer_minor),
        "count": len(entries),
        "accounts": [
            {
                "account": account,
                "label": LEDGER_ACCOUNT_LABELS.get(account, account),
                "income": from_minor(values["income_minor"]),
                "expense": from_minor(values["expense_minor"]),
                "net": from_minor(values["net_minor"]),
                "count": values["count"],
            }
            for account, values in sorted(by_account.items())
        ],
    }


def _money(value: float) -> float:
    return round_money(value)


def _shop_price_minor(base_price: float, settings: dict[str, Any] | None = None) -> int:
    settings = load_settings() if settings is None else settings
    return to_minor(local_purchase_price(base_price, settings))


def _shop_price(base_price: float, settings: dict[str, Any] | None = None) -> float:
    return from_minor(_shop_price_minor(base_price, settings))


def _pricing_settings_cache_key(settings: dict[str, Any]) -> str:
    pricing_settings = {
        key: settings.get(key)
        for key in ("money_currency", "money_rate", "purchase_money_scale")
    }
    return json.dumps(
        pricing_settings, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    )


@lru_cache(maxsize=16)
def _shop_catalog_cached(pricing_settings_json: str) -> tuple[tuple[str, dict[str, Any]], ...]:
    """Cache the immutable product projection for one complete pricing context."""
    settings = json.loads(pricing_settings_json)
    return tuple(
        (
            sku,
            {
                **PRODUCTS[sku],
                "price": _shop_price(PRODUCTS[sku]["price"], settings),
            },
        )
        for sku in SHOP_SKUS
    )


def _shop_catalog(settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    settings = load_settings() if settings is None else settings
    return [
        {"sku": sku, **projection}
        for sku, projection in _shop_catalog_cached(
            _pricing_settings_cache_key(settings),
        )
    ]


@lru_cache(maxsize=16)
def _welfare_pricing_cached(
    pricing_settings_json: str,
) -> tuple[
    tuple[tuple[str, float], ...],
    tuple[tuple[str, float], ...],
    tuple[tuple[str, float], ...],
    tuple[tuple[str, float], ...],
]:
    """Cache scalar activity price projections for one complete currency context."""
    settings = json.loads(pricing_settings_json)
    centre_prices = tuple(
        (
            key,
            local_purchase_price(
                local_purchase_price(value["price"], settings), settings,
            ),
        )
        for key, value in ACTIVITY_CENTRES.items()
    )
    instant_prices = tuple(
        (
            str(level),
            local_purchase_price(language_instant_level_price(level), settings),
        )
        for level in range(10)
    )
    instant_max_prices = tuple(
        (
            str(level),
            local_purchase_price(language_instant_max_price(level), settings),
        )
        for level in range(10)
    )
    nationality_processing_prices = tuple(
        (
            key,
            local_purchase_price(value, settings),
        )
        for key, value in NATIONALITY_PROCESSING_PRICES.items()
    )
    return (
        centre_prices,
        instant_prices,
        instant_max_prices,
        nationality_processing_prices,
    )


def _salary_accrual(value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("薪资累计金额必须为有限数值")
    return round(value + 1e-9, 6)


def _new_state() -> dict[str, Any]:
    return {
        "schema_version": 4,
        "general_balance": 0.0,
        "general_balance_minor": 0,
        "inventory": [],
        "match_item_history": [],
        "transactions": [],
        "club_dividend_baselines": {},
        "club_dividend_checked_through": {},
        "club_dividend_forecasts": {},
        "club_dividend_payments": [],
        "club_dividend_last_settlement_month": "",
        "salary_payments": [],
        "salary_schedule": "weekly",
        "salary_schedule_defaulted": True,
        "salary_checked_through": None,
        "salary_accrued": 0.0,
        "salary_accrual_through": None,
        "activity_progress": {},
        "activity_history": [],
        "activity_daily_usage": {},
        "owned_activity_centres": list(DEFAULT_ACTIVITY_CENTRES),
        "owned_activity_facilities": [],
        "activity_floor_cooldowns": {},
        "match_intelligence_purchases": {},
        "match_intelligence_price_locks": {},
        "activity_centre_rules_version": ACTIVITY_CENTRE_RULES_VERSION,
        "media_activity_daily_usage": {},
        "psychological_counseling": {},
        "language_learning": {"auto": {"enabled": False}, "plans": {}, "history": []},
        "canteen_plan": "basic",
        "free_services": False,
        "medical_default_treatment": "manual",
        "medical_treatments": {},
        "medical_treatment_pricing_version": MEDICAL_TREATMENT_PRICING_VERSION,
        "transfer_budget_withdrawals": [],
        "transfer_budget_rollbacks": [],
        "credit": {
            "principal": 0.0, "original_principal": 0.0, "interest": 0.0, "borrowed_on": None,
            "due_date": None, "last_accrual_date": None, "status": "inactive",
        },
        "default_notices": [],
    }


_ECONOMY_TRANSACTION_MONEY_FIELDS = (
    "amount", "general_balance_after", "bank_used", "wallet_used",
    "bank_overdraft", "bank_refund", "wallet_refund",
    "due_amount", "unpaid", "confiscation_requested", "confiscated",
    "standard_fine", "actual_fine", "bank_deducted", "wallet_deducted",
    "unrecovered",
)
_CREDIT_MONEY_FIELDS = ("principal", "original_principal", "interest")


def _bank_balance_minor(payload: dict[str, Any]) -> int:
    return read_minor(payload, "general_balance")


def _set_bank_balance_minor(payload: dict[str, Any], minor: int) -> None:
    write_minor(payload, "general_balance", minor)


def _credit_minor(credit: dict[str, Any], field: str) -> int:
    return read_minor(credit, field)


def _set_credit_minor(credit: dict[str, Any], field: str, minor: int) -> None:
    write_minor(credit, field, minor)


def _public_money_view(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _public_money_view(item)
            for key, item in value.items()
            if not str(key).endswith("_minor")
        }
    if isinstance(value, list):
        return [_public_money_view(item) for item in value]
    return value


def _normalize_economy_money(payload: dict[str, Any]) -> bool:
    changed = migrate_money_fields(payload, ("general_balance",))
    schema_version = max(4, int(payload.get("schema_version") or 1))
    changed = changed or payload.get("schema_version") != schema_version
    payload["schema_version"] = schema_version
    credit = payload.setdefault("credit", {})
    changed = migrate_money_fields(credit, _CREDIT_MONEY_FIELDS) or changed
    histories = (
        ("transactions", [], _ECONOMY_TRANSACTION_MONEY_FIELDS),
        ("salary_payments", [], ("gross_weekly", "net_amount")),
        ("club_dividend_forecasts", {}, ("estimated_amount",)),
        ("club_dividend_payments", [], ("amount",)),
        ("medical_treatments", {}, ("daily_cost", "charged_today")),
        ("match_intelligence_price_locks", {}, ("combined_funds",)),
        ("transfer_budget_withdrawals", [], ("amount", "bank_deducted", "wallet_deducted")),
        ("transfer_budget_rollbacks", [], ("amount", "bank_deducted", "wallet_deducted", "unrecovered")),
        ("inventory", [], ("price",)),
    )
    for key, default, fields in histories:
        changed = migrate_money_records(payload.setdefault(key, default), fields) or changed
    return changed


def _repair_invalid_clock_rollbacks(payload: dict[str, Any]) -> bool:
    """Refund clawbacks caused by the FM 1900-01-01 unreadable-clock sentinel."""
    changed = False
    withdrawals = {
        str(item.get("id") or ""): item
        for item in payload.get("transfer_budget_withdrawals", [])
    }
    for rollback in payload.get("transfer_budget_rollbacks", []):
        if (
            str(rollback.get("current_game_date") or "") != "1900-01-01"
            or rollback.get("invalid_clock_refunded_at")
        ):
            continue
        rollback_id = str(rollback.get("id") or "")
        bank_refund = _money(max(0.0, float(rollback.get("bank_deducted") or 0)))
        wallet_refund = _money(max(0.0, float(rollback.get("wallet_deducted") or 0)))
        wallet_refunded = any(
            row.get("type") == "invalid_clock_rollback_refund"
            and str(row.get("rollback_id") or "") == rollback_id
            for row in load_wallet().get("transactions", [])
        )
        _set_bank_balance_minor(
            payload, _bank_balance_minor(payload) + to_minor(bank_refund),
        )
        refunded_at = _now()
        rollback["invalid_clock_refunded_at"] = refunded_at
        rollback["invalid_clock_bank_refund"] = bank_refund
        rollback["invalid_clock_wallet_refund"] = wallet_refund
        for withdrawal_id in rollback.get("withdrawal_ids", []):
            withdrawal = withdrawals.get(str(withdrawal_id))
            if withdrawal:
                withdrawal.pop("clawed_back_at", None)
                withdrawal.pop("clawed_back_amount", None)
        _transaction(
            payload, "invalid_clock_rollback_refund", bank_refund + wallet_refund,
            rollback_id=rollback_id, bank_refund=bank_refund,
            wallet_refund=wallet_refund,
        )
        if wallet_refund and not wallet_refunded:
            _save_economy_with_wallet_adjustments(payload, [{
                "amount": wallet_refund,
                "type": "invalid_clock_rollback_refund",
                "metadata": {"rollback_id": rollback_id},
            }])
        changed = True
    return changed


def load_economy() -> dict[str, Any]:
    payload = load_document("economy", None, legacy_path=economy_path())
    if not isinstance(payload, dict):
        payload = _new_state()
    payload.setdefault("general_balance", 0.0)
    payload.setdefault("inventory", [])
    payload.setdefault("match_item_history", [])
    payload.setdefault("transactions", [])
    payload.setdefault("club_dividend_baselines", {})
    payload.setdefault("club_dividend_checked_through", {})
    payload.setdefault("club_dividend_forecasts", {})
    payload.setdefault("club_dividend_payments", [])
    payload.setdefault("club_dividend_last_settlement_month", "")
    payload.setdefault("salary_payments", [])
    # Salary is fixed to Sunday each week. Preserve accrued salary while
    # migrating older monthly or disabled schedules.
    if payload.get("salary_schedule") != "weekly" or "salary_schedule_defaulted" not in payload:
        payload["salary_schedule"] = "weekly"
        payload["salary_schedule_defaulted"] = True
        save_economy(payload)
    payload.setdefault("salary_schedule", "weekly")
    payload.setdefault("salary_schedule_defaulted", True)
    payload.setdefault("salary_checked_through", None)
    payload.setdefault("salary_accrued", 0.0)
    payload.setdefault("salary_accrual_through", payload.get("salary_checked_through"))
    payload.setdefault("activity_progress", {})
    payload.setdefault("activity_history", [])
    payload.setdefault("activity_daily_usage", {})
    owned_centres = list(payload.get("owned_activity_centres") or [])
    if int(payload.get("activity_centre_rules_version") or 1) < ACTIVITY_CENTRE_RULES_VERSION:
        owned_centres = [
            centre for centre in owned_centres
            if centre not in {"entertainment", "talk_room"}
        ]
        payload["owned_activity_facilities"] = []
        payload["activity_floor_cooldowns"] = {}
        payload["activity_centre_rules_version"] = ACTIVITY_CENTRE_RULES_VERSION
        payload["owned_activity_centres"] = owned_centres
        save_economy(payload)
    else:
        payload["owned_activity_centres"] = owned_centres
    payload.setdefault("owned_activity_facilities", [])
    payload.setdefault("activity_floor_cooldowns", {})
    payload.setdefault("match_intelligence_purchases", {})
    payload.setdefault("match_intelligence_price_locks", {})
    payload.setdefault("activity_centre_rules_version", ACTIVITY_CENTRE_RULES_VERSION)
    payload.setdefault("media_activity_daily_usage", {})
    payload.setdefault("psychological_counseling", {})
    language_learning = payload.setdefault("language_learning", {})
    language_auto = language_learning.setdefault("auto", {"enabled": False})
    player_enabled = bool(language_auto.get("player_enabled", language_auto.get("enabled", False)))
    language_auto.setdefault("enabled", player_enabled)
    language_auto.setdefault("player_enabled", player_enabled)
    language_auto.setdefault("staff_enabled", False)
    language_auto.setdefault("player_language_id", language_auto.get("language_id"))
    language_auto.setdefault("player_language_name", language_auto.get("language_name", ""))
    language_auto.setdefault("staff_language_id", None)
    language_auto.setdefault("staff_language_name", "")
    language_auto.setdefault("excluded_player_ids", language_auto.get("excluded_player_ids", []))
    language_auto.setdefault("excluded_staff_ids", [])
    language_learning.setdefault("plans", {})
    language_learning.setdefault("history", [])
    if payload.get("canteen_plan") not in {"basic", "nutrition", "elite", "peak"}:
        payload["canteen_plan"] = "basic"
    payload.setdefault("free_services", False)
    if payload.get("medical_default_treatment") not in MEDICAL_DEFAULT_TREATMENTS:
        payload["medical_default_treatment"] = "manual"
    payload.setdefault("medical_treatments", {})
    if int(payload.get("medical_treatment_pricing_version") or 1) < MEDICAL_TREATMENT_PRICING_VERSION:
        for treatment in payload["medical_treatments"].values():
            option = MEDICAL_TREATMENTS.get(str(treatment.get("mode") or ""))
            if option:
                write_minor(treatment, "daily_cost", to_minor(option["daily_cost"]))
        payload["medical_treatment_pricing_version"] = MEDICAL_TREATMENT_PRICING_VERSION
        save_economy(payload)
    else:
        payload.setdefault("medical_treatment_pricing_version", MEDICAL_TREATMENT_PRICING_VERSION)
    payload.setdefault("transfer_budget_withdrawals", [])
    payload.setdefault("transfer_budget_rollbacks", [])
    payload.setdefault("credit", {})
    payload.setdefault("default_notices", [])
    for key, value in _new_state()["credit"].items():
        payload["credit"].setdefault(key, value)
    credit = payload["credit"]
    if credit.get("status") == "active" and not float(credit.get("original_principal") or 0):
        borrowed_on = str(credit.get("borrowed_on") or "")
        historical_borrowing = sum(
            max(0.0, float(row.get("amount") or 0))
            for row in payload["transactions"]
            if row.get("type") == "credit_borrowed"
            and (not borrowed_on or str(row.get("game_date") or "") >= borrowed_on)
        )
        _set_credit_minor(
            credit, "original_principal",
            to_minor(historical_borrowing or credit.get("principal") or 0),
        )
        save_economy(payload)
    money_migrated = _normalize_economy_money(payload)
    if payload.get("wallet_mode") not in {"casino_only", "bank_v2"}:
        legacy_balance = payload["general_balance"]
        payload["legacy_general_balance_at_migration"] = legacy_balance
        _set_bank_balance_minor(payload, 0)
        payload["wallet_mode"] = "casino_only"
        payload["wallet_migrated_at"] = _now()
        _save_economy_with_wallet_adjustments(payload, [{
            "amount": legacy_balance, "type": "wallet_unified_migration",
        }])
    if payload.get("wallet_mode") != "bank_v2":
        payload["wallet_mode"] = "bank_v2"
        payload["bank_enabled_at"] = _now()
        save_economy(payload)
    if int(payload.get("referee_level_schema") or 1) < 2:
        for item in payload["inventory"]:
            if item.get("sku") == "referee_level2":
                item["sku"] = "referee_level3"
                item["name"] = PRODUCTS["referee_level3"]["name"]
                item["referee_level"] = 3
                item["effect_mode"] = "managed_ignore_opponent_red"
        payload["referee_level_schema"] = 2
        payload["referee_level_migrated_at"] = _now()
        save_economy(payload)
    # Product renames and current list prices apply to existing inventory.
    # Destroying an item never refunds money, so this is display metadata only.
    for item in payload["inventory"]:
        product = PRODUCTS.get(str(item.get("sku") or ""))
        if product:
            item["name"] = product["name"]
            write_minor(item, "price", to_minor(product["price"]))
            if product.get("tier"):
                item["tier"] = product["tier"]
    if _repair_invalid_clock_rollbacks(payload):
        save_economy(payload)
    elif money_migrated:
        save_economy(payload)
    return payload


def save_economy(payload: dict[str, Any]) -> None:
    _normalize_economy_money(payload)
    save_document("economy", payload, legacy_path=economy_path())


def _save_economy_with_wallet_adjustments(
    payload: dict[str, Any], adjustments: list[dict[str, Any]],
) -> dict[str, Any]:
    active = [
        adjustment for adjustment in adjustments
        if _money(adjustment.get("amount", 0)) != 0
    ]
    if not active:
        wallet = load_wallet()
        save_economy(payload)
        return wallet
    return commit_documents_with_wallet_adjustments(
        {"economy": payload}, active, legacy_path=economy_path(),
    )


def canteen_plan() -> str:
    with _LOCK:
        plan = str(load_economy().get("canteen_plan") or "basic")
        return plan if plan in {"basic", "nutrition", "elite", "peak"} else "basic"


def set_canteen_plan(plan: str) -> str:
    plan = str(plan or "")
    if plan not in {"basic", "nutrition", "elite", "peak"}:
        raise ValueError("invalid canteen plan")
    with _LOCK:
        payload = load_economy()
        previous = str(payload.get("canteen_plan") or "basic")
        if previous not in {"basic", "nutrition", "elite", "peak"}:
            previous = "basic"
        if previous != plan:
            payload["canteen_plan"] = plan
            _transaction(payload, "canteen_plan_changed", 0.0, previous=previous, plan=plan)
            save_economy(payload)
    return plan


def _transaction(
    payload: dict[str, Any], kind: str, amount: float, **details: Any,
) -> dict[str, Any]:
    amount_minor = to_minor(amount)
    transaction = {
        "id": str(uuid.uuid4()), "at": _now(), "type": kind,
        **details,
    }
    write_minor(transaction, "amount", amount_minor)
    write_minor(
        transaction, "general_balance_after", _bank_balance_minor(payload),
    )
    migrate_money_fields(transaction, _ECONOMY_TRANSACTION_MONEY_FIELDS)
    payload["transactions"].append(transaction)
    payload["transactions"] = payload["transactions"][-1000:]
    return transaction


def _collect_combined_funds(
    payload: dict[str, Any], amount: float, kind: str, *,
    allow_bank_overdraft: bool = False, **details: Any,
) -> dict[str, Any]:
    amount_minor = to_minor(amount)
    amount = from_minor(amount_minor)
    if amount_minor <= 0:
        return {"bank": 0.0, "wallet": 0.0, "total": 0.0}
    bank_available_minor = max(0, _bank_balance_minor(payload))
    wallet_available_minor = max(0, to_minor(available_balance()))
    if not allow_bank_overdraft and bank_available_minor + wallet_available_minor < amount_minor:
        raise ValueError("银行与钱包余额合计不足")
    bank_from_balance_minor = min(bank_available_minor, amount_minor)
    remaining_minor = amount_minor - bank_from_balance_minor
    wallet_minor = min(wallet_available_minor, remaining_minor)
    bank_overdraft_minor = remaining_minor - wallet_minor
    bank_minor = bank_from_balance_minor + bank_overdraft_minor
    _set_bank_balance_minor(payload, _bank_balance_minor(payload) - bank_minor)
    bank = from_minor(bank_minor)
    wallet = from_minor(wallet_minor)
    bank_overdraft = from_minor(bank_overdraft_minor)
    transaction = _transaction(
        payload, kind, -amount, bank_used=bank, bank_overdraft=bank_overdraft,
        wallet_used=wallet, **details,
    )
    return {
        "bank": bank, "wallet": wallet, "total": amount,
        "transaction_id": str(transaction["id"]),
    }


def welfare_status(
    match_intelligence_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    with _LOCK:
        payload = load_economy()
        settings = load_settings()
        owned_centres = list(payload.get("owned_activity_centres") or DEFAULT_ACTIVITY_CENTRES)
        cached_prices = _welfare_pricing_cached(
            _pricing_settings_cache_key(settings),
        )
        centre_prices = dict(cached_prices[0])
        nationality_processing_prices = dict(cached_prices[3])
        language_learning = _public_money_view(payload.get("language_learning") or {})
        language_learning["instant_prices"] = dict(cached_prices[1])
        language_learning["instant_max_prices"] = dict(cached_prices[2])
        medical = _medical_status_from_payload(payload)
        result = {
            "activities": {key: dict(value) for key, value in PLAYER_ACTIVITIES.items()},
            "activity_centres": {
                key: {
                    **value,
                    "available": _activity_centre_available(value),
                    "price": centre_prices[key],
                    "owned": _activity_centre_owned(payload, key),
                    "purchased": key in owned_centres,
                }
                for key, value in ACTIVITY_CENTRES.items()
            },
            "activity_facilities": {
                key: {
                    **value,
                    "owned": _activity_facility_owned(payload, key),
                }
                for key, value in ACTIVITY_FACILITIES.items()
            },
            "activity_progress": dict(payload.get("activity_progress") or {}),
            "activity_daily_usage": dict(payload.get("activity_daily_usage") or {}),
            "activity_floor_cooldowns": dict(payload.get("activity_floor_cooldowns") or {}),
            "media_activity_daily_usage": dict(payload.get("media_activity_daily_usage") or {}),
            "activity_history": list(reversed((payload.get("activity_history") or [])[-50:])),
            "nationality_processing": {
                "club_price": 0.0 if bool(payload.get("free_services")) else nationality_processing_prices["club"],
                "world_price": 0.0 if bool(payload.get("free_services")) else nationality_processing_prices["world"],
            },
            "psychological_counseling": dict(payload.get("psychological_counseling") or {}),
            "language_learning": language_learning,
            **medical,
        }
        if match_intelligence_candidates is not None:
            result["_match_intelligence"] = match_intelligence_status(
                match_intelligence_candidates, payload=payload,
            )
        return result


def _medical_status_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Project only the medical fields without rebuilding activity-centre state."""
    return {
        "free_services": bool(payload.get("free_services")),
        "default_treatment": str(payload.get("medical_default_treatment") or "manual"),
        "treatment_options": {
            key: dict(value) for key, value in MEDICAL_TREATMENTS.items()
        },
        "treatments": dict(payload.get("medical_treatments") or {}),
    }


def medical_status() -> dict[str, Any]:
    """Return the current medical snapshot without projecting unrelated welfare data."""
    with _LOCK:
        return _medical_status_from_payload(load_economy())


def assert_activity_centre_unlocked(centre: str) -> dict[str, Any]:
    centre = str(centre or "")
    option = ACTIVITY_CENTRES.get(centre)
    if not option:
        raise ValueError("活动中心楼层无效")
    if not _activity_centre_available(option):
        raise ValueError(str(option.get("unavailable_reason") or "该楼层暂未开启"))
    with _LOCK:
        payload = load_economy()
        if not _activity_centre_owned(payload, centre):
            raise ValueError(f"请先解锁{option['name']}")
    return dict(option)


def purchase_activity_centre(centre: str, *, free: bool = False) -> dict[str, Any]:
    centre = str(centre or "")
    option = ACTIVITY_CENTRES.get(centre)
    if not option:
        raise ValueError("活动中心楼层无效")
    if not _activity_centre_available(option):
        raise ValueError(str(option.get("unavailable_reason") or "该楼层暂未开启"))
    with _LOCK:
        payload = load_economy()
        free = bool(payload.get("free_services"))
        owned_centres = payload.setdefault("owned_activity_centres", list(DEFAULT_ACTIVITY_CENTRES))
        if _activity_centre_owned(payload, centre):
            raise ValueError(f"{option['name']}已经解锁")
        price = 0.0 if free else local_purchase_price(option["price"])
        payment = _collect_combined_funds(
            payload, price, "activity_centre_purchase",
            centre=centre, centre_name=str(option["name"]), free=bool(free),
        )
        owned_centres.append(centre)
        _save_economy_with_wallet_adjustments(payload, [{
            "amount": -payment["wallet"], "type": "activity_centre_purchase",
            "metadata": {"centre": centre, "centre_name": str(option["name"])},
        }])
        return {"centre": centre, "owned": True, "payment": payment}


def _assert_activity_facility_owned(
    payload: dict[str, Any], facility: str,
) -> dict[str, Any]:
    option = ACTIVITY_FACILITIES.get(str(facility or ""))
    if not option:
        raise ValueError("活动房间无效")
    centre = str(option["centre"])
    centre_option = ACTIVITY_CENTRES[centre]
    if not _activity_centre_available(centre_option):
        raise ValueError(str(centre_option.get("unavailable_reason") or "该楼层暂未开启"))
    if not _activity_centre_owned(payload, centre):
        raise ValueError(f"请先解锁{centre_option['name']}")
    if not _activity_facility_owned(payload, facility):
        raise ValueError(f"请先解锁{option['name']}")
    return dict(option)


def assert_activity_facility_unlocked(facility: str) -> dict[str, Any]:
    with _LOCK:
        return _assert_activity_facility_owned(load_economy(), str(facility or ""))


def purchase_activity_facility(facility: str, *, free: bool = False) -> dict[str, Any]:
    facility = str(facility or "")
    option = ACTIVITY_FACILITIES.get(facility)
    if not option:
        raise ValueError("活动房间无效")
    if option.get("included_with_centre"):
        raise ValueError(f"{option['name']}随楼层自动开放")
    centre = str(option["centre"])
    with _LOCK:
        payload = load_economy()
        if not _activity_centre_owned(payload, centre):
            raise ValueError(f"请先解锁{ACTIVITY_CENTRES[centre]['name']}")
        owned_facilities = payload.setdefault("owned_activity_facilities", [])
        if _activity_facility_owned(payload, facility):
            raise ValueError(f"{option['name']}已经解锁")
        free = bool(payload.get("free_services"))
        price = 0.0 if free else local_purchase_price(option["price"])
        payment = _collect_combined_funds(
            payload, price, "activity_facility_purchase",
            facility=facility, facility_name=str(option["name"]),
            centre=centre, free=free,
        )
        owned_facilities.append(facility)
        _save_economy_with_wallet_adjustments(payload, [{
            "amount": -payment["wallet"], "type": "activity_facility_purchase",
            "metadata": {
                "facility": facility, "facility_name": str(option["name"]),
                "centre": centre,
            },
        }])
        return {"facility": facility, "owned": True, "payment": payment}


def _match_intelligence_combined_funds_minor(payload: dict[str, Any]) -> int:
    return max(0, _bank_balance_minor(payload)) + max(
        0, to_minor(available_balance()),
    )


def _match_intelligence_betting_limit_minor() -> int | None:
    """Return the same effective single-bet profit limit used by betting."""
    settings = load_settings()
    custom_limits_enabled = bool(settings.get("betting_limits_enabled"))
    if custom_limits_enabled and bool(settings.get("unlimited_betting")):
        return None
    setting_key = (
        "betting_limit_single" if custom_limits_enabled
        else "default_betting_limit_single"
    )
    try:
        limit = float(settings.get(setting_key) or 0.0)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(limit) or limit <= 0:
        return None
    return to_minor(limit)


def _match_intelligence_pricing_base_minor(
    combined_funds_minor: int, betting_limit_minor: int | None,
) -> int:
    """Cap the intelligence basis at funds the account has actually held."""
    combined_funds_minor = max(0, int(combined_funds_minor))
    if betting_limit_minor is None:
        return combined_funds_minor
    return min(combined_funds_minor, max(0, int(betting_limit_minor)))


def _match_intelligence_price_lock(
    payload: dict[str, Any], game_date: str, *, create: bool,
) -> tuple[dict[str, Any], bool]:
    game_date = str(game_date or "")
    try:
        date.fromisoformat(game_date)
    except ValueError:
        if create:
            raise ValueError("尚未读取当前游戏日期，无法锁定今日情报价格")
        return {}, False
    locks = payload.setdefault("match_intelligence_price_locks", {})
    existing = dict(locks.get(game_date) or {})
    if (
        "combined_funds_minor" in existing
        or "combined_funds" in existing
    ):
        if create:
            current_combined_minor = _match_intelligence_combined_funds_minor(payload)
            existing_combined_minor = read_minor(existing, "combined_funds")
            combined_minor = max(existing_combined_minor, current_combined_minor)
            betting_limit_minor = _match_intelligence_betting_limit_minor()
            desired_source = (
                "single_betting_limit"
                if betting_limit_minor is not None
                else "combined_funds"
            )
            current_source = str(existing.get("pricing_source") or "")
            existing_base_minor = read_minor(existing, "pricing_base")
            desired_base_minor = _match_intelligence_pricing_base_minor(
                combined_minor, betting_limit_minor,
            )
            changed = False
            if combined_minor != existing_combined_minor:
                write_minor(existing, "combined_funds", combined_minor)
                changed = True
            if desired_base_minor != existing_base_minor:
                write_minor(existing, "pricing_base", desired_base_minor)
                changed = True
            if current_source != desired_source:
                existing["pricing_source"] = desired_source
                changed = True
            if changed:
                existing["repriced_at"] = _now()
                locks[game_date] = existing
                return existing, True
        return existing, False
    combined_funds_minor = _match_intelligence_combined_funds_minor(payload)
    price_lock = {
        "game_date": game_date,
        "locked_at": _now(),
    }
    write_minor(price_lock, "combined_funds", combined_funds_minor)
    betting_limit_minor = _match_intelligence_betting_limit_minor()
    if betting_limit_minor is not None:
        write_minor(
            price_lock, "pricing_base",
            _match_intelligence_pricing_base_minor(
                combined_funds_minor, betting_limit_minor,
            ),
        )
        price_lock["pricing_source"] = "single_betting_limit"
    else:
        write_minor(price_lock, "pricing_base", combined_funds_minor)
        price_lock["pricing_source"] = "combined_funds"
    locks[game_date] = price_lock
    return price_lock, True


def _public_match_intelligence_pricing(
    price_lock: dict[str, Any], game_date: str, fallback_minor: int,
) -> dict[str, Any]:
    locked = bool(price_lock)
    if locked:
        base_minor = read_minor(price_lock, "pricing_base")
        if not base_minor and (
            "pricing_base_minor" not in price_lock
            and "pricing_base" not in price_lock
        ):
            base_minor = read_minor(price_lock, "combined_funds")
    else:
        base_minor = int(fallback_minor)
    combined_minor = (
        read_minor(price_lock, "combined_funds") if locked else int(fallback_minor)
    )
    result = {
        "locked": locked,
        "game_date": str(game_date or ""),
        "combined_funds": from_minor(combined_minor),
        "locked_at": str(price_lock.get("locked_at") or ""),
    }
    pricing_source = str(price_lock.get("pricing_source") or "combined_funds")
    if pricing_source == "single_betting_limit":
        result.update({
            "pricing_base": from_minor(base_minor),
            "pricing_source": pricing_source,
        })
    return result


def _match_intelligence_quote_minor(
    payload: dict[str, Any], tier: str, available_minor: int | None = None,
    *, pricing_source: str = "combined_funds", purchase_mode: str = "specific",
) -> int:
    target = MATCH_INTELLIGENCE_TIERS[tier]
    if bool(payload.get("free_services")):
        return 0
    if available_minor is None:
        available_minor = _match_intelligence_combined_funds_minor(payload)
    basis_points = int(
        target["random_basis_points"]
        if purchase_mode == "random"
        else target["basis_points"]
    )
    percentage_price = (available_minor * basis_points + 5_000) // 10_000
    return percentage_price


def _match_intelligence_tier_description(
    tier: str, source: str, purchase_mode: str = "specific",
) -> str:
    option = MATCH_INTELLIGENCE_TIERS[tier]
    if source == "single_betting_limit":
        basis_points = int(
            option["random_basis_points"]
            if purchase_mode == "random"
            else option["basis_points"]
        )
        percentage = basis_points / 100
        suffix = "\uff0c\u5e76\u5305\u542b\u524d\u4e24\u9879" if tier == "score" else ""
        return (
            f"\u5355\u5173\u6295\u6ce8\u4e0a\u9650\u4e0e\u5408\u8ba1\u8d44\u91d1"
            f"\u8f83\u4f4e\u503c{percentage:g}%{suffix}"
        )
    return str(
        option["random_description"]
        if purchase_mode == "random"
        else option["description"]
    )


def _public_intelligence_team(team: Any) -> dict[str, Any]:
    team = dict(team or {})
    return {
        "id": int(team.get("id") or 0),
        "name": str(team.get("name") or team.get("short_name") or "未知球队"),
    }


def _match_intelligence_reveal(
    candidate: dict[str, Any], tier: str,
) -> dict[str, Any]:
    home = _public_intelligence_team(candidate.get("home"))
    away = _public_intelligence_team(candidate.get("away"))
    home_goals = int(candidate.get("home_goals") or 0)
    away_goals = int(candidate.get("away_goals") or 0)
    outcome = "home" if home_goals > away_goals else "away" if away_goals > home_goals else "draw"
    if tier == "outcome":
        return {
            "outcome": outcome,
            "outcome_label": (
            f"{home['name']}获胜" if outcome == "home"
            else f"{away['name']}获胜" if outcome == "away"
            else "90分钟战平"
            ),
        }
    if tier == "total_goals":
        return {"total_goals": home_goals + away_goals}
    if tier == "score":
        return {"home_goals": home_goals, "away_goals": away_goals}
    return {}


def _match_intelligence_prediction_records(
    purchase: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Read current independent purchases and preserve legacy tier value."""
    predictions = {
        str(key): dict(value)
        for key, value in dict(purchase.get("predictions") or {}).items()
        if key in MATCH_INTELLIGENCE_TIERS and isinstance(value, dict)
    }
    if predictions and "score" in predictions:
        score_record = dict(predictions["score"])
        score_reveal = dict(score_record.get("reveal") or {})
        if (
            score_reveal.get("home_goals") is not None
            and score_reveal.get("away_goals") is not None
        ):
            score_candidate = {
                **purchase,
                "home_goals": int(score_reveal["home_goals"]),
                "away_goals": int(score_reveal["away_goals"]),
            }
            for included_tier in ("outcome", "total_goals"):
                included_option = MATCH_INTELLIGENCE_TIERS[included_tier]
                predictions.setdefault(included_tier, {
                    "tier": included_tier,
                    "tier_name": str(included_option["name"]),
                    "reveal": _match_intelligence_reveal(
                        score_candidate, included_tier,
                    ),
                    "purchased_at": str(
                        score_record.get("purchased_at")
                        or purchase.get("purchased_at") or ""
                    ),
                    "purchased_game_date": str(
                        score_record.get("purchased_game_date")
                        or purchase.get("purchased_game_date") or ""
                    ),
                    "unlocked_by": "score",
                })
    if predictions:
        return predictions
    legacy_tier = str(purchase.get("tier") or "")
    legacy_reveal = dict(purchase.get("reveal") or {})
    if legacy_tier and legacy_reveal.get("outcome_label"):
        predictions["outcome"] = {
            "reveal": {
                key: legacy_reveal[key]
                for key in ("outcome", "outcome_label")
                if key in legacy_reveal
            },
        }
    if (
        legacy_reveal.get("home_goals") is not None
        and legacy_reveal.get("away_goals") is not None
    ):
        home_goals = int(legacy_reveal["home_goals"])
        away_goals = int(legacy_reveal["away_goals"])
        predictions["total_goals"] = {
            "reveal": {"total_goals": home_goals + away_goals},
        }
        predictions["score"] = {
            "reveal": {
                "home_goals": home_goals, "away_goals": away_goals,
            },
        }
    return predictions


def match_intelligence_betting_unlocked_fixture_keys(
    *, payload: dict[str, Any] | None = None,
) -> set[tuple[str, int, int, int]]:
    """Return fixtures whose purchased intelligence bypasses engine-view locks."""
    with _LOCK:
        payload = payload if payload is not None else load_economy()
        unlocked: set[tuple[str, int, int, int]] = set()
        for fixture_id, raw_purchase in dict(
            payload.get("match_intelligence_purchases") or {}
        ).items():
            purchase = dict(raw_purchase or {})
            if not _match_intelligence_prediction_records(purchase):
                continue
            parts = str(fixture_id or purchase.get("fixture_id") or "").split("|")
            if len(parts) != 4:
                continue
            try:
                unlocked.add((parts[0], int(parts[1]), int(parts[2]), int(parts[3])))
            except ValueError:
                continue
        return unlocked


def _match_intelligence_tier_templates(
    payload: dict[str, Any], available_minor: int, pricing_source: str,
) -> dict[str, dict[str, Any]]:
    """Build candidate-invariant tier pricing and copy once per status call."""
    templates: dict[str, dict[str, Any]] = {}
    for tier in MATCH_INTELLIGENCE_TIER_ORDER:
        option = MATCH_INTELLIGENCE_TIERS[tier]
        specific_price = from_minor(_match_intelligence_quote_minor(
            payload, tier, available_minor,
            pricing_source=pricing_source,
            purchase_mode="specific",
        ))
        random_price = from_minor(_match_intelligence_quote_minor(
            payload, tier, available_minor,
            pricing_source=pricing_source,
            purchase_mode="random",
        ))
        templates[tier] = {
            "key": tier,
            "name": str(option["name"]),
            "description": _match_intelligence_tier_description(
                tier, pricing_source, "specific",
            ),
            "specific_description": _match_intelligence_tier_description(
                tier, pricing_source, "specific",
            ),
            "random_description": _match_intelligence_tier_description(
                tier, pricing_source, "random",
            ),
            "percentage": float(option["basis_points"]) / 100.0,
            "specific_percentage": float(option["basis_points"]) / 100.0,
            "random_percentage": float(option["random_basis_points"]) / 100.0,
            "price": specific_price,
            "specific_price": specific_price,
            "random_price": random_price,
        }
    return templates


def _match_intelligence_history_tier_templates() -> dict[str, dict[str, Any]]:
    return {
        tier: {
            "key": tier,
            "name": str(MATCH_INTELLIGENCE_TIERS[tier]["name"]),
            "description": str(MATCH_INTELLIGENCE_TIERS[tier]["description"]),
            "price": 0.0,
        }
        for tier in MATCH_INTELLIGENCE_TIER_ORDER
    }


def match_intelligence_status(
    candidates: list[dict[str, Any]], *, payload: dict[str, Any] | None = None,
    pricing_date: str = "",
) -> dict[str, Any]:
    """Return purchasable metadata while withholding every unpaid result field."""
    with _LOCK:
        payload = payload if payload is not None else load_economy()
        unlocked = _activity_centre_owned(payload, "intelligence")
        purchases = dict(payload.get("match_intelligence_purchases") or {})
        pricing_date = str(
            pricing_date or (candidates[0].get("fixture_date") if candidates else "") or ""
        )
        price_lock, _created = _match_intelligence_price_lock(
            payload, pricing_date, create=False,
        )
        available_minor = 0
        pricing_source = "combined_funds"
        if price_lock:
            available_minor = read_minor(price_lock, "pricing_base")
            if not available_minor and (
                "pricing_base_minor" not in price_lock
                and "pricing_base" not in price_lock
            ):
                available_minor = read_minor(price_lock, "combined_funds")
            pricing_source = str(
                price_lock.get("pricing_source") or "combined_funds"
            )
        elif unlocked and candidates:
            available_minor = _match_intelligence_combined_funds_minor(payload)
            betting_limit_minor = _match_intelligence_betting_limit_minor()
            if betting_limit_minor is not None:
                available_minor = _match_intelligence_pricing_base_minor(
                    available_minor, betting_limit_minor,
                )
                pricing_source = "single_betting_limit"
        rows: list[dict[str, Any]] = []
        if unlocked:
            tier_templates = _match_intelligence_tier_templates(
                payload, available_minor, pricing_source,
            )
            history_tier_templates = _match_intelligence_history_tier_templates()
            for candidate in candidates:
                fixture_id = str(candidate.get("fixture_id") or "")
                if not fixture_id:
                    continue
                previous = dict(purchases.get(fixture_id) or {})
                predictions = _match_intelligence_prediction_records(previous)
                purchased_tiers = [
                    tier for tier in MATCH_INTELLIGENCE_TIER_ORDER
                    if tier in predictions
                ]
                reveal: dict[str, Any] = {}
                for tier in purchased_tiers:
                    reveal.update(dict(predictions[tier].get("reveal") or {}))
                tiers = [
                    {
                        **tier_templates[tier],
                        "purchased": tier in predictions,
                        "purchased_game_date": str(
                            predictions.get(tier, {}).get("purchased_game_date") or ""
                        ),
                    }
                    for tier in MATCH_INTELLIGENCE_TIER_ORDER
                ]
                rows.append({
                    "fixture_id": fixture_id,
                    "fixture_date": str(candidate.get("fixture_date") or ""),
                    "kickoff_minutes": candidate.get("kickoff_minutes"),
                    "kickoff_time": candidate.get("kickoff_time"),
                    "competition_id": int(candidate.get("competition_id") or 0),
                    "competition_name": str(candidate.get("competition_name") or "未知赛事"),
                    "home": _public_intelligence_team(candidate.get("home")),
                    "away": _public_intelligence_team(candidate.get("away")),
                    "tiers": tiers,
                    "purchased_tiers": purchased_tiers,
                    "available": True,
                    "purchased_today": bool(pricing_date) and any(
                        str(record.get("purchased_game_date") or "") == pricing_date
                        for record in predictions.values()
                    ),
                    "reveal": reveal,
                })
            current_fixture_ids = {
                str(row.get("fixture_id") or "") for row in rows
            }
            for fixture_id, raw_purchase in purchases.items():
                fixture_id = str(fixture_id or "")
                if not fixture_id or fixture_id in current_fixture_ids:
                    continue
                previous = dict(raw_purchase or {})
                predictions = _match_intelligence_prediction_records(previous)
                if not predictions:
                    continue
                purchased_tiers = [
                    tier for tier in MATCH_INTELLIGENCE_TIER_ORDER
                    if tier in predictions
                ]
                reveal: dict[str, Any] = {}
                for tier in purchased_tiers:
                    reveal.update(dict(predictions[tier].get("reveal") or {}))
                tiers = [
                    {
                        **history_tier_templates[tier],
                        "purchased": tier in predictions,
                        "purchased_game_date": str(
                            predictions.get(tier, {}).get("purchased_game_date") or ""
                        ),
                    }
                    for tier in MATCH_INTELLIGENCE_TIER_ORDER
                ]
                rows.append({
                    "fixture_id": fixture_id,
                    "fixture_date": str(previous.get("fixture_date") or ""),
                    "kickoff_minutes": previous.get("kickoff_minutes"),
                    "kickoff_time": previous.get("kickoff_time"),
                    "competition_id": int(previous.get("competition_id") or 0),
                    "competition_name": str(previous.get("competition_name") or "未知赛事"),
                    "home": _public_intelligence_team(previous.get("home")),
                    "away": _public_intelligence_team(previous.get("away")),
                    "tiers": tiers,
                    "purchased_tiers": purchased_tiers,
                    "available": False,
                    "purchased_today": bool(pricing_date) and any(
                        str(record.get("purchased_game_date") or "") == pricing_date
                        for record in predictions.values()
                    ),
                    "reveal": reveal,
                })
            rows.sort(key=lambda row: (
                str(row.get("fixture_date") or ""),
                int(row.get("kickoff_minutes") or 24 * 60),
            ))
        return {
            "unlocked": unlocked,
            "floor": "5F",
            "pricing_basis": (
                "daily_entry_single_betting_limit_percentage"
                if pricing_source == "single_betting_limit"
                else "daily_entry_combined_funds_percentage"
            ),
            "pricing": _public_match_intelligence_pricing(
                price_lock, pricing_date, available_minor,
            ),
            "candidates": rows,
        }


def open_match_intelligence_status(
    candidates: list[dict[str, Any]], game_date: str,
) -> dict[str, Any]:
    """Lock the day's price basis on explicit entry, never on background probes."""
    with _LOCK:
        payload = load_economy()
        unlocked = _activity_centre_owned(payload, "intelligence")
        if unlocked:
            _price_lock, created = _match_intelligence_price_lock(
                payload, game_date, create=True,
            )
            if created:
                save_economy(payload)
        return match_intelligence_status(
            candidates, payload=payload, pricing_date=game_date,
        )


def purchase_match_intelligence(
    candidate: dict[str, Any], tier: str, client_submission_id: str,
    *, game_date: str = "", purchase_mode: str = "specific",
) -> dict[str, Any]:
    tier = str(tier or "")
    purchase_mode = str(purchase_mode or "specific")
    client_submission_id = str(client_submission_id or "").strip()
    if tier not in MATCH_INTELLIGENCE_TIERS:
        raise ValueError("情报项目无效")
    if purchase_mode not in {"random", "specific"}:
        raise ValueError("情报购买模式无效")
    if not client_submission_id or len(client_submission_id) > 128:
        raise ValueError("情报获取请求标识无效")
    fixture_id = str(candidate.get("fixture_id") or "")
    if not fixture_id:
        raise ValueError("比赛情报身份无效")
    with _LOCK:
        payload = load_economy()
        if not _activity_centre_owned(payload, "intelligence"):
            raise ValueError("请先解锁情报中心")
        purchases = payload.setdefault("match_intelligence_purchases", {})
        previous = dict(purchases.get(fixture_id) or {})
        for payment_record in previous.get("payments") or []:
            if str(payment_record.get("client_submission_id") or "") == client_submission_id:
                return {
                    "purchase": previous,
                    "payment": dict(payment_record.get("payment") or {}),
                    "already_purchased": True,
                }
        predictions = _match_intelligence_prediction_records(previous)
        if tier in predictions:
            return {
                "purchase": previous,
                "payment": {"bank": 0.0, "wallet": 0.0, "total": 0.0},
                "already_purchased": True,
            }
        pricing_date = str(game_date or candidate.get("fixture_date") or "")
        price_lock, _created = _match_intelligence_price_lock(
            payload, pricing_date, create=True,
        )
        pricing_base_minor = read_minor(price_lock, "pricing_base")
        if not pricing_base_minor and (
            "pricing_base_minor" not in price_lock
            and "pricing_base" not in price_lock
        ):
            pricing_base_minor = read_minor(price_lock, "combined_funds")
        pricing_source = str(
            price_lock.get("pricing_source") or "combined_funds"
        )
        price = from_minor(_match_intelligence_quote_minor(
            payload, tier, pricing_base_minor,
            pricing_source=pricing_source, purchase_mode=purchase_mode,
        ))
        option = MATCH_INTELLIGENCE_TIERS[tier]
        basis_points = int(
            option["random_basis_points"]
            if purchase_mode == "random"
            else option["basis_points"]
        )
        payment = _collect_combined_funds(
            payload, price, "match_intelligence_purchase",
            fixture_id=fixture_id, tier=tier, tier_name=str(option["name"]),
            percentage=float(basis_points) / 100.0,
            pricing_source=pricing_source,
            purchase_mode=purchase_mode,
            pricing_game_date=pricing_date,
            pricing_base_balance=from_minor(pricing_base_minor),
            client_submission_id=client_submission_id,
        )
        purchased_at = _now()
        included_tiers = (
            MATCH_INTELLIGENCE_TIER_ORDER if tier == "score" else (tier,)
        )
        for included_tier in included_tiers:
            included_option = MATCH_INTELLIGENCE_TIERS[included_tier]
            predictions.setdefault(included_tier, {
                "tier": included_tier,
                "tier_name": str(included_option["name"]),
                "reveal": _match_intelligence_reveal(candidate, included_tier),
                "purchased_at": purchased_at,
                "purchased_game_date": pricing_date,
                **({"unlocked_by": "score"} if included_tier != tier else {}),
            })
        purchase = {
            "fixture_id": fixture_id,
            "fixture_date": str(candidate.get("fixture_date") or ""),
            "kickoff_minutes": candidate.get("kickoff_minutes"),
            "kickoff_time": candidate.get("kickoff_time"),
            "competition_id": int(candidate.get("competition_id") or 0),
            "competition_name": str(candidate.get("competition_name") or "未知赛事"),
            "home": _public_intelligence_team(candidate.get("home")),
            "away": _public_intelligence_team(candidate.get("away")),
            "predictions": predictions,
            "purchased_at": purchased_at,
            "purchased_game_date": pricing_date,
            "payments": [
                *(previous.get("payments") or []),
                {
                    "client_submission_id": client_submission_id,
                    "tier": tier,
                    "payment": dict(payment),
                },
            ],
        }
        purchases[fixture_id] = purchase
        _save_economy_with_wallet_adjustments(payload, [{
            "amount": -payment["wallet"],
            "type": "match_intelligence_purchase",
            "metadata": {
                "fixture_id": fixture_id, "tier": tier,
                "client_submission_id": client_submission_id,
            },
        }])
        return {
            "purchase": purchase,
            "payment": payment,
            "already_purchased": False,
        }


def _intelligence_settlement_score(evaluation: Any) -> tuple[int, int] | None:
    if not isinstance(evaluation, dict):
        return None
    raw = str(evaluation.get("score") or "")
    try:
        home, away = raw.split("-", 1)
        score = (int(home), int(away))
    except (TypeError, ValueError):
        return None
    return score if min(score) >= 0 else None


def _intelligence_fixture_id(leg: dict[str, Any]) -> str:
    try:
        fixture_date = str(leg.get("fixture_date") or "")
        competition_id = int(leg.get("competition_id"))
        home_id = int(leg.get("home_id"))
        away_id = int(leg.get("away_id"))
        date.fromisoformat(fixture_date)
        if min(competition_id, home_id, away_id) <= 0:
            return ""
        return "|".join(map(str, (
            fixture_date, competition_id, home_id, away_id,
        )))
    except (TypeError, ValueError):
        return ""


def reconcile_match_intelligence_refunds(
    settled_records: list[dict[str, Any]], game_date: str = "",
) -> list[dict[str, Any]]:
    """Refund intelligence payments whose revealed option disagrees with settlement.

    The operation is account-transactional and keyed by the original payment
    transaction id.  Re-running settlement therefore cannot credit the same
    payment twice or send duplicate mail.
    """
    if not settled_records:
        return []
    with _LOCK:
        payload = load_economy()
        purchases = payload.get("match_intelligence_purchases") or {}
        if not isinstance(purchases, dict):
            return []
        mail = load_document("mail", [])
        if not isinstance(mail, list):
            mail = []
        existing_mail = {
            str(item.get("source_id") or item.get("id") or "")
            for item in mail if isinstance(item, dict)
        }
        adjustments: list[dict[str, Any]] = []
        refunds: list[dict[str, Any]] = []
        changed = False
        for settled in settled_records:
            raw_legs = settled.get("leg_records")
            legs = raw_legs if isinstance(raw_legs, list) else [settled]
            evaluations = settled.get("settlement") or []
            for index, leg in enumerate(legs):
                if not isinstance(leg, dict) or index >= len(evaluations):
                    continue
                score = _intelligence_settlement_score(evaluations[index])
                fixture_id = _intelligence_fixture_id(leg)
                if score is None or not fixture_id:
                    continue
                purchase = purchases.get(fixture_id)
                if not isinstance(purchase, dict):
                    continue
                predictions = purchase.get("predictions") or {}
                mismatches: list[str] = []
                actual_outcome = (
                    "home" if score[0] > score[1]
                    else "away" if score[1] > score[0] else "draw"
                )
                actual_total = score[0] + score[1]
                for tier in MATCH_INTELLIGENCE_TIER_ORDER:
                    prediction = predictions.get(tier)
                    reveal = prediction.get("reveal") if isinstance(prediction, dict) else None
                    if not isinstance(reveal, dict):
                        continue
                    mismatch = (
                        tier == "outcome" and reveal.get("outcome") != actual_outcome
                        or tier == "total_goals" and int(reveal.get("total_goals", -1)) != actual_total
                        or tier == "score" and (
                            int(reveal.get("home_goals", -1)),
                            int(reveal.get("away_goals", -1)),
                        ) != score
                    )
                    if mismatch:
                        mismatches.append(tier)
                if not mismatches:
                    continue
                for payment_record in purchase.get("payments") or []:
                    if not isinstance(payment_record, dict):
                        continue
                    tier = str(payment_record.get("tier") or "")
                    if tier not in mismatches:
                        continue
                    payment = dict(payment_record.get("payment") or {})
                    bank_minor = max(0, to_minor(payment.get("bank") or 0))
                    wallet_minor = max(0, to_minor(payment.get("wallet") or 0))
                    total_minor = bank_minor + wallet_minor
                    transaction_id = str(payment.get("transaction_id") or "")
                    source_id = f"match_intelligence_refund:{fixture_id}:{payment_record.get('client_submission_id') or transaction_id or tier}"
                    already_refunded = bool(payment_record.get("refunded")) or any(
                        str(row.get("refund_of") or "") == transaction_id
                        for row in payload.get("transactions") or []
                        if isinstance(row, dict) and transaction_id
                    )
                    refund = {
                        "fixture_id": fixture_id,
                        "tier": tier,
                        "mismatches": list(mismatches),
                        "source_id": source_id,
                        "already_refunded": already_refunded,
                    }
                    if not already_refunded:
                        _set_bank_balance_minor(
                            payload, _bank_balance_minor(payload) + bank_minor,
                        )
                        transaction = _transaction(
                            payload, "match_intelligence_refund",
                            from_minor(total_minor),
                            fixture_id=fixture_id, tier=tier,
                            refund_of=transaction_id or None,
                        )
                        if wallet_minor:
                            adjustments.append({
                                "amount": from_minor(wallet_minor),
                                "type": "match_intelligence_refund",
                                "metadata": {
                                    "fixture_id": fixture_id, "tier": tier,
                                    "refund_of": transaction_id or None,
                                },
                            })
                        refund.update({
                            "bank": from_minor(bank_minor),
                            "wallet": from_minor(wallet_minor),
                            "total": from_minor(total_minor),
                            "transaction_id": str(transaction["id"]),
                            "already_refunded": False,
                        })
                        payment_record["refunded"] = True
                        payment_record["refund"] = dict(refund)
                        changed = True
                    else:
                        refund.update({
                            "bank": from_minor(bank_minor),
                            "wallet": from_minor(wallet_minor),
                            "total": from_minor(total_minor),
                        })
                    if source_id not in existing_mail:
                        amount = float(refund.get("total") or 0.0)
                        mail.append({
                            "id": source_id,
                            "source_id": source_id,
                            "type": "match_intelligence_refund",
                            "title": "比赛情报退款",
                            "message": (
                                f"{purchase.get('home', {}).get('name', '-') } VS "
                                f"{purchase.get('away', {}).get('name', '-') }的比赛情报与最终赛果不一致，"
                                f"已自动退款 {amount:,.2f}。"
                            ),
                            "body": (
                                f"购买项目：{'、'.join(mismatches)}；"
                                f"最终比分：{score[0]}-{score[1]}。"
                            ),
                            "amount": amount,
                            "fixture_id": fixture_id,
                            "tier": tier,
                            "game_date": str(game_date or ""),
                            "created_at": _now(),
                            "read": False,
                        })
                        existing_mail.add(source_id)
                        changed = True
                    if not already_refunded:
                        refunds.append(refund)
        if changed:
            commit_documents_with_wallet_adjustments(
                {"economy": payload, "mail": mail}, adjustments,
                legacy_path=economy_path(),
            )
        return refunds


def _activity_floor_cooldown_key(
    manager_id: int, person_id: int, centre: str, *, target_kind: str = "player",
) -> str:
    prefix = "staff:" if str(target_kind) == "staff" else ""
    return f"{prefix}{int(manager_id)}:{int(person_id)}:{str(centre)}"


def _assert_activity_floor_available(
    payload: dict[str, Any], manager_id: int, player_id: int,
    centre: str, game_date: str, *, target_kind: str = "player",
) -> tuple[str, date]:
    current_date = date.fromisoformat(str(game_date))
    cooldown_key = _activity_floor_cooldown_key(
        manager_id, player_id, centre, target_kind=target_kind,
    )
    previous = dict((payload.get("activity_floor_cooldowns") or {}).get(cooldown_key) or {})
    cooldown_until = str(previous.get("cooldown_until") or "")
    if cooldown_until and current_date < date.fromisoformat(cooldown_until):
        floor = str(ACTIVITY_CENTRES.get(centre, {}).get("floor") or centre)
        target_label = "职员" if str(target_kind) == "staff" else "球员"
        raise ValueError(f"该{target_label}的{floor}活动冷却至 {cooldown_until}")
    return cooldown_key, current_date


def _record_activity_floor_cooldown(
    payload: dict[str, Any], manager_id: int, player_id: int,
    centre: str, game_date: str, *, target_kind: str = "player",
) -> str:
    cooldown_key, current_date = _assert_activity_floor_available(
        payload, manager_id, player_id, centre, game_date,
        target_kind=target_kind,
    )
    cooldown_until = (current_date + timedelta(days=ACTIVITY_FLOOR_COOLDOWN_DAYS)).isoformat()
    payload.setdefault("activity_floor_cooldowns", {})[cooldown_key] = {
        "last_game_date": current_date.isoformat(),
        "cooldown_until": cooldown_until,
    }
    return cooldown_until


def assert_activity_floor_available(
    manager_id: int, player_id: int, centre: str, game_date: str,
) -> dict[str, Any]:
    with _LOCK:
        payload = load_economy()
        cooldown_key, _current_date = _assert_activity_floor_available(
            payload, manager_id, player_id, centre, game_date,
        )
        return {"cooldown_key": cooldown_key}


def record_activity_floor_cooldown(
    manager_id: int, player_id: int, centre: str, game_date: str,
) -> str:
    with _LOCK:
        payload = load_economy()
        cooldown_until = _record_activity_floor_cooldown(
            payload, manager_id, player_id, centre, game_date,
        )
        save_economy(payload)
        return cooldown_until


def reserve_activity_floor_cooldown(
    manager_id: int, player_id: int, centre: str, game_date: str,
) -> dict[str, Any]:
    cooldown_key = _activity_floor_cooldown_key(manager_id, player_id, centre)
    with _LOCK:
        payload = load_economy()
        previous = dict((payload.get("activity_floor_cooldowns") or {}).get(cooldown_key) or {})
        cooldown_until = _record_activity_floor_cooldown(
            payload, manager_id, player_id, centre, game_date,
        )
        save_economy(payload)
        return {
            "cooldown_key": cooldown_key, "game_date": str(game_date),
            "cooldown_until": cooldown_until, "previous": previous,
        }


def rollback_activity_floor_cooldown(reservation: dict[str, Any]) -> bool:
    cooldown_key = str(reservation.get("cooldown_key") or "")
    game_date = str(reservation.get("game_date") or "")
    if not cooldown_key or not game_date:
        return False
    with _LOCK:
        payload = load_economy()
        cooldowns = payload.setdefault("activity_floor_cooldowns", {})
        current = dict(cooldowns.get(cooldown_key) or {})
        if str(current.get("last_game_date") or "") != game_date:
            return False
        previous = dict(reservation.get("previous") or {})
        if previous:
            cooldowns[cooldown_key] = previous
        else:
            cooldowns.pop(cooldown_key, None)
        save_economy(payload)
        return True


def _language_level(
    player: dict[str, Any], language_id: int, language_name: str = "",
) -> int:
    expected_name = str(language_name or "").strip().casefold()
    for row in player.get("languages") or []:
        row_id = int(row.get("id") or 0)
        row_name = str(row.get("name") or "").strip().casefold()
        if row_id == int(language_id) or (expected_name and row_name == expected_name):
            return max(0, min(10, int(row.get("proficiency") or 0)))
    return 0


def _language_plan_key(
    person_id: int, language_id: int, person_kind: str = "player",
) -> str:
    if person_kind not in {"player", "staff"}:
        raise ValueError("语言学习人物类型无效")
    return f"{person_kind}:{int(person_id)}:{int(language_id)}"


def _legacy_language_plan_key(person_id: int, language_id: int) -> str:
    return f"{int(person_id)}:{int(language_id)}"


def _language_plan(
    state: dict[str, Any], person_id: int, language_id: int,
    person_kind: str = "player",
) -> tuple[str, dict[str, Any] | None]:
    plans = state.setdefault("plans", {})
    key = _language_plan_key(person_id, language_id, person_kind)
    plan = plans.get(key)
    if isinstance(plan, dict):
        return key, plan
    if person_kind == "player":
        legacy_key = _legacy_language_plan_key(person_id, language_id)
        legacy = plans.get(legacy_key)
        if isinstance(legacy, dict):
            legacy.setdefault("person_kind", "player")
            legacy.setdefault("person_id", int(person_id))
            legacy.setdefault("person_name", legacy.get("player_name"))
            return legacy_key, legacy
    return key, None


def language_instant_level_price(current_level: int) -> float:
    level = int(current_level)
    if not 0 <= level < 10:
        raise ValueError("语言熟练度必须在 0 到 9 之间")
    proportional = (
        LANGUAGE_INSTANT_MAX_PRICE
        * int(LANGUAGE_LEVEL_DAYS[level])
        / int(LANGUAGE_LEVEL_DAYS[-1])
    )
    return float(round(proportional / 1_000.0) * 1_000)


def language_instant_max_price(current_level: int) -> float:
    level = int(current_level)
    if not 0 <= level < 10:
        raise ValueError("语言熟练度必须在 0 到 9 之间")
    return float(sum(
        language_instant_level_price(stage) for stage in range(level, 10)
    ))


def _language_learning_state(payload: dict[str, Any]) -> dict[str, Any]:
    state = payload.setdefault("language_learning", {})
    auto = state.setdefault("auto", {"enabled": False})
    player_enabled = bool(auto.get("player_enabled", auto.get("enabled", False)))
    auto["enabled"] = player_enabled
    auto["player_enabled"] = player_enabled
    auto["staff_enabled"] = bool(auto.get("staff_enabled", False))
    auto.setdefault("player_language_id", int(auto.get("language_id") or 0))
    auto.setdefault("player_language_name", str(auto.get("language_name") or ""))
    auto.setdefault("staff_language_id", 0)
    auto.setdefault("staff_language_name", "")
    auto.setdefault("excluded_player_ids", [])
    auto.setdefault("excluded_staff_ids", [])
    state.setdefault("plans", {})
    state.setdefault("history", [])
    return state


def _language_auto_config(
    state: dict[str, Any], person_kind: str,
) -> dict[str, Any]:
    auto = state.setdefault("auto", {"enabled": False})
    prefix = "staff" if person_kind == "staff" else "player"
    return {
        "enabled": bool(auto.get(f"{prefix}_enabled", auto.get("enabled", False))),
        "language_id": int(auto.get(f"{prefix}_language_id") or (
            auto.get("language_id") if person_kind == "player" else 0
        ) or 0),
        "language_name": str(auto.get(f"{prefix}_language_name") or (
            auto.get("language_name") if person_kind == "player" else ""
        ) or ""),
        "excluded_ids": [
            int(person_id) for person_id in auto.get(f"excluded_{prefix}_ids") or []
            if int(person_id) > 0
        ],
    }


def _enroll_language_people(
    state: dict[str, Any], language_id: int, language_name: str,
    game_date: str, people: list[dict[str, Any]], source: str,
    *, person_kind: str = "player", excluded_ids: set[int] | None = None,
) -> dict[str, Any]:
    if person_kind not in {"player", "staff"}:
        raise ValueError("语言学习人物类型无效")
    current_date = date.fromisoformat(str(game_date))
    plans = state.setdefault("plans", {})
    enrolled: list[int] = []
    skipped: list[int] = []
    seen: set[int] = set()
    excluded = set(excluded_ids or set())
    for person in people:
        person_id = int(person.get("id") or 0)
        if person_id <= 0 or person_id in seen:
            continue
        seen.add(person_id)
        if person_id in excluded:
            skipped.append(person_id)
            continue
        level = _language_level(person, int(language_id), language_name)
        key, existing_plan = _language_plan(
            state, person_id, int(language_id), person_kind,
        )
        existing = dict(existing_plan or {})
        if (
            level >= 10 or existing.get("status") == "active"
            or (source == "auto" and existing.get("status") == "target_unavailable")
        ):
            skipped.append(person_id)
            continue
        plan = {
            "person_kind": person_kind,
            "person_id": person_id,
            "person_name": str(person.get("name") or person_id),
            "team_id": int(person.get("team_id") or 0),
            "team_name": str(person.get("team_name") or ""),
            "language_id": int(language_id),
            "language_name": str(language_name),
            "source": str(source),
            "status": "active",
            "started_on": current_date.isoformat(),
            "start_level": level,
            "current_level": level,
            "last_checked_on": current_date.isoformat(),
            "last_error": "",
        }
        if person_kind == "player":
            plan.update({"player_id": person_id, "player_name": plan["person_name"]})
        else:
            plan.update({"staff_id": person_id, "staff_name": plan["person_name"]})
        plans[_language_plan_key(person_id, int(language_id), person_kind)] = plan
        if key != _language_plan_key(person_id, int(language_id), person_kind):
            plans.pop(key, None)
        enrolled.append(person_id)
    return {"enrolled": enrolled, "skipped": skipped}


def _enroll_language_learning(
    language_id: int, language_name: str, game_date: str,
    people: list[dict[str, Any]], *, source: str, person_kind: str,
) -> dict[str, Any]:
    if int(language_id) <= 0 or not str(language_name or "").strip():
        raise ValueError("请选择有效的目标语言")
    if source not in {"manual", "auto"}:
        raise ValueError("语言学习来源无效")
    with _LOCK:
        payload = load_economy()
        _assert_activity_facility_owned(payload, "language_classroom")
        state = _language_learning_state(payload)
        result = _enroll_language_people(
            state, int(language_id), str(language_name), str(game_date),
            list(people), str(source), person_kind=person_kind,
        )
        if result["enrolled"]:
            auto = state.setdefault("auto", {"enabled": False})
            config = _language_auto_config(state, person_kind)
            if int(config["language_id"]) == int(language_id):
                prefix = "staff" if person_kind == "staff" else "player"
                excluded = {
                    int(person_id)
                    for person_id in auto.get(f"excluded_{prefix}_ids") or []
                    if int(person_id) not in set(result["enrolled"])
                }
                auto[f"excluded_{prefix}_ids"] = sorted(excluded)
            save_economy(payload)
        return {**result, "language_learning": _public_money_view(state)}


def enroll_language_learning(
    language_id: int, language_name: str, game_date: str,
    players: list[dict[str, Any]], *, source: str = "manual",
) -> dict[str, Any]:
    return _enroll_language_learning(
        language_id, language_name, game_date, players,
        source=source, person_kind="player",
    )


def enroll_staff_language_learning(
    language_id: int, language_name: str, game_date: str,
    staff: list[dict[str, Any]], *, source: str = "manual",
) -> dict[str, Any]:
    return _enroll_language_learning(
        language_id, language_name, game_date, staff,
        source=source, person_kind="staff",
    )


def _configure_automatic_language_learning(
    language_id: int, language_name: str, enabled: bool, game_date: str,
    people: list[dict[str, Any]], *, person_kind: str,
) -> dict[str, Any]:
    with _LOCK:
        payload = load_economy()
        _assert_activity_facility_owned(payload, "language_classroom")
        state = _language_learning_state(payload)
        auto = state.setdefault("auto", {"enabled": False})
        prefix = "staff" if person_kind == "staff" else "player"
        if enabled:
            if int(language_id) <= 0 or not str(language_name or "").strip():
                raise ValueError("请选择有效的目标语言")
            auto[f"{prefix}_enabled"] = True
            auto[f"{prefix}_language_id"] = int(language_id)
            auto[f"{prefix}_language_name"] = str(language_name)
            auto[f"excluded_{prefix}_ids"] = []
            auto[f"{prefix}_updated_on"] = date.fromisoformat(str(game_date)).isoformat()
            if person_kind == "player":
                auto.update({
                    "enabled": True, "language_id": int(language_id),
                    "language_name": str(language_name),
                })
            result = _enroll_language_people(
                state, int(language_id), str(language_name), str(game_date),
                list(people), "auto", person_kind=person_kind,
            )
        else:
            auto[f"{prefix}_enabled"] = False
            auto[f"{prefix}_updated_on"] = date.fromisoformat(str(game_date)).isoformat()
            if person_kind == "player":
                auto["enabled"] = False
            result = {"enrolled": [], "skipped": []}
        save_economy(payload)
        return {**result, "language_learning": _public_money_view(state)}


def configure_automatic_language_learning(
    language_id: int, language_name: str, enabled: bool, game_date: str,
    players: list[dict[str, Any]],
) -> dict[str, Any]:
    return _configure_automatic_language_learning(
        language_id, language_name, enabled, game_date, players,
        person_kind="player",
    )


def configure_automatic_staff_language_learning(
    language_id: int, language_name: str, enabled: bool, game_date: str,
    staff: list[dict[str, Any]],
) -> dict[str, Any]:
    return _configure_automatic_language_learning(
        language_id, language_name, enabled, game_date, staff,
        person_kind="staff",
    )


def _cancel_language_learning(
    language_id: int, game_date: str, person_ids: list[int],
    *, person_kind: str,
) -> dict[str, Any]:
    current_date = date.fromisoformat(str(game_date)).isoformat()
    selected = {int(person_id) for person_id in person_ids if int(person_id) > 0}
    if int(language_id) <= 0 or not selected:
        label = "职员" if person_kind == "staff" else "球员"
        raise ValueError(f"请至少选择一名正在学习的{label}")
    with _LOCK:
        payload = load_economy()
        _assert_activity_facility_owned(payload, "language_classroom")
        state = _language_learning_state(payload)
        cancelled: list[int] = []
        for person_id in sorted(selected):
            _key, plan = _language_plan(
                state, person_id, int(language_id), person_kind,
            )
            if not isinstance(plan, dict) or plan.get("status") != "active":
                continue
            plan.update({
                "status": "cancelled", "cancelled_on": current_date,
                "next_level_on": "", "last_checked_on": current_date,
                "last_error": "",
            })
            cancelled.append(person_id)
            history = {
                "person_kind": person_kind, "person_id": person_id,
                "person_name": str(plan.get("person_name") or person_id),
                "language_id": int(language_id),
                "language_name": str(plan.get("language_name") or ""),
                "status": "cancelled", "cancelled_on": current_date,
            }
            if person_kind == "player":
                history.update({"player_id": person_id, "player_name": history["person_name"]})
            else:
                history.update({"staff_id": person_id, "staff_name": history["person_name"]})
            state.setdefault("history", []).append(history)
        if not cancelled:
            label = "职员" if person_kind == "staff" else "球员"
            raise ValueError(f"所选{label}没有正在进行的该语言课程")
        auto = state.setdefault("auto", {"enabled": False})
        config = _language_auto_config(state, person_kind)
        if config["enabled"] and int(config["language_id"]) == int(language_id):
            prefix = "staff" if person_kind == "staff" else "player"
            excluded = {
                int(person_id) for person_id in auto.get(f"excluded_{prefix}_ids") or []
                if int(person_id) > 0
            }
            excluded.update(cancelled)
            auto[f"excluded_{prefix}_ids"] = sorted(excluded)
        state["history"] = list(state.get("history") or [])[-200:]
        save_economy(payload)
        return {
            "cancelled": cancelled,
            "language_learning": _public_money_view(state),
        }


def cancel_language_learning(
    language_id: int, game_date: str, player_ids: list[int],
) -> dict[str, Any]:
    return _cancel_language_learning(
        language_id, game_date, player_ids, person_kind="player",
    )


def cancel_staff_language_learning(
    language_id: int, game_date: str, staff_ids: list[int],
) -> dict[str, Any]:
    return _cancel_language_learning(
        language_id, game_date, staff_ids, person_kind="staff",
    )


def advance_language_learning_now(
    language_id: int, language_name: str, game_date: str,
    players: list[dict[str, Any]],
    apply_level: Callable[[dict[str, Any], int, int], dict[str, Any]],
    rollback_level: Callable[[dict[str, Any]], None],
    *, to_maximum: bool = False, person_kind: str = "player",
) -> dict[str, Any]:
    """Raise selected people immediately and commit payment and plans together."""
    if person_kind not in {"player", "staff"}:
        raise ValueError("语言学习人物类型无效")
    if int(language_id) <= 0 or not str(language_name or "").strip():
        raise ValueError("请选择有效的目标语言")
    current_date = date.fromisoformat(str(game_date)).isoformat()
    deduped: dict[int, dict[str, Any]] = {}
    for player in players:
        player_id = int(player.get("id") or 0)
        if player_id > 0 and player_id not in deduped:
            deduped[player_id] = player
    upgrades = [
        {
            "player": player,
            "player_id": int(player.get("id") or 0),
            "before": _language_level(player, int(language_id), str(language_name)),
        }
        for player in deduped.values()
    ]
    upgrades = [row for row in upgrades if int(row["before"]) < 10]
    if not upgrades:
        label = "职员" if person_kind == "staff" else "球员"
        raise ValueError(f"所选{label}已经精通该语言")
    for row in upgrades:
        row["after"] = 10 if to_maximum else int(row["before"]) + 1
        row["nominal_price"] = (
            language_instant_max_price(int(row["before"]))
            if to_maximum
            else language_instant_level_price(int(row["before"]))
        )
    with _LOCK:
        payload = load_economy()
        _assert_activity_facility_owned(payload, "language_classroom")
        free = bool(payload.get("free_services"))
        price = 0.0 if free else sum(
            local_purchase_price(float(row["nominal_price"])) for row in upgrades
        )
        transaction_type = (
            "language_learning_instant_max"
            if to_maximum else "language_learning_instant"
        )
        payment = _collect_combined_funds(
            payload, price, transaction_type,
            language_id=int(language_id), language_name=str(language_name),
            player_ids=[int(row["player_id"]) for row in upgrades],
            person_kind=person_kind,
            person_ids=[int(row["player_id"]) for row in upgrades],
            game_date=current_date, free=free, to_maximum=bool(to_maximum),
        )
        state = _language_learning_state(payload)
        rollback_results: list[dict[str, Any]] = []
        completed: list[int] = []
        try:
            for row in upgrades:
                target = int(row["after"])
                result = apply_level(row["player"], int(language_id), target)
                rollback_results.append(result)
                actual_before = int(result.get("before") or 0)
                actual_after = int(result.get("after") or actual_before)
                if actual_before != int(row["before"]) or actual_after != target:
                    label = "职员" if person_kind == "staff" else "球员"
                    raise RuntimeError(f"{label}语言熟练度已变化，请刷新后重试")
                _key, plan = _language_plan(
                    state, int(row["player_id"]), int(language_id), person_kind,
                )
                if isinstance(plan, dict) and plan.get("status") == "active":
                    plan.update({
                        "current_level": target, "start_level": target,
                        "started_on": current_date, "last_checked_on": current_date,
                        "last_error": "",
                        "next_level_on": (
                            (date.fromisoformat(current_date) + timedelta(
                                days=int(LANGUAGE_LEVEL_DAYS[target]),
                            )).isoformat() if target < 10 else ""
                        ),
                    })
                    if target >= 10:
                        plan["status"] = "completed"
                        plan["completed_on"] = current_date
                        completed.append(int(row["player_id"]))
                history = {
                    "person_kind": person_kind,
                    "person_id": int(row["player_id"]),
                    "person_name": str(row["player"].get("name") or row["player_id"]),
                    "language_id": int(language_id),
                    "language_name": str(language_name),
                    "status": "instant_max" if to_maximum else "instant",
                    "game_date": current_date,
                    "before": int(row["before"]), "after": target,
                    "price": 0.0 if free else local_purchase_price(float(row["nominal_price"])),
                }
                if person_kind == "player":
                    history.update({
                        "player_id": history["person_id"],
                        "player_name": history["person_name"],
                    })
                else:
                    history.update({
                        "staff_id": history["person_id"],
                        "staff_name": history["person_name"],
                    })
                state.setdefault("history", []).append(history)
            state["history"] = list(state.get("history") or [])[-200:]
            _save_economy_with_wallet_adjustments(payload, [{
                "amount": -payment["wallet"],
                "type": transaction_type,
                "metadata": {
                    "language_id": int(language_id),
                    "player_ids": [int(row["player_id"]) for row in upgrades],
                    "person_kind": person_kind,
                    "person_ids": [int(row["player_id"]) for row in upgrades],
                    "game_date": current_date,
                    "to_maximum": bool(to_maximum),
                },
            }])
        except Exception as error:
            rollback_failed = False
            for result in reversed(rollback_results):
                try:
                    rollback_level(result)
                except Exception:
                    rollback_failed = True
            if rollback_failed:
                raise RuntimeError("即时语言提升失败且内存回滚异常") from error
            raise
        for row in upgrades:
            _patch_profile_language(
                row["player"], int(language_id), str(language_name), int(row["after"]),
            )
        return {
            "upgraded": [{
                "person_kind": person_kind,
                "person_id": int(row["player_id"]),
                "player_id": int(row["player_id"]),
                "staff_id": int(row["player_id"]) if person_kind == "staff" else None,
                "before": int(row["before"]), "after": int(row["after"]),
                "price": 0.0 if free else local_purchase_price(float(row["nominal_price"])),
            } for row in upgrades],
            "completed": completed, "payment": payment,
            "language_learning": _public_money_view(state),
        }


def advance_staff_language_learning_now(
    language_id: int, language_name: str, game_date: str,
    staff: list[dict[str, Any]],
    apply_level: Callable[[dict[str, Any], int, int], dict[str, Any]],
    rollback_level: Callable[[dict[str, Any]], None],
    *, to_maximum: bool = False,
) -> dict[str, Any]:
    return advance_language_learning_now(
        language_id, language_name, game_date, staff, apply_level, rollback_level,
        to_maximum=to_maximum, person_kind="staff",
    )


def _planned_language_level(plan: dict[str, Any], game_date: str) -> tuple[int, str]:
    current = date.fromisoformat(str(game_date))
    started = date.fromisoformat(str(plan.get("started_on") or game_date))
    elapsed = max(0, (current - started).days)
    start_level = max(0, min(10, int(plan.get("start_level") or 0)))
    target = start_level
    cumulative = 0
    next_date = ""
    for level in range(start_level, 10):
        cumulative += int(LANGUAGE_LEVEL_DAYS[level])
        milestone = started + timedelta(days=cumulative)
        if elapsed >= cumulative:
            target = level + 1
        else:
            next_date = milestone.isoformat()
            break
    return target, next_date


def _patch_profile_language(
    player: dict[str, Any], language_id: int, language_name: str, level: int,
) -> None:
    rows = player.setdefault("languages", [])
    for row in rows:
        if (
            int(row.get("id") or 0) == int(language_id)
            or str(row.get("name") or "").strip().casefold()
            == str(language_name or "").strip().casefold()
        ):
            row.update({
                "id": int(language_id), "name": str(language_name),
                "proficiency": int(level), "maximum": 10,
            })
            return
    rows.append({
        "id": int(language_id), "name": str(language_name),
        "proficiency": int(level), "maximum": 10,
    })


def process_language_learning(
    game_date: str, players: list[dict[str, Any]],
    apply_level: Callable[[dict[str, Any], int, int], dict[str, Any]],
    rollback_level: Callable[[dict[str, Any]], None],
    *, staff: list[dict[str, Any]] | None = None,
    apply_staff_level: Callable[[dict[str, Any], int, int], dict[str, Any]] | None = None,
    rollback_staff_level: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Settle player and staff courses in one refresh transaction."""
    current_date = date.fromisoformat(str(game_date)).isoformat()
    with _LOCK:
        payload = load_economy()
        state = _language_learning_state(payload)
        state_before = json.dumps(state, ensure_ascii=False, sort_keys=True)
        if not _activity_facility_owned(payload, "language_classroom"):
            return {"enrolled": [], "advanced": [], "completed": [], "failed": []}
        def dedupe(people: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
            rows: dict[int, dict[str, Any]] = {}
            for person in people:
                person_id = int(person.get("id") or 0)
                if person_id <= 0:
                    continue
                previous = rows.get(person_id)
                if previous is None or str(person.get("team_type") or "club") == "club":
                    rows[person_id] = person
            return rows

        people_by_kind = {
            "player": dedupe(list(players)),
            "staff": dedupe(list(staff or [])),
        }
        callbacks = {
            "player": (apply_level, rollback_level),
            "staff": (apply_staff_level, rollback_staff_level),
        }
        enrolled_by_kind: dict[str, list[int]] = {"player": [], "staff": []}
        for person_kind in ("player", "staff"):
            config = _language_auto_config(state, person_kind)
            if not config["enabled"]:
                continue
            if callbacks[person_kind][0] is None:
                continue
            enrollment = _enroll_language_people(
                state, int(config["language_id"]), str(config["language_name"]),
                current_date, list(people_by_kind[person_kind].values()), "auto",
                person_kind=person_kind,
                excluded_ids=set(config["excluded_ids"]),
            )
            enrolled_by_kind[person_kind] = list(enrollment["enrolled"])

        advanced: list[dict[str, Any]] = []
        completed: list[int] = []
        failed: list[dict[str, Any]] = []
        rollback_results: list[tuple[str, dict[str, Any]]] = []
        for key, plan in list((state.get("plans") or {}).items()):
            if not isinstance(plan, dict) or plan.get("status") != "active":
                continue
            person_kind = str(plan.get("person_kind") or (
                "staff" if str(key).startswith("staff:") else "player"
            ))
            person_id = int(
                plan.get("person_id") or plan.get("staff_id")
                or plan.get("player_id") or 0
            )
            person = people_by_kind.get(person_kind, {}).get(person_id)
            apply_person, _rollback_person = callbacks.get(person_kind, (None, None))
            if person is None:
                plan["last_checked_on"] = current_date
                if person_kind == "staff":
                    plan["status"] = "target_unavailable"
                    plan["unavailable_on"] = current_date
                    plan["last_error"] = "职员已不在当前执教队伍，需重新确认后恢复"
                else:
                    plan["last_error"] = "球员暂不在当前执教队伍名单中"
                continue
            if apply_person is None:
                plan["last_checked_on"] = current_date
                plan["last_error"] = "当前人物类型的语言写入尚未启用"
                continue
            language_id = int(plan.get("language_id") or 0)
            language_name = str(plan.get("language_name") or "")
            actual = _language_level(person, language_id, language_name)
            planned, next_date = _planned_language_level(plan, current_date)
            target = max(actual, planned)
            if target > actual:
                try:
                    result = apply_person(person, language_id, target)
                except Exception as error:
                    plan["last_checked_on"] = current_date
                    plan["last_error"] = str(error)
                    failed.append({
                        "person_kind": person_kind, "person_id": person_id,
                        "player_id": person_id if person_kind == "player" else None,
                        "staff_id": person_id if person_kind == "staff" else None,
                        "error": str(error),
                    })
                    continue
                rollback_results.append((person_kind, result))
                actual = max(actual, int(result.get("after") or target))
                _patch_profile_language(person, language_id, language_name, actual)
                advanced.append({
                    "person_kind": person_kind, "person_id": person_id,
                    "player_id": person_id if person_kind == "player" else None,
                    "staff_id": person_id if person_kind == "staff" else None,
                    "language_id": language_id,
                    "before": int(result.get("before") or 0), "after": actual,
                })
            plan.update({
                "person_kind": person_kind, "person_id": person_id,
                "person_name": str(person.get("name") or plan.get("person_name") or person_id),
                "team_id": int(person.get("team_id") or plan.get("team_id") or 0),
                "team_name": str(person.get("team_name") or plan.get("team_name") or ""),
                "current_level": actual, "last_checked_on": current_date,
                "next_level_on": next_date, "last_error": "",
            })
            if person_kind == "player":
                plan.update({"player_id": person_id, "player_name": plan["person_name"]})
            else:
                plan.update({"staff_id": person_id, "staff_name": plan["person_name"]})
            if actual >= 10:
                plan["status"] = "completed"
                plan["completed_on"] = current_date
                plan["next_level_on"] = ""
                completed.append(person_id)
                history = {
                    "person_kind": person_kind, "person_id": person_id,
                    "person_name": plan.get("person_name"),
                    "language_id": language_id, "language_name": language_name,
                    "completed_on": current_date,
                }
                if person_kind == "player":
                    history.update({"player_id": person_id, "player_name": history["person_name"]})
                else:
                    history.update({"staff_id": person_id, "staff_name": history["person_name"]})
                state.setdefault("history", []).append(history)
        state["history"] = list(state.get("history") or [])[-200:]
        try:
            if json.dumps(state, ensure_ascii=False, sort_keys=True) != state_before:
                save_economy(payload)
        except Exception as error:
            rollback_failed = False
            for person_kind, result in reversed(rollback_results):
                try:
                    rollback = callbacks[person_kind][1]
                    if rollback is None:
                        raise RuntimeError("缺少语言写入回滚器")
                    rollback(result)
                except Exception:
                    rollback_failed = True
            if rollback_failed:
                raise RuntimeError("语言学习进度保存失败且内存回滚异常") from error
            raise
        return {
            "enrolled": enrolled_by_kind["player"],
            "staff_enrolled": enrolled_by_kind["staff"], "advanced": advanced,
            "completed": completed, "failed": failed,
            "language_learning": _public_money_view(state),
        }


def assert_media_activity_available(
    activity: str, target_id: int, game_date: str,
) -> dict[str, Any]:
    activity = str(activity or "")
    option = MEDIA_ACTIVITIES.get(activity)
    if not option:
        raise ValueError("媒体活动类型无效")
    assert_activity_centre_unlocked("media")
    usage_key = f"{activity}:{int(target_id)}"
    with _LOCK:
        payload = load_economy()
        media_usage = payload.setdefault("media_activity_daily_usage", {})
        if str(media_usage.get(usage_key) or "") == str(game_date):
            raise ValueError("该媒体活动今天已经执行过")
    return {**option, "usage_key": usage_key}


def record_media_activity(
    activity: str, target_id: int, target_name: str, game_date: str,
    effect: dict[str, Any],
) -> dict[str, Any]:
    option = assert_media_activity_available(activity, target_id, game_date)
    with _LOCK:
        payload = load_economy()
        payload.setdefault("media_activity_daily_usage", {})[option["usage_key"]] = str(game_date)
        row = {
            "id": str(uuid.uuid4()), "at": _now(), "activity": str(activity),
            "activity_name": str(option["name"]), "target_id": int(target_id),
            "target_name": str(target_name), "game_date": str(game_date),
            "effect": dict(effect),
        }
        payload.setdefault("activity_history", []).append(row)
        payload["activity_history"] = payload["activity_history"][-200:]
        save_economy(payload)
        return row


def record_international_activity(
    player_id: int, player_name: str, nation_id: int, nation_name: str,
    game_date: str, effect: dict[str, Any], slot: str = "secondary",
) -> dict[str, Any]:
    assert_activity_centre_unlocked("international")
    with _LOCK:
        payload = load_economy()
        row = {
            "id": str(uuid.uuid4()), "at": _now(), "activity": "naturalization",
            "activity_name": "国籍处理", "target_id": int(player_id),
            "target_name": str(player_name), "nation_id": int(nation_id),
            "nation_name": str(nation_name), "game_date": str(game_date),
            "slot": "primary" if str(slot) == "primary" else "secondary",
            "effect": dict(effect),
        }
        payload.setdefault("activity_history", []).append(row)
        payload["activity_history"] = payload["activity_history"][-200:]
        save_economy(payload)
        return row


def set_default_medical_treatment(mode: str | None) -> dict[str, Any]:
    mode = str(mode or "manual")
    if mode not in MEDICAL_DEFAULT_TREATMENTS:
        raise ValueError("默认治疗方式无效")
    with _LOCK:
        payload = load_economy()
        payload["medical_default_treatment"] = mode
        save_economy(payload)
        return welfare_status()


def perform_player_activity(
    manager_id: int, player_id: int, player_name: str, activity: str,
    game_date: str, current_intimacy: int,
    apply_effects: Callable[[int], dict[str, Any]],
) -> dict[str, Any]:
    option = PLAYER_ACTIVITIES.get(str(activity))
    if not option:
        raise ValueError("活动类型无效")
    key = f"{int(manager_id)}:{int(player_id)}"
    centre = str(option.get("centre") or "entertainment")
    daily_key = key if centre == "entertainment" else f"{key}:{centre}"
    with _LOCK:
        payload = load_economy()
        if centre == "entertainment":
            _assert_activity_facility_owned(payload, str(activity))
        else:
            centre_option = ACTIVITY_CENTRES[centre]
            if not _activity_centre_available(centre_option):
                raise ValueError(str(centre_option.get("unavailable_reason") or "该楼层暂未开启"))
            if not _activity_centre_owned(payload, centre):
                raise ValueError(f"请先解锁{centre_option['name']}")
        _assert_activity_floor_available(
            payload, manager_id, player_id, centre, game_date,
        )
        remainder = max(0.0, min(0.999999, float(payload["activity_progress"].get(key) or 0)))
        intimacy_full = int(current_intimacy) >= 100
        accumulated = remainder if intimacy_full else remainder + float(option["intimacy"])
        integer_points = 0 if intimacy_full else int(math.floor(accumulated + 1e-9))
        memory_result = apply_effects(integer_points)
        applied = int(memory_result.get("applied") or 0)
        if integer_points and applied <= 0:
            raise RuntimeError("亲密度整数写入未生效")
        remainder = round(accumulated - integer_points, 6)
        payload["activity_progress"][key] = remainder
        payload.setdefault("activity_daily_usage", {})[daily_key] = str(game_date)
        cooldown_until = _record_activity_floor_cooldown(
            payload, manager_id, player_id, centre, game_date,
        )
        payment = {"bank": 0.0, "wallet": 0.0, "total": 0.0}
        row = {
            "id": str(uuid.uuid4()), "at": _now(), "manager_id": int(manager_id),
            "player_id": int(player_id), "player_name": str(player_name),
            "activity": str(activity), "game_date": str(game_date),
            "intimacy_gain": float(option["intimacy"]),
            "intimacy_applied_gain": 0.0 if intimacy_full else float(option["intimacy"]),
            "integer_applied": applied, "fractional_remainder": remainder,
            "effect": dict(memory_result.get("effect") or {}),
            "cooldown_until": cooldown_until,
        }
        payload["activity_history"].append(row)
        payload["activity_history"] = payload["activity_history"][-200:]
        _save_economy_with_wallet_adjustments(payload, [{
            "amount": -payment["wallet"], "type": "player_activity",
            "metadata": {
                "manager_id": int(manager_id), "player_id": int(player_id),
                "activity": str(activity), "game_date": str(game_date),
            },
        }])
        return {**row, "intimacy": int(memory_result.get("after") or current_intimacy) + remainder, "payment": payment}


def perform_staff_activity(
    manager_id: int, staff_id: int, staff_name: str, activity: str,
    game_date: str, current_intimacy: int,
    apply_intimacy: Callable[[int], dict[str, Any]],
    rollback_intimacy: Callable[[dict[str, Any]], None],
    *, reverse_intimacy: int | None = None,
) -> dict[str, Any]:
    option = PLAYER_ACTIVITIES.get(str(activity))
    if not option or str(option.get("centre") or "") != "entertainment":
        raise ValueError("职员活动仅限娱乐中心")
    key = f"staff:{int(manager_id)}:{int(staff_id)}"
    with _LOCK:
        payload = load_economy()
        _assert_activity_facility_owned(payload, str(activity))
        _assert_activity_floor_available(
            payload, manager_id, staff_id, "entertainment", game_date,
            target_kind="staff",
        )
        remainder = max(
            0.0, min(0.999999, float(payload["activity_progress"].get(key) or 0)),
        )
        reverse_current = int(
            current_intimacy if reverse_intimacy is None else reverse_intimacy
        )
        intimacy_full = int(current_intimacy) >= 100 and reverse_current >= 100
        accumulated = remainder if intimacy_full else remainder + float(option["intimacy"])
        integer_points = 0 if intimacy_full else int(math.floor(accumulated + 1e-9))
        memory_result = apply_intimacy(integer_points) if integer_points else {
            "a_to_b": {
                "before": int(current_intimacy), "after": int(current_intimacy),
                "applied": 0,
            },
            "b_to_a": {
                "before": reverse_current, "after": reverse_current, "applied": 0,
            },
        }
        primary_result = memory_result.get("a_to_b")
        reverse_result = memory_result.get("b_to_a")
        if not isinstance(primary_result, dict) or not isinstance(reverse_result, dict):
            raise RuntimeError("职员活动必须返回完整的双向关系写入结果")

        def result_int(row: dict[str, Any], field: str, fallback: int) -> int:
            value = row.get(field)
            return fallback if value is None else int(value)

        primary_before = result_int(primary_result, "before", int(current_intimacy))
        primary_after = result_int(primary_result, "after", primary_before)
        primary_applied = result_int(primary_result, "applied", primary_after - primary_before)
        reverse_before = result_int(reverse_result, "before", reverse_current)
        reverse_after = result_int(reverse_result, "after", reverse_before)
        reverse_applied = result_int(reverse_result, "applied", reverse_after - reverse_before)
        if integer_points:
            if primary_before < 100 and primary_applied <= 0:
                raise RuntimeError("职员到主教练方向的亲密度写入未生效")
            if reverse_before < 100 and reverse_applied <= 0:
                raise RuntimeError("主教练到职员方向的亲密度写入未生效")
        applied = max(primary_applied, reverse_applied)
        next_remainder = round(accumulated - integer_points, 6)
        payload["activity_progress"][key] = next_remainder
        payload.setdefault("activity_daily_usage", {})[key] = str(game_date)
        cooldown_until = _record_activity_floor_cooldown(
            payload, manager_id, staff_id, "entertainment", game_date,
            target_kind="staff",
        )
        row = {
            "id": str(uuid.uuid4()), "at": _now(), "manager_id": int(manager_id),
            "staff_id": int(staff_id), "staff_name": str(staff_name),
            "activity": str(activity), "game_date": str(game_date),
            "intimacy_gain": float(option["intimacy"]),
            "intimacy_applied_gain": 0.0 if intimacy_full else float(option["intimacy"]),
            "integer_applied": applied, "fractional_remainder": next_remainder,
            "integer_applied_staff_to_manager": primary_applied,
            "integer_applied_manager_to_staff": reverse_applied,
            "relationship_directions": {
                "a_to_b": {
                    key: primary_result[key] for key in (
                        "before", "after", "applied", "reason", "new_record",
                        "relation_type", "permanence", "permanent",
                    ) if key in primary_result
                },
                "b_to_a": {
                    key: reverse_result[key] for key in (
                        "before", "after", "applied", "reason", "new_record",
                        "relation_type", "permanence", "permanent",
                    ) if key in reverse_result
                },
            },
            "cooldown_until": cooldown_until, "target_kind": "staff",
        }
        payload["activity_history"].append(row)
        payload["activity_history"] = payload["activity_history"][-200:]
        try:
            _save_economy_with_wallet_adjustments(payload, [{
                "amount": 0.0, "type": "staff_activity",
                "metadata": {
                    "manager_id": int(manager_id), "staff_id": int(staff_id),
                    "activity": str(activity), "game_date": str(game_date),
                },
            }])
        except Exception:
            if integer_points:
                rollback_intimacy(memory_result)
            raise
        return {
            **row,
            "intimacy_before": primary_before,
            "intimacy_after": primary_after,
            "intimacy": primary_after + next_remainder,
            "reverse_intimacy_before": reverse_before,
            "reverse_intimacy_after": reverse_after,
            "reverse_intimacy": reverse_after + next_remainder,
            "payment": {"bank": 0.0, "wallet": 0.0, "total": 0.0},
        }


def psychological_counseling_outcome(roll: float | None = None) -> str:
    value = random.random() if roll is None else float(roll)
    if not 0 <= value < 1:
        raise ValueError("心理辅导概率值无效")
    if value < 0.40:
        return "relieved"
    if value < 0.65:
        return "opened_up"
    if value < 0.95:
        return "no_effect"
    return "rupture"


def perform_psychological_counseling(
    manager_id: int, player_id: int, player_name: str, game_date: str,
    current_intimacy: int,
    apply_effects: Callable[[str, int], dict[str, Any]],
    *, roll: float | None = None, forced_outcome: str | None = None,
) -> dict[str, Any]:
    key = f"{int(manager_id)}:{int(player_id)}"
    current_date = date.fromisoformat(str(game_date))
    with _LOCK:
        payload = load_economy()
        _assert_activity_facility_owned(payload, "psychological_counseling")
        _assert_activity_floor_available(
            payload, manager_id, player_id, "talk_room", game_date,
        )
        counseling = payload.setdefault("psychological_counseling", {})

        outcome = str(forced_outcome or psychological_counseling_outcome(roll))
        if outcome not in PSYCHOLOGICAL_COUNSELING_OUTCOMES:
            raise ValueError("心理辅导结果无效")
        option = PSYCHOLOGICAL_COUNSELING_OUTCOMES[outcome]
        remainder = max(0.0, min(0.999999, float(payload["activity_progress"].get(key) or 0)))
        intimacy_gain = float(option["intimacy"])
        integer_points = 0
        next_remainder = remainder
        if intimacy_gain > 0 and int(current_intimacy) < 100:
            accumulated = remainder + intimacy_gain
            integer_points = int(math.floor(accumulated + 1e-9))
            next_remainder = round(accumulated - integer_points, 6)
        elif intimacy_gain < 0:
            integer_points = int(intimacy_gain)

        memory_result = apply_effects(outcome, integer_points)
        applied = int(memory_result.get("applied") or 0)
        if integer_points > 0 and int(current_intimacy) < 100 and applied <= 0:
            raise RuntimeError("亲密度整数写入未生效")
        if integer_points < 0 and int(current_intimacy) > 0 and applied >= 0:
            raise RuntimeError("亲密度扣减未生效")

        payload["activity_progress"][key] = next_remainder
        cooldown = _record_activity_floor_cooldown(
            payload, manager_id, player_id, "talk_room", game_date,
        )
        counseling[key] = {
            "last_game_date": current_date.isoformat(),
            "last_outcome": outcome,
            "cooldown_until": cooldown,
        }
        dialogue = random.choice(option["dialogues"])
        effect = dict(memory_result.get("effect") or {})
        row = {
            "id": str(uuid.uuid4()), "at": _now(), "manager_id": int(manager_id),
            "player_id": int(player_id), "player_name": str(player_name),
            "activity": "psychological_counseling", "game_date": current_date.isoformat(),
            "outcome": outcome, "outcome_name": str(option["name"]),
            "reply": f"{player_name}：{dialogue}",
            "intimacy_gain": intimacy_gain,
            "intimacy_applied_gain": 0.0 if int(current_intimacy) >= 100 and intimacy_gain > 0 else intimacy_gain,
            "integer_applied": applied, "fractional_remainder": next_remainder,
            "cooldown_until": cooldown, "effect": effect,
        }
        payload["activity_history"].append(row)
        payload["activity_history"] = payload["activity_history"][-200:]
        save_economy(payload)
        return {
            **row,
            "intimacy": int(memory_result.get("after", current_intimacy)) + next_remainder,
        }


def set_medical_treatment(
    player_id: int, player_name: str, team_id: int, player_address: str,
    mode: str | None, game_date: str,
    transform: Callable[[float, float], dict[str, Any]],
) -> dict[str, Any]:
    mode = str(mode or "none")
    if mode not in {"none", *MEDICAL_TREATMENTS}:
        raise ValueError("治疗方式无效")
    key = str(int(player_id))
    with _LOCK:
        payload = load_economy()
        free_services = bool(payload.get("free_services"))
        treatments = payload["medical_treatments"]
        previous = dict(treatments.get(key) or {})
        old_mode = str(previous.get("mode") or "none")
        if old_mode == mode:
            return previous
        old_factor = float((MEDICAL_TREATMENTS.get(old_mode) or {}).get("factor") or 1.0)
        new_option = MEDICAL_TREATMENTS.get(mode)
        new_factor = float((new_option or {}).get("factor") or 1.0)
        previous_charge = 0.0 if free_services else (
            float(previous.get("charged_today") or 0)
            if str(previous.get("last_charged_date") or "") == str(game_date) else 0.0
        )
        daily_cost = float((new_option or {}).get("daily_cost") or 0)
        charge = 0.0 if free_services else max(0.0, daily_cost - previous_charge)
        if (
            _bank_balance_minor(payload) + to_minor(available_balance())
            < to_minor(charge)
        ):
            raise ValueError("银行与钱包余额合计不足")
        result = transform(old_factor, new_factor)
        payment = _collect_combined_funds(
            payload, charge, "medical_treatment",
            player_id=int(player_id), player_name=str(player_name), mode=mode,
            game_date=str(game_date), free=free_services,
        )
        if mode == "none":
            treatments.pop(key, None)
            _save_economy_with_wallet_adjustments(payload, [{
                "amount": -payment["wallet"], "type": "medical_treatment",
                "metadata": {"player_id": int(player_id), "mode": mode},
            }])
            return {"player_id": int(player_id), "mode": "none", "payment": payment, **result}
        injuries = list(result.get("injuries") or [])
        expected_return_to = max(
            (str(item.get("estimated_return_to") or "") for item in injuries), default="",
        )
        state = {
            "player_id": int(player_id), "player_name": str(player_name),
            "team_id": int(team_id), "player_address": str(player_address),
            "mode": mode, "factor": new_factor, "daily_cost": daily_cost,
            "started_on": previous.get("started_on") or str(game_date),
            "last_charged_date": str(game_date),
            "charged_today": 0.0 if free_services else max(previous_charge, daily_cost),
            "expected_return_to": expected_return_to,
            "injury_start_dates": sorted({
                str(item.get("start_date")) for item in injuries
                if item.get("start_date")
            }),
        }
        treatments[key] = state
        _save_economy_with_wallet_adjustments(payload, [{
            "amount": -payment["wallet"], "type": "medical_treatment",
            "metadata": {"player_id": int(player_id), "mode": mode},
        }])
        return {**state, "payment": payment, "injuries": injuries}


def apply_default_medical_treatments(
    game_date: str, candidates: list[dict[str, Any]],
    transform: Callable[[dict[str, Any], float, float], dict[str, Any]],
    *, status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status is None:
        status = medical_status()
    mode = str(status.get("default_treatment") or "manual")
    if mode in {"manual", "none"}:
        return {"mode": mode, "applied": [], "skipped": [], "errors": []}
    existing = {
        int(item.get("player_id") or key)
        for key, item in (status.get("treatments") or {}).items()
    }
    applied: list[int] = []
    skipped: list[int] = []
    errors: list[dict[str, Any]] = []
    for player in candidates:
        player_id = int(player.get("id") or 0)
        if (
            player_id <= 0
            or player_id in existing
            or int((player.get("availability") or {}).get("injury_count") or 0) <= 0
        ):
            continue
        try:
            set_medical_treatment(
                player_id, str(player.get("name") or player_id),
                int(player.get("team_id") or 0), str(player.get("address") or ""),
                mode, game_date,
                lambda old_factor, new_factor, row=player: transform(
                    row, old_factor, new_factor,
                ),
            )
            existing.add(player_id)
            applied.append(player_id)
        except ValueError as error:
            skipped.append(player_id)
            errors.append({"player_id": player_id, "error": str(error)})
        except Exception as error:
            errors.append({"player_id": player_id, "error": str(error)})
    return {"mode": mode, "applied": applied, "skipped": skipped, "errors": errors}


def process_medical_treatments(
    game_date: str, injured_player_ids: set[int],
    cancel_treatment: Callable[[dict[str, Any]], None],
    complete_treatment: Callable[[dict[str, Any]], None] | None = None,
    *, resolved_player_ids: set[int] | None = None,
) -> dict[str, Any]:
    current = date.fromisoformat(str(game_date))
    with _LOCK:
        payload = load_economy()
        free_services = bool(payload.get("free_services"))
        treatments = payload["medical_treatments"]
        charged_minor = 0
        cancelled: list[int] = []
        completed: list[int] = []
        pending: list[int] = []
        wallet_adjustments: list[dict[str, Any]] = []
        changed = False
        for key, state in list(treatments.items()):
            player_id = int(state.get("player_id") or key)
            if resolved_player_ids is not None and player_id not in resolved_player_ids:
                pending.append(player_id)
                continue
            if player_id not in injured_player_ids:
                if complete_treatment is not None:
                    complete_treatment(state)
                treatments.pop(key, None)
                completed.append(player_id)
                changed = True
                continue
            try:
                last = date.fromisoformat(str(state.get("last_charged_date") or game_date))
            except ValueError:
                last = current
            due_days = max(0, (current - last).days)
            if not due_days:
                continue
            if free_services:
                state["last_charged_date"] = str(current)
                write_minor(state, "charged_today", 0)
                treatments[key] = state
                changed = True
                continue
            daily_cost_minor = to_minor(state.get("daily_cost") or 0)
            due_minor = daily_cost_minor * due_days
            due = from_minor(due_minor)
            available_minor = _bank_balance_minor(payload) + to_minor(available_balance())
            if available_minor < due_minor:
                affordable_days = (
                    available_minor // daily_cost_minor
                    if daily_cost_minor > 0 else due_days
                )
                if affordable_days:
                    payment = _collect_combined_funds(
                        payload, from_minor(affordable_days * daily_cost_minor),
                        "medical_treatment_daily",
                        player_id=player_id, player_name=state.get("player_name"),
                        mode=state.get("mode"), through_date=str(current),
                    )
                    charged_minor += to_minor(payment["total"])
                    wallet_adjustments.append({
                        "amount": -payment["wallet"],
                        "type": "medical_treatment_daily",
                        "metadata": {"player_id": player_id, "through_date": str(current)},
                    })
                cancel_treatment(state)
                treatments.pop(key, None)
                cancelled.append(player_id)
                changed = True
                continue
            payment = _collect_combined_funds(
                payload, due, "medical_treatment_daily",
                player_id=player_id, player_name=state.get("player_name"),
                mode=state.get("mode"), through_date=str(current),
            )
            charged_minor += to_minor(payment["total"])
            wallet_adjustments.append({
                "amount": -payment["wallet"],
                "type": "medical_treatment_daily",
                "metadata": {"player_id": player_id, "through_date": str(current)},
            })
            state["last_charged_date"] = str(current)
            write_minor(state, "charged_today", daily_cost_minor)
            treatments[key] = state
            changed = True
        if changed:
            _save_economy_with_wallet_adjustments(payload, wallet_adjustments)
        return {
            "charged": from_minor(charged_minor), "cancelled": cancelled,
            "completed": completed, "pending": pending,
        }


def set_free_services(enabled: bool) -> dict[str, Any]:
    with _LOCK:
        payload = load_economy()
        payload["free_services"] = bool(enabled)
        if enabled:
            for state in payload["medical_treatments"].values():
                write_minor(state, "charged_today", 0)
        save_economy(payload)
        return welfare_status()


def free_services_enabled() -> bool:
    with _LOCK:
        return bool(load_economy().get("free_services"))


def _previous_month(value: str) -> str:
    try:
        first = date.fromisoformat(f"{str(value)[:7]}-01")
    except ValueError:
        return ""
    return (first - timedelta(days=1)).strftime("%Y-%m")


def club_dividend_forecast(
    current_month: str, club: dict[str, Any], metrics: dict[str, Any],
    baseline: str,
) -> dict[str, Any] | None:
    """Estimate this month's dividend from the latest closed calendar month."""
    profit_month = _previous_month(current_month)
    if not profit_month:
        return None
    row = next((
        item for item in reversed(metrics.get("monthly_profits") or [])
        if str(item.get("month") or "") == profit_month
    ), None)
    source = "monthly_summary"
    if row is None and metrics.get("last_month_profit") is not None:
        row = {"profit": metrics.get("last_month_profit")}
        source = "income_statement"
    if row is None:
        return None
    profit = int(row.get("profit") or 0)
    return {
        "team_id": int(club.get("id") or 0),
        "team_name": str(club.get("name") or ""),
        "month": current_month,
        "profit_month": profit_month,
        "profit": profit,
        "profit_source": source,
        "rate": CLUB_DIVIDEND_RATE,
        "estimated_amount": from_minor(
            multiply_minor(to_minor(max(0, profit)), CLUB_DIVIDEND_RATE),
        ),
        "eligible": current_month > baseline,
    }


def _club_dividend_status(payload: dict[str, Any]) -> dict[str, Any]:
    stored_forecasts = list(payload.get("club_dividend_forecasts", {}).values())
    forecasts = [_public_money_view(row) for row in stored_forecasts]
    forecasts.sort(key=lambda row: str(row.get("team_name") or ""))
    payments = _public_money_view(list(reversed(
        payload.get("club_dividend_payments", [])[-20:]
    )))
    settlement_month = str(payload.get("club_dividend_last_settlement_month") or "")
    last_paid_month = _previous_month(settlement_month)
    last_paid_total = from_minor(sum(
        read_minor(row, "amount")
        for row in payload.get("club_dividend_payments", [])
        if str(row.get("month") or "") == last_paid_month
    ))
    return {
        "rate": CLUB_DIVIDEND_RATE,
        "estimated_total": from_minor(sum(
            read_minor(row, "estimated_amount")
            for row in stored_forecasts if row.get("eligible")
        )),
        "forecast_month": max(
            (str(row.get("month") or "") for row in forecasts), default="",
        ),
        "forecasts": forecasts,
        "payments": payments,
        "last_paid_month": last_paid_month,
        "last_paid_total": last_paid_total,
        "total_paid": from_minor(sum(
            read_minor(row, "amount")
            for row in payload.get("club_dividend_payments", [])
        )),
    }


def settle_club_dividends(
    game_date: str, clubs: list[dict[str, Any]],
    metrics: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Credit 5% of each fully-owned club's closed monthly profit once."""
    try:
        current_date = date.fromisoformat(str(game_date))
        current_month = current_date.strftime("%Y-%m")
        scheduled_payday = current_date.replace(day=1).isoformat()
    except ValueError:
        with _LOCK:
            status = _club_dividend_status(load_economy())
        status.update({"paid_count": 0, "paid_amount": 0.0, "new_payments": []})
        return status
    with _LOCK:
        payload = load_economy()
        baselines = payload["club_dividend_baselines"]
        checked = payload["club_dividend_checked_through"]
        forecasts: dict[str, dict[str, Any]] = {}
        paid: list[dict[str, Any]] = []
        changed = False
        if payload.get("club_dividend_last_settlement_month") != current_month:
            payload["club_dividend_last_settlement_month"] = current_month
            changed = True

        for club in clubs:
            team_id = int(club.get("id") or 0)
            if team_id <= 0:
                continue
            key = str(team_id)
            team_name = str(club.get("name") or team_id)
            acquired_month = str(club.get("acquired_game_date") or "")[:7]
            if len(acquired_month) != 7:
                acquired_month = current_month
            if key not in baselines:
                baselines[key] = acquired_month
                checked[key] = acquired_month
                changed = True
            baseline = str(baselines.get(key) or current_month)
            checked_through = str(checked.get(key) or baseline)
            rows = sorted(
                (
                    row for row in metrics.get(team_id, {}).get("monthly_profits", [])
                    if len(str(row.get("month") or "")) == 7
                ),
                key=lambda row: str(row.get("month") or ""),
            )
            closed_month = _previous_month(current_month)
            if (
                closed_month
                and not any(str(row.get("month") or "") == closed_month for row in rows)
                and metrics.get(team_id, {}).get("last_month_profit") is not None
            ):
                rows.append({
                    "month": closed_month,
                    "profit": int(metrics[team_id]["last_month_profit"]),
                })
                rows.sort(key=lambda row: str(row.get("month") or ""))
            forecast = club_dividend_forecast(
                current_month, club, metrics.get(team_id, {}), baseline,
            )
            if forecast is not None:
                forecasts[key] = forecast

            for row in rows:
                month = str(row.get("month") or "")
                if month >= current_month or month <= checked_through:
                    continue
                profit = int(row.get("profit") or 0)
                if month > baseline and profit > 0:
                    amount_minor = multiply_minor(to_minor(profit), CLUB_DIVIDEND_RATE)
                    amount = from_minor(amount_minor)
                    _set_bank_balance_minor(
                        payload, _bank_balance_minor(payload) + amount_minor,
                    )
                    payment = {
                        "id": str(uuid.uuid4()),
                        "team_id": team_id,
                        "team_name": team_name,
                        "month": month,
                        "profit": profit,
                        "rate": CLUB_DIVIDEND_RATE,
                        "amount": amount,
                        "amount_minor": amount_minor,
                        "paid_on": scheduled_payday,
                        "recorded_at": _now(),
                    }
                    payload["club_dividend_payments"].append(payment)
                    _transaction(
                        payload, "club_dividend", amount,
                        team_id=team_id, team_name=team_name,
                        dividend_month=month, monthly_profit=profit,
                        dividend_rate=CLUB_DIVIDEND_RATE,
                        game_date=str(game_date),
                    )
                    paid.append(payment)
                checked_through = month
                checked[key] = month
                changed = True

        if payload.get("club_dividend_forecasts") != forecasts:
            payload["club_dividend_forecasts"] = forecasts
            changed = True
        payload["club_dividend_payments"] = payload["club_dividend_payments"][-500:]
        if changed:
            save_economy(payload)
        status = _club_dividend_status(payload)
        status.update({
            "paid_count": len(paid),
            "paid_amount": from_minor(sum(read_minor(row, "amount") for row in paid)),
            "new_payments": _public_money_view(paid),
        })
        return status


def forget_club_dividend_tracking(team_id: int) -> None:
    """Stop future dividend settlement for a sold club without deleting payments."""
    key = str(int(team_id))
    with _LOCK:
        payload = load_economy()
        changed = False
        for field in (
            "club_dividend_baselines",
            "club_dividend_checked_through",
            "club_dividend_forecasts",
        ):
            if key in payload[field]:
                payload[field].pop(key, None)
                changed = True
        if changed:
            save_economy(payload)


def public_economy() -> dict[str, Any]:
    with _LOCK:
        payload = load_economy()
        wallet = load_wallet()
        return _public_economy_payload(
            payload, from_minor(read_minor(wallet, "balance")),
            wallet.get("transactions", []),
        )


def _public_economy_payload(
    payload: dict[str, Any], casino_balance: float,
    wallet_transactions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    casino_balance_minor = to_minor(casino_balance)
    casino_balance = from_minor(casino_balance_minor)
    bank_balance_minor = _bank_balance_minor(payload)
    transactions = list(reversed(payload["transactions"][-100:]))
    for row in transactions:
        if isinstance(row, dict):
            row["display_type"] = transaction_label(row.get("type"), row)
    if wallet_transactions is None:
        try:
            wallet_transactions = load_wallet().get("transactions", [])
        except (OSError, RuntimeError, TypeError, ValueError):
            # Mutation responses must still return their committed bank state
            # if a legacy/mock wallet projection is temporarily unavailable.
            wallet_transactions = []
    statement = _build_account_statement(
        payload["transactions"], wallet_transactions,
    )
    return {
        "casino_balance": casino_balance,
        "bank_balance": from_minor(bank_balance_minor),
        "total_balance": from_minor(bank_balance_minor + casino_balance_minor),
        "canteen_plan": str(payload.get("canteen_plan") or "basic"),
        "credit": _public_money_view(payload["credit"]),
        "default_notice": _public_money_view(next((
            row for row in reversed(payload["default_notices"])
            if not row.get("acknowledged")
        ), None)),
        "inventory": _public_money_view(list(payload["inventory"])),
        "transactions": _public_money_view(transactions),
        "statement": _public_money_view(statement),
        "dividends": _club_dividend_status(payload),
        "catalog": _shop_catalog(),
    }


@lru_cache(maxsize=64)
def _account_statement_rows(
    transaction_signature: tuple[tuple[str, int], ...], fallback_month: str,
) -> tuple[str, tuple[tuple[str, int, int, int, int], ...]]:
    """Aggregate the only transaction fields that affect the public statement."""
    months: dict[str, dict[str, Any]] = {}
    for raw_date, amount_minor in transaction_signature:
        try:
            month = date.fromisoformat(raw_date).strftime("%Y-%m")
        except ValueError:
            month = fallback_month
        bucket = months.setdefault(month, {"month": month, "income_minor": 0, "expense_minor": 0, "net_minor": 0, "count": 0})
        if amount_minor == 0:
            continue
        bucket["income_minor"] += max(0, amount_minor)
        bucket["expense_minor"] += max(0, -amount_minor)
        bucket["net_minor"] += amount_minor
        bucket["count"] += 1
    ordered = tuple(
        (
            str(item["month"]), int(item["income_minor"]),
            int(item["expense_minor"]), int(item["net_minor"]),
            int(item["count"]),
        )
        for item in sorted(
            months.values(), key=lambda item: item["month"], reverse=True,
        )
        if item["net_minor"] != 0
    )[:12]
    current_month = ordered[0][0] if ordered else fallback_month
    return current_month, ordered


def _build_account_statement(
    transactions: list[dict[str, Any]],
    wallet_transactions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a compact unified statement for the main economy state payload."""
    return _build_unified_account_statement(transactions, wallet_transactions)


def _build_unified_account_statement(
    transactions: list[dict[str, Any]],
    wallet_transactions: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    fallback_month = date.today().strftime("%Y-%m")
    entries: list[dict[str, Any]] = []
    mirrored_wallet_types = _mirrored_wallet_types(transactions)
    for default_account, rows in (
        ("bank", transactions), ("wallet", wallet_transactions or []),
    ):
        for row in rows:
            if not isinstance(row, dict):
                continue
            kind = str(row.get("type") or "account_adjustment")
            if default_account == "wallet" and kind in {
                *_INTERNAL_TRANSFER_TYPES, *mirrored_wallet_types,
            }:
                continue
            amount_minor = read_minor(row, "amount")
            if not amount_minor:
                continue
            internal = kind in _INTERNAL_TRANSFER_TYPES
            account = default_account
            if default_account == "bank" and internal:
                account = "transfer"
            elif default_account == "bank" and any(
                read_minor(row, field)
                for field in (
                    "wallet_used", "wallet_refund", "wallet_credit",
                    "wallet_deducted",
                )
            ):
                account = "combined"
            raw_date = str(row.get("game_date") or row.get("at") or "")[:10]
            try:
                month = date.fromisoformat(raw_date).strftime("%Y-%m")
            except ValueError:
                month = fallback_month
            entries.append({
                "date": raw_date,
                "month": month,
                "amount": from_minor(amount_minor),
                "account": account,
                "category": transaction_category(kind),
                "is_internal_transfer": internal,
            })
    signature = tuple(
        (
            str(entry.get("date") or ""),
            0 if entry.get("is_internal_transfer") else to_minor(entry.get("amount") or 0),
        )
        for entry in entries
    )
    current_month, cached_rows = _account_statement_rows(signature, fallback_month)
    ordered = []
    for month, income_minor, expense_minor, _net_minor, count in cached_rows:
        income = from_minor(income_minor)
        expense = from_minor(expense_minor)
        ordered.append({
            "month": month,
            "income": income,
            "expense": expense,
            "net": round_money(income - expense),
            "count": count,
        })
    current = next(
        (item for item in ordered if item["month"] == current_month), None,
    )
    return {
        "schema_version": 2,
        "current_month": current_month,
        "current": current or {
            "month": current_month, "income": 0.0, "expense": 0.0,
            "net": 0.0, "count": 0,
        },
        "months": ordered,
        "summary": _ledger_summary(entries),
        "categories": [
            {"value": key, "label": label}
            for key, label in LEDGER_CATEGORY_LABELS.items()
            if any(entry.get("category") == key for entry in entries)
        ],
    }


def account_statement_page(
    *, page: int = 1, page_size: int = 25, month: str = "",
    account: str = "", category: str = "", direction: str = "",
    search: str = "",
) -> dict[str, Any]:
    """Return a save-scoped, filterable bank + wallet ledger projection."""
    page = max(1, int(page))
    page_size = min(100, max(10, int(page_size)))
    month = str(month or "").strip()
    account = str(account or "").strip().lower()
    category = str(category or "").strip().lower()
    direction = str(direction or "").strip().lower()
    search = str(search or "").strip().casefold()
    if account and account not in {*LEDGER_ACCOUNT_LABELS, "all"}:
        raise ValueError("账单账户筛选无效")
    if category and category not in {*LEDGER_CATEGORY_LABELS, "all"}:
        raise ValueError("账单类别筛选无效")
    if direction and direction not in {"all", "income", "expense", "transfer"}:
        raise ValueError("账单方向筛选无效")
    if month and month != "all":
        try:
            datetime.strptime(month, "%Y-%m")
        except ValueError as error:
            raise ValueError("账单月份筛选无效") from error

    with _LOCK:
        economy = load_economy()
        wallet = load_wallet()
        all_entries = _build_ledger_entries(
            economy.get("transactions", []), wallet.get("transactions", []),
        )
    categories = [
        {
            "value": key, "label": label,
            "count": sum(entry.get("category") == key for entry in all_entries),
        }
        for key, label in LEDGER_CATEGORY_LABELS.items()
        if any(entry.get("category") == key for entry in all_entries)
    ]

    def included(entry: dict[str, Any], *, include_month: bool = True) -> bool:
        if (
            include_month and month not in {"", "all"}
            and entry.get("month") != month
        ):
            return False
        if account not in {"", "all"} and entry.get("account") != account:
            return False
        if category not in {"", "all"} and entry.get("category") != category:
            return False
        if direction not in {"", "all"} and entry.get("direction") != direction:
            return False
        if search:
            haystack = " ".join(str(entry.get(key) or "") for key in (
                "label", "description", "type", "account_label", "category_label",
                "date", "month",
            )).casefold()
            if search not in haystack:
                return False
        return True

    month_entries: dict[str, list[dict[str, Any]]] = {}
    for entry in all_entries:
        value = str(entry.get("month") or "")
        if value and included(entry, include_month=False):
            month_entries.setdefault(value, []).append(entry)
    months = []
    for value in sorted(month_entries, reverse=True):
        month_summary = _ledger_summary(month_entries[value])
        months.append({
            "value": value,
            "count": month_summary["count"],
            "income": month_summary["income"],
            "expense": month_summary["expense"],
            "net": month_summary["net"],
            "transfers": month_summary["transfers"],
        })

    filtered = [entry for entry in all_entries if included(entry)]
    total = len(filtered)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    start = (page - 1) * page_size
    return {
        "schema_version": 2,
        "summary": _ledger_summary(filtered),
        "months": months,
        "categories": categories,
        "accounts": [
            {"value": key, "label": label}
            for key, label in LEDGER_ACCOUNT_LABELS.items()
        ],
        "entries": _public_money_view(filtered[start:start + page_size]),
        "pagination": {
            "page": page, "page_size": page_size, "total": total,
            "total_pages": total_pages,
        },
        "filters": {
            "month": month or "all", "account": account or "all",
            "category": category or "all", "direction": direction or "all",
            "search": search,
        },
    }


def grant_bankruptcy_relief() -> dict[str, Any]:
    """Coordinate the bank-side check with the atomic wallet-side relief grant."""
    with _LOCK:
        payload = load_economy()
        result = grant_wallet_bankruptcy_relief(
            from_minor(_bank_balance_minor(payload)),
        )
        if result.get("granted"):
            _transaction(
                payload, "bankruptcy_relief", float(result["amount"]),
                wallet_credit=float(result["amount"]),
                wallet_transaction_id=str(result["transaction_id"]),
            )
            save_economy(payload)
        return result


def _monthly_payday(year: int, month: int) -> date:
    return date(year, month, 1)


def next_salary_payday(game_date: str, schedule: str | None) -> str | None:
    current = date.fromisoformat(str(game_date))
    if schedule == "weekly":
        days = (6 - current.weekday()) % 7
        if days == 0:
            days = 7
        return (current + timedelta(days=days)).isoformat()
    if schedule == "monthly":
        candidate = _monthly_payday(current.year, current.month)
        if candidate <= current:
            year = current.year + (1 if current.month == 12 else 0)
            month = 1 if current.month == 12 else current.month + 1
            candidate = _monthly_payday(year, month)
        return candidate.isoformat()
    return None


def salary_status(game_date: str, gross_weekly: float) -> dict[str, Any]:
    with _LOCK:
        payload = load_economy()
        schedule = payload.get("salary_schedule") if payload.get("salary_schedule") in {"weekly", "monthly"} else None
        gross_weekly_minor = to_minor(gross_weekly)
        gross_weekly = from_minor(gross_weekly_minor)
        net_weekly_minor = multiply_minor(gross_weekly_minor, SALARY_NET_RATE_DECIMAL)
        net_weekly = from_minor(net_weekly_minor)
        net_monthly = from_minor(multiply_minor(gross_weekly_minor, SALARY_NET_MONTHLY_MULTIPLIER))
        accrued_precise = _salary_accrual(payload.get("salary_accrued") or 0)
        accrued = _money(accrued_precise)
        next_payday = next_salary_payday(game_date, schedule)
        next_payday_amount = 0.0
        if next_payday:
            days_until_payday = max(0, (date.fromisoformat(next_payday) - date.fromisoformat(str(game_date))).days)
            next_payday_amount = from_minor(to_minor(
                accrued_precise + days_until_payday * net_weekly / 7,
            ))
        return {
            "schedule": schedule,
            "tax_rate": SALARY_TAX_RATE,
            "gross_weekly": gross_weekly,
            "net_weekly": net_weekly,
            "net_monthly": net_monthly,
            "accrued": accrued,
            "next_payday": next_payday,
            "next_payday_amount": next_payday_amount,
            "checked_through": payload.get("salary_checked_through"),
        }


def _advance_salary_state(
    payload: dict[str, Any], current: date, gross_weekly: float, schedule: str | None,
) -> tuple[list[date], float]:
    """Accrue salary once per game day and release only the accrued balance on paydays."""
    through_text = payload.get("salary_accrual_through") or payload.get("salary_checked_through")
    if not through_text:
        through = current - timedelta(days=1)
    else:
        try:
            through = date.fromisoformat(str(through_text))
        except ValueError:
            through = current
    if current <= through:
        payload["salary_accrual_through"] = max(current, through).isoformat()
        payload["salary_checked_through"] = max(current, through).isoformat()
        return [], 0.0

    gross_weekly_minor = to_minor(gross_weekly)
    gross_weekly = from_minor(gross_weekly_minor)
    daily_net = from_minor(multiply_minor(gross_weekly_minor, SALARY_NET_RATE_DECIMAL)) / 7
    accrued = _salary_accrual(payload.get("salary_accrued") or 0)
    paydays: list[date] = []
    total_minor = 0
    cursor = through + timedelta(days=1)
    while cursor <= current:
        accrued = _salary_accrual(accrued + daily_net)
        is_payday = (
            (schedule == "weekly" and cursor.weekday() == 6)
            or (schedule == "monthly" and cursor == _monthly_payday(cursor.year, cursor.month))
        )
        if is_payday:
            amount_minor = to_minor(accrued)
            amount = from_minor(amount_minor)
            accrued = 0.0
            _set_bank_balance_minor(
                payload, _bank_balance_minor(payload) + amount_minor,
            )
            payload["salary_payments"].append({
                "id": str(uuid.uuid4()), "game_date": cursor.isoformat(), "schedule": schedule,
                "gross_weekly": gross_weekly, "gross_weekly_minor": gross_weekly_minor,
                "tax_rate": SALARY_TAX_RATE, "net_amount": amount, "net_amount_minor": amount_minor,
                "recorded_at": _now(),
            })
            _transaction(payload, "manager_salary", amount, game_date=cursor.isoformat(), schedule=schedule)
            paydays.append(cursor)
            total_minor += amount_minor
        cursor += timedelta(days=1)
    payload["salary_accrued"] = accrued
    payload["salary_accrual_through"] = current.isoformat()
    payload["salary_checked_through"] = current.isoformat()
    payload["salary_payments"] = payload["salary_payments"][-500:]
    return paydays, from_minor(total_minor)


def set_salary_schedule(schedule: str | None, game_date: str, gross_weekly: float) -> dict[str, Any]:
    if schedule != "weekly":
        raise ValueError("工资发放方式无效")
    with _LOCK:
        payload = load_economy()
        current = date.fromisoformat(str(game_date))
        old_schedule = payload.get("salary_schedule") if payload.get("salary_schedule") in {"weekly", "monthly"} else None
        _advance_salary_state(payload, current, gross_weekly, old_schedule)
        payload["salary_schedule"] = schedule
        _transaction(payload, "salary_schedule_changed", 0, schedule=schedule, game_date=str(game_date))
        save_economy(payload)
    return salary_status(game_date, gross_weekly)


def process_salary_payments(game_date: str, gross_weekly: float) -> dict[str, Any]:
    current = date.fromisoformat(str(game_date))
    with _LOCK:
        # Complete legacy/default migrations before entering the account-store
        # transaction.  The actual salary read-modify-write below must use the
        # latest document while the cross-process storage lock is held: saving
        # a snapshot loaded before a wallet withdrawal could otherwise replace
        # the newly credited bank balance with only this salary payment.
        load_economy()

        def settle(payload: dict[str, Any]) -> dict[str, Any]:
            _normalize_economy_money(payload)
            payload.setdefault("transactions", [])
            payload.setdefault("salary_payments", [])
            payload.setdefault("salary_checked_through", None)
            payload.setdefault("salary_accrued", 0.0)
            payload.setdefault(
                "salary_accrual_through",
                payload.get("salary_checked_through"),
            )
            schedule = (
                payload.get("salary_schedule")
                if payload.get("salary_schedule") in {"weekly", "monthly"}
                else None
            )
            payment_count = len(payload["salary_payments"])
            paydays, total = _advance_salary_state(
                payload, current, gross_weekly, schedule,
            )
            _normalize_economy_money(payload)
            return {
                "paid": len(paydays),
                "amount": total,
                "new_payments": _public_money_view(
                    payload["salary_payments"][payment_count:]
                ),
            }

        result = update_document(
            "economy", _new_state(), settle, legacy_path=economy_path(),
        )
        return {**result, **salary_status(game_date, gross_weekly)}


def transfer_wallet(direction: str, amount: float) -> dict[str, Any]:
    """Move funds between the bank account and the casino wallet."""
    amount_minor = to_minor(amount)
    amount = from_minor(amount_minor)
    if amount_minor <= 0:
        raise ValueError("请输入大于0的金额")
    if direction not in {"recharge", "withdraw"}:
        raise ValueError("资金操作无效")
    with _LOCK:
        payload = load_economy()
        if direction == "recharge":
            if _bank_balance_minor(payload) < amount_minor:
                raise ValueError("银行余额不足")
            _set_bank_balance_minor(
                payload, _bank_balance_minor(payload) - amount_minor,
            )
            wallet_delta = amount
            wallet_kind = "bank_recharge"
            kind = "bank_to_casino"
        else:
            if to_minor(available_balance()) < amount_minor:
                raise ValueError("钱包余额不足")
            _set_bank_balance_minor(
                payload, _bank_balance_minor(payload) + amount_minor,
            )
            wallet_delta = -amount
            wallet_kind = "casino_withdrawal"
            kind = "casino_to_bank"
        _transaction(
            payload, kind, -amount if direction == "recharge" else amount,
            direction=direction,
        )
        wallet = _save_economy_with_wallet_adjustments(payload, [{
            "amount": wallet_delta, "type": wallet_kind,
            "metadata": {"direction": direction},
        }])
        return _public_economy_payload(payload, wallet["balance"])


def adjust_bank_balance(delta: float, kind: str, **details: Any) -> dict[str, Any]:
    """Apply one bank-side leg of an external transfer."""
    delta_minor = to_minor(delta)
    delta = from_minor(delta_minor)
    if not delta_minor:
        raise ValueError("请输入大于0的金额")
    with _LOCK:
        payload = load_economy()
        updated_minor = _bank_balance_minor(payload) + delta_minor
        if updated_minor < 0:
            raise ValueError("银行余额不足")
        _set_bank_balance_minor(payload, updated_minor)
        _transaction(payload, kind, delta, **details)
        if kind in {"transfer_budget_to_bank", "club_balance_to_bank"}:
            payload["transfer_budget_withdrawals"].append({
                "id": str(details.get("transfer_event_id") or uuid.uuid4()),
                "team_id": int(details.get("team_id") or 0),
                "team_name": str(details.get("team_name") or ""),
                "amount": abs(delta),
                "amount_minor": abs(delta_minor),
                "game_date": str(details.get("game_date") or ""),
                "game_minutes": int(details.get("game_minutes") or 0),
                "game_time": str(details.get("game_time") or ""),
                "budget_before": int(details.get("budget_before") or 0),
                "budget_after": int(details.get("budget_after") or 0),
                "source": str(details.get("source") or "transfer_budget"),
                "recorded_at": _now(),
            })
            payload["transfer_budget_withdrawals"] = payload["transfer_budget_withdrawals"][-1000:]
        result = _public_economy_payload(payload, available_balance())
        save_economy(payload)
        return result


def charge_combined_funds(
    amount: float, kind: str, *, allow_bank_overdraft: bool = False, **details: Any,
) -> dict[str, Any]:
    """Charge bank first and then the betting wallet as one recorded payment."""
    with _LOCK:
        payload = load_economy()
        payment = _collect_combined_funds(
            payload, amount, kind,
            allow_bank_overdraft=allow_bank_overdraft, **details,
        )
        _save_economy_with_wallet_adjustments(payload, [{
            "amount": -payment["wallet"], "type": kind, "metadata": details,
        }])
        return payment


def refund_combined_funds(
    payment: dict[str, Any], kind: str, **details: Any,
) -> dict[str, Any]:
    """Reverse a previous combined payment using its original funding legs."""
    bank_minor = max(0, to_minor(payment.get("bank") or 0))
    wallet_minor = max(0, to_minor(payment.get("wallet") or 0))
    bank = from_minor(bank_minor)
    wallet = from_minor(wallet_minor)
    total = from_minor(bank_minor + wallet_minor)
    source_transaction_id = str(payment.get("transaction_id") or "")
    with _LOCK:
        payload = load_economy()
        if source_transaction_id:
            previous = next((
                row for row in reversed(payload["transactions"])
                if str(row.get("refund_of") or "") == source_transaction_id
            ), None)
            if previous:
                return {
                    "bank": bank, "wallet": wallet, "total": total,
                    "transaction_id": str(previous.get("id") or ""),
                    "already_refunded": True,
                }
        _set_bank_balance_minor(
            payload, _bank_balance_minor(payload) + bank_minor,
        )
        transaction = _transaction(
            payload, kind, total, bank_refund=bank, wallet_refund=wallet,
            refund_of=source_transaction_id or None, **details,
        )
        _save_economy_with_wallet_adjustments(payload, [{
            "amount": wallet, "type": kind, "metadata": details,
        }])
    return {
        "bank": bank, "wallet": wallet, "total": total,
        "transaction_id": str(transaction["id"]),
        "already_refunded": False,
    }


def rewound_transfer_budget_withdrawals(
    team_id: int, previous_clock: tuple[str, int], current_clock: tuple[str, int],
) -> list[dict[str, Any]]:
    """Return withdrawals crossed by one backward game-clock transition."""
    try:
        previous_date = date.fromisoformat(str(previous_clock[0]))
        current_date = date.fromisoformat(str(current_clock[0]))
        previous_minutes = int(previous_clock[1])
        current_minutes = int(current_clock[1])
    except (TypeError, ValueError):
        return []
    if (
        previous_date == date(1900, 1, 1)
        or current_date == date(1900, 1, 1)
        or not 0 <= previous_minutes < 24 * 60
        or not 0 <= current_minutes < 24 * 60
    ):
        return []
    if current_clock >= previous_clock:
        return []
    with _LOCK:
        payload = load_economy()
        return [
            dict(item) for item in payload["transfer_budget_withdrawals"]
            if int(item.get("team_id") or 0) == int(team_id)
            and not item.get("clawed_back_at")
            and current_clock < (
                str(item.get("game_date") or ""), int(item.get("game_minutes") or 0)
            ) <= previous_clock
        ]


def clawback_rewound_transfer_budget(
    withdrawals: list[dict[str, Any]], previous_clock: tuple[str, int],
    current_clock: tuple[str, int],
) -> dict[str, Any]:
    if not withdrawals:
        return {"clawed_back": 0, "amount": 0.0, "bank": 0.0, "wallet": 0.0, "unrecovered": 0.0}
    with _LOCK:
        payload = load_economy()
        withdrawal_ids = {str(item.get("id") or "") for item in withdrawals}
        active = [
            item for item in payload["transfer_budget_withdrawals"]
            if str(item.get("id") or "") in withdrawal_ids and not item.get("clawed_back_at")
        ]
        if not active:
            return {"clawed_back": 0, "amount": 0.0, "bank": 0.0, "wallet": 0.0, "unrecovered": 0.0}
        amount_minor = sum(read_minor(item, "amount") for item in active)
        bank_used_minor = min(max(0, _bank_balance_minor(payload)), amount_minor)
        wallet_due_minor = amount_minor - bank_used_minor
        wallet_used_minor = min(max(0, to_minor(available_balance())), wallet_due_minor)
        unrecovered_minor = amount_minor - bank_used_minor - wallet_used_minor
        _set_bank_balance_minor(
            payload, _bank_balance_minor(payload) - bank_used_minor,
        )
        amount = from_minor(amount_minor)
        bank_used = from_minor(bank_used_minor)
        wallet_used = from_minor(wallet_used_minor)
        unrecovered = from_minor(unrecovered_minor)
        clawed_at = _now()
        for item in active:
            item["clawed_back_at"] = clawed_at
            write_minor(item, "clawed_back_amount", read_minor(item, "amount"))
        rollback = {
            "id": str(uuid.uuid4()),
            "withdrawal_ids": [str(item.get("id") or "") for item in active],
            "amount": amount,
            "bank_deducted": bank_used,
            "wallet_deducted": wallet_used,
            "unrecovered": unrecovered,
            "previous_game_date": previous_clock[0],
            "previous_game_minutes": previous_clock[1],
            "current_game_date": current_clock[0],
            "current_game_minutes": current_clock[1],
            "recorded_at": clawed_at,
        }
        migrate_money_fields(
            rollback,
            ("amount", "bank_deducted", "wallet_deducted", "unrecovered"),
        )
        payload["transfer_budget_rollbacks"].append(rollback)
        payload["transfer_budget_rollbacks"] = payload["transfer_budget_rollbacks"][-1000:]
        _transaction(
            payload, "transfer_budget_rollback_clawback", -amount,
            bank_deducted=bank_used, wallet_deducted=wallet_used, unrecovered=unrecovered,
        )
        _save_economy_with_wallet_adjustments(payload, [{
            "amount": -wallet_used,
            "type": "transfer_budget_rollback_clawback",
            "metadata": {
                "withdrawal_ids": sorted(withdrawal_ids),
                "previous_game_date": previous_clock[0],
                "current_game_date": current_clock[0],
            },
        }])
        return {
            "clawed_back": len(active), "amount": amount,
            "bank": bank_used, "wallet": wallet_used, "unrecovered": unrecovered,
        }


CREDIT_THEORETICAL_MULTIPLIER = 18


def _credit_multiplier(effective_bet_count: int) -> int:
    count = max(0, int(effective_bet_count or 0))
    if count < 10:
        return 4
    if count < 20:
        return 8
    if count < 30:
        return 12
    return CREDIT_THEORETICAL_MULTIPLIER


def _credit_limit(gross_weekly: float, effective_bet_count: int = 0) -> float:
    return from_minor(multiply_minor(
        to_minor(max(0.0, float(gross_weekly))),
        _credit_multiplier(effective_bet_count),
    ))


def _accrue_credit(payload: dict[str, Any], game_date: str) -> None:
    credit = payload["credit"]
    principal_minor = _credit_minor(credit, "principal")
    if principal_minor <= 0 or credit.get("status") != "active":
        return
    current = date.fromisoformat(str(game_date))
    last_text = credit.get("last_accrual_date") or credit.get("borrowed_on") or game_date
    try:
        last = date.fromisoformat(str(last_text))
    except ValueError:
        last = current
    days = max(0, (current - last).days)
    if days:
        _set_credit_minor(
            credit, "interest",
            _credit_minor(credit, "interest")
            + multiply_minor(principal_minor, 0.005 * days),
        )
        credit["last_accrual_date"] = current.isoformat()


def credit_status(
    game_date: str, gross_weekly: float, effective_bet_count: int = 0,
) -> dict[str, Any]:
    with _LOCK:
        payload = load_economy()
        _accrue_credit(payload, game_date)
        save_economy(payload)
        return _credit_status_payload(payload, gross_weekly, effective_bet_count)


def _credit_status_payload(
    payload: dict[str, Any], gross_weekly: float, effective_bet_count: int = 0,
) -> dict[str, Any]:
    credit = payload["credit"]
    principal_minor = _credit_minor(credit, "principal")
    interest_minor = _credit_minor(credit, "interest")
    principal = from_minor(principal_minor)
    interest = from_minor(interest_minor)
    multiplier = _credit_multiplier(effective_bet_count)
    limit = _credit_limit(gross_weekly, effective_bet_count)
    theoretical_limit = from_minor(multiply_minor(
        to_minor(max(0.0, float(gross_weekly))),
        CREDIT_THEORETICAL_MULTIPLIER,
    ))
    return {
        **dict(credit), "principal": principal, "interest": interest,
        "outstanding": from_minor(principal_minor + interest_minor), "limit": limit,
        "available_credit": from_minor(max(0, to_minor(limit) - principal_minor)),
        "theoretical_limit": theoretical_limit,
        "theoretical_multiplier": CREDIT_THEORETICAL_MULTIPLIER,
        "credit_multiplier": multiplier,
        "effective_bet_count": max(0, int(effective_bet_count or 0)),
        "daily_rate": 0.005, "term_days": 30,
    }


def borrow_credit(
    amount: float, game_date: str, gross_weekly: float,
    effective_bet_count: int = 0,
) -> dict[str, Any]:
    amount_minor = to_minor(amount)
    amount = from_minor(amount_minor)
    if amount_minor <= 0:
        raise ValueError("请输入大于0的借款金额")
    current = date.fromisoformat(str(game_date))
    with _LOCK:
        payload = load_economy()
        _accrue_credit(payload, game_date)
        credit = payload["credit"]
        limit = _credit_limit(gross_weekly, effective_bet_count)
        if limit <= 0:
            raise ValueError("当前没有可用于授信的周薪")
        principal_minor = _credit_minor(credit, "principal")
        if principal_minor + amount_minor > to_minor(limit):
            raise ValueError("借款金额超过当前可用额度")
        if principal_minor <= 0:
            credit.update({
                "due_date": (current + timedelta(days=30)).isoformat(),
                "last_accrual_date": current.isoformat(), "status": "active",
            })
            _set_credit_minor(credit, "principal", amount_minor)
            _set_credit_minor(credit, "original_principal", amount_minor)
            _set_credit_minor(
                credit, "interest", multiply_minor(amount_minor, 0.005),
            )
        else:
            _set_credit_minor(
                credit, "principal", principal_minor + amount_minor,
            )
            _set_credit_minor(
                credit, "interest", _credit_minor(credit, "interest")
                + multiply_minor(amount_minor, 0.005),
            )
            _set_credit_minor(
                credit, "original_principal",
                _credit_minor(credit, "original_principal") + amount_minor,
            )
        _set_bank_balance_minor(
            payload, _bank_balance_minor(payload) + amount_minor,
        )
        _transaction(payload, "credit_borrowed", amount, game_date=current.isoformat(), due_date=credit["due_date"])
        result = _public_economy_payload(payload, available_balance())
        result["credit"] = _credit_status_payload(
            payload, gross_weekly, effective_bet_count,
        )
        save_economy(payload)
    return result


def repay_credit(
    amount: float, game_date: str, gross_weekly: float,
    effective_bet_count: int = 0,
) -> dict[str, Any]:
    amount_minor = to_minor(amount)
    if amount_minor <= 0:
        raise ValueError("请输入大于0的还款金额")
    with _LOCK:
        payload = load_economy()
        _accrue_credit(payload, game_date)
        credit = payload["credit"]
        principal_minor = _credit_minor(credit, "principal")
        interest_minor = _credit_minor(credit, "interest")
        amount_minor = min(amount_minor, principal_minor + interest_minor)
        if amount_minor <= 0:
            raise ValueError("当前没有待还款项")
        if _bank_balance_minor(payload) + to_minor(available_balance()) < amount_minor:
            raise ValueError("银行与钱包余额合计不足")
        bank_used_minor = min(_bank_balance_minor(payload), amount_minor)
        wallet_used_minor = amount_minor - bank_used_minor
        _set_bank_balance_minor(
            payload, _bank_balance_minor(payload) - bank_used_minor,
        )
        interest_paid_minor = min(interest_minor, amount_minor)
        _set_credit_minor(credit, "interest", interest_minor - interest_paid_minor)
        _set_credit_minor(
            credit, "principal",
            principal_minor - (amount_minor - interest_paid_minor),
        )
        if _credit_minor(credit, "principal") <= 0 and _credit_minor(credit, "interest") <= 0:
            _set_credit_minor(credit, "principal", 0)
            _set_credit_minor(credit, "interest", 0)
            credit.update({"status": "repaid", "due_date": None})
        amount = from_minor(amount_minor)
        bank_used = from_minor(bank_used_minor)
        wallet_used = from_minor(wallet_used_minor)
        _transaction(payload, "credit_repaid", -amount, bank_used=bank_used, wallet_used=wallet_used, game_date=game_date)
        wallet = _save_economy_with_wallet_adjustments(payload, [{
            "amount": -wallet_used, "type": "credit_repayment",
            "metadata": {"game_date": game_date},
        }])
    result = _public_economy_payload(payload, wallet["balance"])
    result["credit"] = _credit_status_payload(
        payload, gross_weekly, effective_bet_count,
    )
    return result


def clear_credit_cheat() -> dict[str, Any]:
    with _LOCK:
        payload = load_economy()
        credit = payload["credit"]
        cleared = _money(
            (credit.get("principal") or 0) + (credit.get("interest") or 0)
        )
        _set_credit_minor(credit, "principal", 0)
        _set_credit_minor(credit, "original_principal", 0)
        _set_credit_minor(credit, "interest", 0)
        credit.update({
            "borrowed_on": None,
            "due_date": None,
            "last_accrual_date": None,
            "status": "repaid",
        })
        _transaction(payload, "credit_cheat_cleared", -cleared)
        result = _public_economy_payload(payload, available_balance())
        save_economy(payload)
        return result


def process_credit_due(game_date: str, gross_weekly: float) -> dict[str, Any]:
    """Collect a due loan. The caller applies the one-time in-game penalty if defaulted."""
    with _LOCK:
        payload = load_economy()
        _accrue_credit(payload, game_date)
        credit = payload["credit"]
        due_text = credit.get("due_date")
        if credit.get("status") != "active" or not due_text or date.fromisoformat(game_date) < date.fromisoformat(due_text):
            save_economy(payload)
            return {"due": False, "defaulted": False}
        outstanding_minor = (
            _credit_minor(credit, "principal")
            + _credit_minor(credit, "interest")
        )
        original_minor = _credit_minor(credit, "original_principal")
        loan_amount = from_minor(original_minor or outstanding_minor)
        collectible_minor = (
            _bank_balance_minor(payload) + to_minor(available_balance())
        )
        paid_minor = min(outstanding_minor, collectible_minor)
        bank_used_minor = min(_bank_balance_minor(payload), paid_minor)
        wallet_used_minor = paid_minor - bank_used_minor
        _set_bank_balance_minor(
            payload, _bank_balance_minor(payload) - bank_used_minor,
        )
        outstanding = from_minor(outstanding_minor)
        paid = from_minor(paid_minor)
        bank_used = from_minor(bank_used_minor)
        wallet_used = from_minor(wallet_used_minor)
        defaulted = paid_minor < outstanding_minor
        _set_credit_minor(credit, "principal", 0)
        _set_credit_minor(credit, "interest", 0)
        credit.update({
            "status": "defaulted" if defaulted else "repaid", "due_date": None,
            "settled_on": str(game_date),
            "unpaid_written_off": from_minor(outstanding_minor - paid_minor),
            "unpaid_written_off_minor": outstanding_minor - paid_minor,
        })
        unpaid = from_minor(outstanding_minor - paid_minor)
        _transaction(payload, "credit_defaulted" if defaulted else "credit_repaid_at_due", -paid,
                     due_amount=outstanding, unpaid=unpaid, game_date=game_date)
        _save_economy_with_wallet_adjustments(payload, [{
            "amount": -wallet_used, "type": "credit_due_collection",
            "metadata": {"game_date": game_date, "due_amount": outstanding},
        }])
        return {
            "due": True, "defaulted": defaulted, "paid": paid,
            "unpaid": unpaid, "loan_amount": loan_amount,
        }


def record_credit_default_notice(penalty: dict[str, Any], game_date: str) -> dict[str, Any]:
    with _LOCK:
        payload = load_economy()
        notice = {
            "id": str(uuid.uuid4()), "game_date": str(game_date), "created_at": _now(),
            "acknowledged": False, **penalty,
        }
        payload["default_notices"].append(notice)
        payload["default_notices"] = payload["default_notices"][-50:]
        save_economy(payload)
        return notice


def acknowledge_credit_default_notice(notice_id: str) -> dict[str, Any]:
    with _LOCK:
        payload = load_economy()
        notice = next((row for row in payload["default_notices"] if row.get("id") == notice_id), None)
        if not notice:
            raise ValueError("违约通知不存在")
        notice["acknowledged"] = True
        notice["acknowledged_at"] = _now()
        save_economy(payload)
        return {"acknowledged": True, "notice": dict(notice)}


def collect_match_integrity_penalty(
    confiscation: float, standard_fine: float, *, maximum_total: float | None = None,
) -> dict[str, float]:
    confiscation_requested_minor = to_minor(max(0.0, float(confiscation)))
    standard_fine_minor = to_minor(max(0.0, float(standard_fine)))
    confiscation_requested = from_minor(confiscation_requested_minor)
    standard_fine = from_minor(standard_fine_minor)
    if maximum_total is None:
        maximum_total_minor = confiscation_requested_minor + standard_fine_minor
    else:
        maximum_total_minor = to_minor(max(0.0, float(maximum_total)))
    target_confiscation_minor = min(
        confiscation_requested_minor, maximum_total_minor,
    )
    target_fine_minor = min(
        standard_fine_minor, maximum_total_minor - target_confiscation_minor,
    )
    with _LOCK:
        payload = load_economy()
        bank_available_minor = max(0, _bank_balance_minor(payload))
        wallet_available_minor = max(0, to_minor(available_balance()))
        collectible_minor = min(
            target_confiscation_minor + target_fine_minor,
            bank_available_minor + wallet_available_minor,
        )
        confiscation_minor = min(target_confiscation_minor, collectible_minor)
        actual_fine_minor = min(
            target_fine_minor, collectible_minor - confiscation_minor,
        )
        total_minor = confiscation_minor + actual_fine_minor
        bank_from_balance_minor = min(bank_available_minor, total_minor)
        wallet_used_minor = min(
            wallet_available_minor, total_minor - bank_from_balance_minor,
        )
        confiscation = from_minor(confiscation_minor)
        actual_fine = from_minor(actual_fine_minor)
        total = from_minor(total_minor)
        wallet_used = from_minor(wallet_used_minor)
        bank_overdraft = 0.0
        bank_used = from_minor(bank_from_balance_minor)
        _set_bank_balance_minor(
            payload, _bank_balance_minor(payload) - bank_from_balance_minor,
        )
        _transaction(
            payload, "match_integrity_penalty", -total,
            confiscation_requested=confiscation_requested, confiscated=confiscation,
            standard_fine=standard_fine, actual_fine=actual_fine,
            bank_used=bank_used, bank_overdraft=bank_overdraft, wallet_used=wallet_used,
        )
        _save_economy_with_wallet_adjustments(payload, [{
            "amount": -wallet_used, "type": "match_integrity_penalty",
            "metadata": {
                "confiscation": confiscation, "standard_fine": standard_fine,
                "actual_fine": actual_fine,
            },
        }])
        return {
            "confiscation_requested": confiscation_requested,
            "confiscated": confiscation,
            "standard_fine": standard_fine,
            "actual_fine": actual_fine,
            "bank_used": bank_used,
            "bank_overdraft": bank_overdraft,
            "wallet_used": wallet_used,
            "total_deducted": total,
        }


def active_player_ids() -> list[int]:
    with _LOCK:
        identifiers: set[int] = set()
        for item in load_economy()["inventory"]:
            if item.get("status") != "active" or item.get("sku") not in {"red_bull", "team_red_bull"}:
                continue
            if item.get("player_id"):
                identifiers.add(int(item["player_id"]))
            identifiers.update(int(value) for value in item.get("player_ids", []) if int(value) > 0)
        return sorted(identifiers)


def buy_item(sku: str, quantity: int = 1, *, free: bool = False) -> dict[str, Any]:
    product = PRODUCTS.get(sku)
    if sku not in SHOP_SKUS or not product or not 1 <= quantity <= 99:
        raise ValueError("商品或数量无效")
    if product.get("purchasable") is False:
        raise ValueError("该商品有闪退风险，暂未实装")
    with _LOCK:
        payload = load_economy()
        free = bool(payload.get("free_services"))
        unit_price_minor = _shop_price_minor(product["price"])
        unit_price = from_minor(unit_price_minor)
        total_minor = 0 if free else unit_price_minor * quantity
        total = from_minor(total_minor)
        if _bank_balance_minor(payload) + to_minor(available_balance()) < total_minor:
            raise ValueError("银行与钱包余额合计不足")
        bank_used_minor = min(_bank_balance_minor(payload), total_minor)
        wallet_used_minor = total_minor - bank_used_minor
        bank_used = from_minor(bank_used_minor)
        wallet_used = from_minor(wallet_used_minor)
        if total_minor:
            _set_bank_balance_minor(
                payload, _bank_balance_minor(payload) - bank_used_minor,
            )
        for _ in range(quantity):
            payload["inventory"].append({
                "id": str(uuid.uuid4()), "sku": sku, "name": product["name"], "price": unit_price,
                "category": product["category"], "duration": product["duration"], "status": "available", "purchased_at": _now(),
            })
        _transaction(payload, "shop_purchase", -total, sku=sku, quantity=quantity, free=bool(free),
                     bank_used=bank_used, wallet_used=wallet_used)
        wallet = _save_economy_with_wallet_adjustments(payload, [{
            "amount": -wallet_used, "type": "shop_purchase",
            "metadata": {"sku": sku, "quantity": quantity},
        }])
        result = _public_economy_payload(payload, wallet["balance"])
        result["payment"] = {"total": total, "bank": bank_used, "wallet": wallet_used}
        return result


def buy_items(items: list[dict[str, Any]], *, free: bool = False) -> dict[str, Any]:
    if not isinstance(items, list) or not items:
        raise ValueError("购物车为空")
    quantities: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("购物车商品无效")
        sku = str(item.get("sku") or "")
        try:
            quantity = int(item.get("quantity", 0))
        except (TypeError, ValueError):
            raise ValueError("商品数量无效") from None
        if sku not in SHOP_SKUS or sku not in PRODUCTS or not 1 <= quantity <= 99:
            raise ValueError("商品或数量无效")
        if PRODUCTS[sku].get("purchasable") is False:
            raise ValueError("该商品有闪退风险，暂未实装")
        quantities[sku] = quantities.get(sku, 0) + quantity
        if quantities[sku] > 99:
            raise ValueError("单种商品最多购买99件")
    purchases = [{"sku": sku, "quantity": quantity} for sku, quantity in quantities.items()]
    with _LOCK:
        payload = load_economy()
        free = bool(payload.get("free_services"))
        settings = load_settings()
        unit_prices_minor = {
            row["sku"]: _shop_price_minor(PRODUCTS[row["sku"]]["price"], settings)
            for row in purchases
        }
        total_minor = 0 if free else sum(
            unit_prices_minor[row["sku"]] * row["quantity"]
            for row in purchases
        )
        total = from_minor(total_minor)
        wallet_balance = available_balance()
        if _bank_balance_minor(payload) + to_minor(wallet_balance) < total_minor:
            raise ValueError("银行与钱包余额合计不足")
        bank_used_minor = min(_bank_balance_minor(payload), total_minor)
        wallet_used_minor = total_minor - bank_used_minor
        bank_used = from_minor(bank_used_minor)
        wallet_used = from_minor(wallet_used_minor)
        if total_minor:
            _set_bank_balance_minor(
                payload, _bank_balance_minor(payload) - bank_used_minor,
            )
        purchased_at = _now()
        for row in purchases:
            product = PRODUCTS[row["sku"]]
            unit_price = from_minor(unit_prices_minor[row["sku"]])
            for _ in range(row["quantity"]):
                payload["inventory"].append({
                    "id": str(uuid.uuid4()), "sku": row["sku"], "name": product["name"], "price": unit_price,
                    "category": product["category"], "duration": product["duration"], "status": "available", "purchased_at": purchased_at,
                })
        _transaction(payload, "shop_purchase", -total, items=purchases,
                     quantity=sum(row["quantity"] for row in purchases), free=bool(free),
                     bank_used=bank_used, wallet_used=wallet_used)
        wallet = _save_economy_with_wallet_adjustments(payload, [{
            "amount": -wallet_used, "type": "shop_purchase",
            "metadata": {
                "items": purchases,
                "quantity": sum(row["quantity"] for row in purchases),
            },
        }])
        result = _public_economy_payload(payload, wallet["balance"])
        result["payment"] = {"total": total, "bank": bank_used, "wallet": wallet_used}
        result["purchases"] = purchases
        return result


def destroy_item(item_id: str) -> dict[str, Any]:
    with _LOCK:
        payload = load_economy()
        item = next((row for row in payload["inventory"] if row.get("id") == item_id), None)
        if not item or item.get("status") != "available":
            raise ValueError("该物品当前不能销毁")
        payload["inventory"].remove(item)
        _transaction(payload, "item_destroyed", 0, sku=item.get("sku"), item_id=item_id)
        save_economy(payload)
        return public_economy()


def destroy_items(item_ids: list[str]) -> dict[str, Any]:
    identifiers = list(dict.fromkeys(str(value) for value in item_ids if str(value)))
    if not identifiers or len(identifiers) > 99:
        raise ValueError("删除数量无效")
    with _LOCK:
        payload = load_economy()
        selected = [row for row in payload["inventory"] if row.get("id") in identifiers]
        if len(selected) != len(identifiers) or any(row.get("status") != "available" for row in selected):
            raise ValueError("部分物品当前不能删除")
        payload["inventory"] = [row for row in payload["inventory"] if row.get("id") not in identifiers]
        for item in selected:
            _transaction(payload, "item_destroyed", 0, sku=item.get("sku"), item_id=item.get("id"))
        save_economy(payload)
        return public_economy()


def consume_instant_item(item_id: str, sku: str, **details: Any) -> dict[str, Any]:
    with _LOCK:
        payload = load_economy()
        item = next((row for row in payload["inventory"] if row.get("id") == item_id), None)
        if not item or item.get("status") != "available" or item.get("sku") != sku:
            raise ValueError("该物品已经使用或不存在")
        payload["inventory"].remove(item)
        _transaction(payload, "instant_item_used", 0, item_id=item_id, sku=sku, **details)
        save_economy(payload)
        return public_economy()


def consume_instant_items(item_ids: list[str], sku: str, **details: Any) -> dict[str, Any]:
    identifiers = list(dict.fromkeys(str(value) for value in item_ids if str(value)))
    if not identifiers or len(identifiers) > 99:
        raise ValueError("批量使用数量无效")
    with _LOCK:
        payload = load_economy()
        selected = [row for row in payload["inventory"] if row.get("id") in identifiers]
        if (
            len(selected) != len(identifiers)
            or any(row.get("status") != "available" or row.get("sku") != sku for row in selected)
        ):
            raise ValueError("部分道具已经使用、不存在或并非同一种道具")
        payload["inventory"] = [row for row in payload["inventory"] if row.get("id") not in identifiers]
        _transaction(
            payload, "instant_items_used", 0, item_ids=identifiers, sku=sku,
            quantity=len(identifiers), **details,
        )
        save_economy(payload)
        return public_economy()


def cancel_item_use(item_id: str) -> dict[str, Any]:
    with _LOCK:
        payload = load_economy()
        item = next((row for row in payload["inventory"] if row.get("id") == item_id), None)
        if not item or item.get("status") not in {"scheduled", "active"}:
            raise ValueError("该物品当前没有可撤销的使用安排")
        previous = {
            "player_id": item.get("player_id"), "fixture_key": item.get("fixture_key"),
            "previous_status": item.get("status"),
        }
        for field in (
            "player_id", "player_name", "player_ids", "player_count", "fixture_key", "fixture_identity", "fixture_date",
            "fixture_name", "kickoff_minutes", "kickoff_time",
            "assigned_at", "activated_at", "effect_error",
            "effect_applied", "effect_applied_at", "effect_detail",
            "player_address", "player_targets", "managed_team_address",
            "managed_team_id", "managed_team_name", "opponent_team_id",
            "opponent_team_name", "opponent_team_address",
        ):
            item.pop(field, None)
        item.update({"status": "available", "cancelled_use_at": _now()})
        _transaction(payload, "item_use_cancelled", 0, item_id=item_id, **previous)
        save_economy(payload)
        return public_economy()


def assign_item(item_id: str, player: dict[str, Any], fixture: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        payload = load_economy()
        item = next((row for row in payload["inventory"] if row.get("id") == item_id), None)
        if not item or item.get("status") != "available":
            raise ValueError("该功能饮料已经使用或不存在")
        parts = str(fixture["key"]).split("|")
        identity = "|".join(parts[1:]) if len(parts) == 4 else str(fixture["key"])
        if item.get("sku") == "doping":
            overlapping = next((
                row for row in payload["inventory"]
                if row.get("status") in {"scheduled", "active"}
                and row.get("sku") in {"doping", "team_doping"}
                and str(row.get("fixture_identity") or "") == identity
                and int(row.get("managed_team_id") or 0) == int(fixture.get("managed_team_id") or 0)
                and (row.get("sku") == "team_doping" or int(row.get("player_id") or 0) == int(player["id"]))
            ), None)
            if overlapping:
                raise ValueError("该球员在这场比赛中已经安排了兴奋剂效果")
        if item.get("sku") == "opponent_flu" and sum(
            1 for row in payload["inventory"]
            if row.get("id") != item_id
            and row.get("sku") == "opponent_flu"
            and row.get("status") in {"scheduled", "active"}
            and str(row.get("fixture_identity") or "") == identity
        ) >= 2:
            raise ValueError("同一场比赛最多使用两份病毒包裹")
        item.update({
            "status": "scheduled", "player_id": int(player["id"]), "player_name": str(player["name"]),
            "player_address": str(player.get("address") or ""),
            "fixture_key": str(fixture["key"]), "fixture_date": str(fixture["date"]),
            "fixture_name": str(fixture["name"]),
            "kickoff_minutes": fixture.get("kickoff_minutes"),
            "kickoff_time": fixture.get("kickoff_time"), "assigned_at": _now(),
            "fixture_identity": identity,
            "managed_team_id": int(fixture.get("managed_team_id") or 0),
            "managed_team_name": str(fixture.get("managed_team_name") or ""),
            "managed_team_address": str(fixture.get("managed_team_address") or ""),
            "competition_id": int(fixture.get("competition_id") or 0),
            "competition_name": str(fixture.get("competition_name") or ""),
            "competition_kind": str(fixture.get("competition_kind") or ""),
            "home_name": str(fixture.get("home_name") or ""),
            "away_name": str(fixture.get("away_name") or ""),
            "hook_version": 2,
        })
        if item.get("sku") == "opponent_flu":
            item.update({
                "effect_mode": "opponent_player_flu",
                "opponent_team_id": int(fixture.get("opponent_team_id") or 0),
                "opponent_team_name": str(fixture.get("opponent_team_name") or ""),
                "opponent_team_address": str(fixture.get("opponent_team_address") or ""),
            })
        _transaction(payload, "item_scheduled", 0, item_id=item_id, player_id=item["player_id"], fixture_key=item["fixture_key"])
        save_economy(payload)
        return public_economy()


def assign_team_item(item_id: str, players: list[dict[str, Any]], fixture: dict[str, Any]) -> dict[str, Any]:
    identifiers = sorted({int(player["id"]) for player in players if int(player.get("id", 0)) > 0})
    if not identifiers:
        raise ValueError("当前一线队没有可用球员")
    with _LOCK:
        payload = load_economy()
        item = next((row for row in payload["inventory"] if row.get("id") == item_id), None)
        if not item or item.get("sku") not in {"team_red_bull", "team_doping"} or item.get("status") != "available":
            raise ValueError("该全队比赛物品已经使用或不存在")
        parts = str(fixture["key"]).split("|")
        identity = "|".join(parts[1:]) if len(parts) == 4 else str(fixture["key"])
        if item.get("sku") == "team_doping" and any(
            row.get("status") in {"scheduled", "active"}
            and row.get("sku") in {"doping", "team_doping"}
            and str(row.get("fixture_identity") or "") == identity
            and int(row.get("managed_team_id") or 0) == int(fixture.get("managed_team_id") or 0)
            for row in payload["inventory"]
        ):
            raise ValueError("该队在这场比赛中已经安排了兴奋剂效果")
        item.update({
            "status": "scheduled", "player_ids": identifiers, "player_count": len(identifiers),
            "player_targets": [
                {"player_id": int(player["id"]), "player_name": str(player.get("name") or player["id"]),
                 "player_address": str(player.get("address") or "")}
                for player in players if int(player.get("id", 0)) > 0
            ],
            "fixture_key": str(fixture["key"]), "fixture_date": str(fixture["date"]),
            "fixture_name": str(fixture["name"]),
            "kickoff_minutes": fixture.get("kickoff_minutes"),
            "kickoff_time": fixture.get("kickoff_time"), "assigned_at": _now(),
            "fixture_identity": identity,
            "managed_team_id": int(fixture.get("managed_team_id") or 0),
            "managed_team_name": str(fixture.get("managed_team_name") or ""),
            "managed_team_address": str(fixture.get("managed_team_address") or ""),
            "competition_id": int(fixture.get("competition_id") or 0),
            "competition_name": str(fixture.get("competition_name") or ""),
            "competition_kind": str(fixture.get("competition_kind") or ""),
            "home_name": str(fixture.get("home_name") or ""),
            "away_name": str(fixture.get("away_name") or ""),
            "hook_version": 2,
        })
        _transaction(payload, "team_item_scheduled", 0, item_id=item_id, player_count=len(identifiers), fixture_key=item["fixture_key"])
        save_economy(payload)
        return public_economy()


def assign_fixture_item(item_id: str, fixture: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        payload = load_economy()
        item = next((row for row in payload["inventory"] if row.get("id") == item_id), None)
        if not item or item.get("sku") not in {"bribed_goalkeeper", "referee", "referee_level2", "referee_level3"} or item.get("status") != "available":
            raise ValueError("该比赛物品已经使用或不存在")
        sku = str(item.get("sku"))
        parts = str(fixture["key"]).split("|")
        identity = "|".join(parts[1:]) if len(parts) == 4 else str(fixture["key"])
        if sku in {"referee", "referee_level2", "referee_level3"} and any(
            row.get("status") in {"scheduled", "active"}
            and row.get("sku") in {"referee", "referee_level2", "referee_level3"}
            and str(row.get("fixture_identity") or "") == identity
            and int(row.get("managed_team_id") or 0)
            == int(fixture.get("managed_team_id") or 0)
            for row in payload["inventory"]
        ):
            raise ValueError("同一场比赛只能使用一个黑哨道具，1、2、3级不能同时安排")
        for stale_key in ("cancelled_use_at", "refunded_at", "effect_error", "expired_at"):
            item.pop(stale_key, None)
        common = {
            "status": "scheduled", "fixture_key": str(fixture["key"]),
            "fixture_date": str(fixture["date"]), "fixture_name": str(fixture["name"]),
            "kickoff_minutes": fixture.get("kickoff_minutes"),
            "kickoff_time": fixture.get("kickoff_time"),
            "fixture_identity": identity, "assigned_at": _now(),
            "managed_team_id": int(fixture.get("managed_team_id") or 0),
            "managed_team_name": str(fixture.get("managed_team_name") or ""),
            "managed_team_address": str(fixture.get("managed_team_address") or ""),
            "competition_id": int(fixture.get("competition_id") or 0),
            "competition_name": str(fixture.get("competition_name") or ""),
            "competition_kind": str(fixture.get("competition_kind") or ""),
            "home_name": str(fixture.get("home_name") or ""),
            "away_name": str(fixture.get("away_name") or ""),
            "manager_name": str(fixture.get("manager_name") or ""),
            "referee_name": str(fixture.get("referee_name") or ""),
        }
        if sku == "bribed_goalkeeper":
            common.update({
                "hook_version": 2,
                "effect_mode": "opponent_match_goalkeeper_attributes",
                "opponent_team_id": int(fixture.get("opponent_team_id") or 0),
                "opponent_team_name": str(fixture.get("opponent_team_name") or ""),
                "opponent_team_address": str(fixture.get("opponent_team_address") or ""),
            })
            if not common["opponent_team_id"]:
                raise ValueError("无法定位所选比赛的对手球队")
        else:
            level = {"referee": 1, "referee_level2": 2, "referee_level3": 3}[sku]
            common.update({
                "hook_version": 9,
                "effect_mode": {1: "managed_ignore_opponent_normal", 2: "managed_ignore_opponent_escalated", 3: "managed_ignore_opponent_red"}[level],
                "referee_level": level,
            })
        item.update(common)
        _transaction(payload, "fixture_item_scheduled", 0, item_id=item_id, sku=item["sku"], fixture_key=item["fixture_key"])
        save_economy(payload)
        return public_economy()


_REFEREE_FIXTURE_ITEM_SKUS = {"referee", "referee_level2", "referee_level3"}


def referee_hook_candidate(game_date: str) -> dict[str, Any] | None:
    """Return the one referee item whose Hook should currently be armed."""
    with _LOCK:
        candidates = [
            dict(item) for item in load_economy()["inventory"]
            if item.get("sku") in _REFEREE_FIXTURE_ITEM_SKUS
            and (
                item.get("status") == "active"
                or (
                    item.get("status") == "scheduled"
                    and str(item.get("fixture_date") or "") == str(game_date or "")
                )
            )
        ]
    candidates.sort(key=lambda item: (
        0 if item.get("status") == "active" else 1,
        -int(item.get("referee_level") or 0),
        str(item.get("assigned_at") or ""),
        str(item.get("id") or ""),
    ))
    return candidates[0] if candidates else None


def activate_scheduled_referee_item(item_id: str, game_date: str) -> dict[str, Any]:
    """Commit match-day activation only after the caller has armed the Hook."""
    with _LOCK:
        payload = load_economy()
        item = next(
            (row for row in payload["inventory"] if str(row.get("id") or "") == str(item_id)),
            None,
        )
        if not item or item.get("sku") not in _REFEREE_FIXTURE_ITEM_SKUS:
            raise ValueError("黑哨道具已经使用或不存在")
        if item.get("status") == "active":
            return dict(item)
        if (
            item.get("status") != "scheduled"
            or str(item.get("fixture_date") or "") != str(game_date or "")
        ):
            raise ValueError("黑哨道具当前不满足比赛日激活条件")
        item.update({"status": "active", "activated_at": _now()})
        _transaction(
            payload, "item_activated", 0,
            item_id=item_id, sku=item.get("sku"), fixture_key=item.get("fixture_key"),
        )
        save_economy(payload)
        return dict(item)


def activate_toggle_item(item_id: str) -> dict[str, Any]:
    with _LOCK:
        payload = load_economy()
        item = next((row for row in payload["inventory"] if row.get("id") == item_id), None)
        if not item or item.get("status") != "available" or item.get("duration") not in {"toggle", "policy"}:
            raise ValueError("该物品当前不能直接激活")
        item.update({"status": "active", "activated_at": _now()})
        _transaction(payload, "item_activated", 0, item_id=item_id, sku=item.get("sku"))
        save_economy(payload)
        return public_economy()


def active_effect_skus() -> list[str]:
    with _LOCK:
        return sorted({
            str(item["sku"]) for item in load_economy()["inventory"]
            if item.get("status") == "active" and item.get("duration") == "toggle" and item.get("sku")
        })


def active_fixture_effect_skus() -> list[str]:
    with _LOCK:
        return sorted({
            str(item["sku"]) for item in load_economy()["inventory"]
            if item.get("status") == "active" and item.get("duration") == "fixture" and item.get("sku")
        })


def active_referee_level() -> int:
    active = set(active_fixture_effect_skus())
    if "referee_level3" in active:
        return 3
    if "referee_level2" in active:
        return 2
    return 1 if "referee" in active else 0


def active_goalkeeper_bribes() -> list[dict[str, Any]]:
    with _LOCK:
        return [
            dict(item) for item in load_economy()["inventory"]
            if item.get("status") == "active" and item.get("sku") == "bribed_goalkeeper"
        ]


_INTEGRITY_MATCH_ITEM_SKUS = {
    "referee", "referee_level2", "referee_level3", "doping", "team_doping",
}


def pending_match_item_consequences() -> list[dict[str, Any]]:
    with _LOCK:
        payload = load_economy()
        rows = [
            item for item in payload.get("inventory", [])
            if item.get("sku") in _INTEGRITY_MATCH_ITEM_SKUS
            and item.get("status") in {"scheduled", "active"}
            and not item.get("consequence_assessed")
        ]
        rows.extend(
            item for item in payload.get("match_item_history", [])
            if item.get("sku") in _INTEGRITY_MATCH_ITEM_SKUS
            and not item.get("consequence_assessed")
        )
        return [dict(item) for item in rows]


def mark_match_item_consequence(
    item_id: str, *, outcome: str, profit: float, bet_ids: list[str], game_date: str,
) -> dict[str, Any]:
    with _LOCK:
        payload = load_economy()
        item = next((
            row for row in [*payload.get("inventory", []), *payload.get("match_item_history", [])]
            if str(row.get("id") or "") == str(item_id)
        ), None)
        if not item:
            raise ValueError("比赛道具记录不存在")
        if item.get("consequence_assessed"):
            return dict(item)
        item.update({
            "consequence_assessed": True,
            "consequence_assessed_at": _now(),
            "consequence_game_date": str(game_date or ""),
            "consequence_outcome": str(outcome),
            "consequence_profit": from_minor(to_minor(max(0.0, float(profit)))),
            "consequence_bet_ids": [str(value) for value in bet_ids if value],
        })
        save_economy(payload)
        return dict(item)


def active_doping_items() -> list[dict[str, Any]]:
    with _LOCK:
        return [
            dict(item) for item in load_economy()["inventory"]
            if item.get("status") == "active" and item.get("sku") in {"doping", "team_doping"}
        ]


def active_flu_items() -> list[dict[str, Any]]:
    with _LOCK:
        return [
            dict(item) for item in load_economy()["inventory"]
            if item.get("status") == "active"
            and item.get("sku") == "opponent_flu"
            and not item.get("effect_applied")
        ]


def resolve_flu_item(item_id: str, *, applied: bool, detail: str) -> dict[str, Any]:
    with _LOCK:
        payload = load_economy()
        item = next((row for row in payload["inventory"] if row.get("id") == item_id), None)
        if not item or item.get("sku") != "opponent_flu":
            raise ValueError("病毒包裹不存在")
        if applied:
            item.update({"effect_applied": True, "effect_applied_at": _now(), "effect_detail": str(detail)})
        else:
            for field in (
                "player_id", "player_name", "player_address", "fixture_key", "fixture_identity",
                "fixture_date", "fixture_name", "kickoff_minutes", "kickoff_time", "assigned_at",
                "activated_at", "managed_team_id", "managed_team_name", "managed_team_address",
                "opponent_team_id", "opponent_team_name", "opponent_team_address", "effect_mode",
            ):
                item.pop(field, None)
            item.update({"status": "available", "effect_error": str(detail), "refunded_at": _now()})
        save_economy(payload)
        return dict(item)


def refund_active_sku(sku: str, reason: str) -> int:
    with _LOCK:
        payload = load_economy(); refunded = 0
        for item in payload["inventory"]:
            if item.get("status") == "active" and item.get("sku") == sku:
                for field in ("fixture_key", "fixture_identity", "fixture_date", "fixture_name", "assigned_at", "activated_at"):
                    item.pop(field, None)
                item.update({"status": "available", "effect_error": reason, "refunded_at": _now()})
                refunded += 1
        if refunded:
            save_economy(payload)
        return refunded


def refund_active_items(reason: str) -> int:
    with _LOCK:
        payload = load_economy(); refunded = 0
        for item in payload["inventory"]:
            if item.get("status") == "active":
                item.update({"status": "available", "effect_error": reason, "refunded_at": _now()}); refunded += 1
        if refunded:
            save_economy(payload)
        return refunded


def _return_fixture_item(item: dict[str, Any], reason: str, timestamp_field: str) -> None:
    for field in (
        "player_id", "player_name", "player_ids", "player_count",
        "player_address", "player_targets", "fixture_key", "fixture_identity",
        "fixture_date", "fixture_name", "kickoff_minutes", "kickoff_time",
        "assigned_at", "activated_at", "managed_team_address", "managed_team_id",
        "managed_team_name", "opponent_team_id", "opponent_team_name",
        "opponent_team_address", "effect_mode", "effect_applied",
        "effect_applied_at", "effect_detail",
    ):
        item.pop(field, None)
    item.update({
        "status": "available",
        timestamp_field: _now(),
        "effect_error": str(reason),
    })


def reconcile_inventory(
    game_date: str, fixtures: dict[str, dict[str, str]], *,
    activate_referee: bool = True,
) -> dict[str, Any]:
    """Advance scheduled item lifecycle; native memory effect is handled separately."""
    with _LOCK:
        payload = load_economy(); changed = False
        for item in list(payload["inventory"]):
            status, target = item.get("status"), str(item.get("fixture_date") or "")
            activation_target = target
            if item.get("sku") == "opponent_flu" and target:
                activation_target = (date.fromisoformat(target) - timedelta(days=1)).isoformat()
            if item.get("sku") in {"red_bull", "team_red_bull"} and status in {"scheduled", "active"} and item.get("hook_version") != 2:
                item.update({"status": "available", "migration_refund_at": _now(), "effect_error": "旧版不安全已停用"})
                changed = True
                continue
            current = fixtures.get(str(item.get("fixture_identity") or ""))
            if status in {"scheduled", "active"} and current and current.get("date") != target:
                _return_fixture_item(item, "比赛已推迟，道具已退回库存", "postponed_fixture_at")
                changed = True
                continue
            if status == "scheduled" and not current and game_date < target:
                _return_fixture_item(item, "比赛已取消，道具已退回库存", "cancelled_at")
                changed = True
                continue
            elif status == "scheduled" and game_date == activation_target:
                if activate_referee or item.get("sku") not in _REFEREE_FIXTURE_ITEM_SKUS:
                    item.update({"status": "active", "activated_at": _now()}); changed = True
            elif status == "scheduled" and activation_target and game_date > activation_target:
                # The item never reached its match-day active state. Return it
                # instead of retaining a permanently scheduled, inert item.
                _return_fixture_item(
                    item, "比赛已经结束，道具未生效并已退回库存",
                    "missed_fixture_at",
                )
                changed = True
            elif status == "active" and game_date > target:
                if item.get("sku") in _INTEGRITY_MATCH_ITEM_SKUS:
                    history = payload.setdefault("match_item_history", [])
                    if not any(str(row.get("id") or "") == str(item.get("id") or "") for row in history):
                        history.append({**item, "status": "consumed", "consumed_game_date": game_date, "consumed_at": _now()})
                        payload["match_item_history"] = history[-500:]
                payload["inventory"].remove(item)
                _transaction(payload, "item_consumed", 0, item_id=item.get("id"), player_id=item.get("player_id")); changed = True
        if changed:
            save_economy(payload)
        return public_economy()
