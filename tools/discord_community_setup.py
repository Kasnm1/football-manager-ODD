"""Safely provision the public FMODD Discord community structure.

The tool is deliberately additive: it reuses matching roles/channels and never
deletes, renames, or moves existing Discord resources.  For seeded text
channels, it may merge the bot's own required channel permissions without
changing any member or role overwrite.  It uses only Discord's HTTP API and
reads the bot token from an environment variable or a hidden interactive
prompt.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import getpass
import json
import os
import sys
import time
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


API_BASE = "https://discord.com/api/v10"
DEFAULT_GUILD_ID = "1546807720953253888"
TOKEN_ENV = "FMODD_DISCORD_BOT_TOKEN"
AUDIT_REASON = "FMODD community bootstrap"

GUILD_TEXT = 0
GUILD_CATEGORY = 4
GUILD_FORUM = 15

MANAGE_CHANNELS = 1 << 4
MANAGE_GUILD = 1 << 5
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
MANAGE_MESSAGES = 1 << 13
EMBED_LINKS = 1 << 14
READ_MESSAGE_HISTORY = 1 << 16
MANAGE_ROLES = 1 << 28
CREATE_PUBLIC_THREADS = 1 << 35
SEND_MESSAGES_IN_THREADS = 1 << 38
PIN_MESSAGES = 1 << 51

REQUIRED_BOT_PERMISSIONS = {
    "Manage Channels": MANAGE_CHANNELS,
    "View Channels": VIEW_CHANNEL,
    "Send Messages": SEND_MESSAGES,
    "Manage Messages": MANAGE_MESSAGES,
    "Read Message History": READ_MESSAGE_HISTORY,
    "Manage Roles": MANAGE_ROLES,
    "Pin Messages": PIN_MESSAGES,
}

BOT_CHANNEL_ALLOW = (
    VIEW_CHANNEL
    | SEND_MESSAGES
    | READ_MESSAGE_HISTORY
    | MANAGE_MESSAGES
    | MANAGE_CHANNELS
    | EMBED_LINKS
    | CREATE_PUBLIC_THREADS
    | SEND_MESSAGES_IN_THREADS
    | PIN_MESSAGES
)


class DiscordApiError(RuntimeError):
    """A Discord HTTP API request failed."""


@dataclass(frozen=True)
class RoleSpec:
    name: str
    color: int


@dataclass(frozen=True)
class TagSpec:
    name: str
    moderated: bool = False


@dataclass(frozen=True)
class ChannelSpec:
    category: str
    name: str
    channel_type: int
    topic: str
    access: str
    tags: tuple[TagSpec, ...] = ()
    seed_key: str | None = None


@dataclass
class Plan:
    guild_id: str
    guild_name: str
    community_enabled: bool
    operations: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def safe_to_apply(self) -> bool:
        return not self.conflicts


ROLE_SPECS = (
    RoleSpec("FM24", 0x5865F2),
    RoleSpec("FM26", 0x57F287),
    RoleSpec("Steam", 0x1B2838),
    RoleSpec("Epic Games", 0x313131),
    RoleSpec("Xbox / Microsoft Store", 0x107C10),
    RoleSpec("Beta Tester", 0xFEE75C),
)

CATEGORY_ACCESS = {
    "START HERE": "read_only",
    "FEEDBACK": "forum",
    "COMMUNITY": "writable",
}

CHANNEL_SPECS = (
    ChannelSpec(
        "START HERE",
        "welcome",
        GUILD_TEXT,
        "Start here: what FMODD is, essential safety information, and where to go next.",
        "read_only",
        seed_key="welcome",
    ),
    ChannelSpec(
        "START HERE",
        "rules",
        GUILD_TEXT,
        "Community rules and privacy requirements. New members should read this first.",
        "read_only",
        seed_key="rules",
    ),
    ChannelSpec(
        "START HERE",
        "announcements",
        GUILD_TEXT,
        "Official FMODD releases, compatibility changes, and maintenance notices.",
        "read_only",
        seed_key="announcements",
    ),
    ChannelSpec(
        "START HERE",
        "getting-started",
        GUILD_TEXT,
        "Installation, first connection, safe use, and uninstall guidance.",
        "read_only",
        seed_key="getting-started",
    ),
    ChannelSpec(
        "START HERE",
        "faq",
        GUILD_TEXT,
        "Short answers to common FMODD installation, compatibility, safety, and support questions.",
        "read_only",
        seed_key="faq",
    ),
    ChannelSpec(
        "FEEDBACK",
        "bug-reports",
        GUILD_FORUM,
        "Use the report template. Never upload passwords, account tokens, full memory dumps, or Football Manager save files.",
        "forum",
        (
            TagSpec("FM24"),
            TagSpec("FM26"),
            TagSpec("Steam"),
            TagSpec("Epic"),
            TagSpec("Xbox / MS Store"),
            TagSpec("Save Risk"),
            TagSpec("Needs Info", moderated=True),
            TagSpec("Reproduced", moderated=True),
            TagSpec("Fixed", moderated=True),
        ),
    ),
    ChannelSpec(
        "FEEDBACK",
        "feature-requests",
        GUILD_FORUM,
        "One feature idea per post. Explain the player problem and expected result.",
        "forum",
        (
            TagSpec("Odds"),
            TagSpec("Club Management"),
            TagSpec("Player Tools"),
            TagSpec("Save Utilities"),
            TagSpec("UI / UX"),
            TagSpec("Planned", moderated=True),
        ),
    ),
    ChannelSpec(
        "COMMUNITY",
        "general",
        GUILD_TEXT,
        "Please keep conversations friendly.",
        "writable",
        seed_key="general",
    ),
)

SEED_MESSAGES = {
    "welcome": """# Welcome to FMODD

FMODD's betting and odds features are intended solely to enrich the Football Manager experience. They are unrelated to real-world gambling or real-money transactions. Never use this tool for any form of real-world gambling, illegal betting or other unlawful activity.

Some features modify game data and may corrupt a save or cause data loss. Back up your save before use. No person or organisation may sell this tool, offer paid licences for it or bundle it for sale.

FMODD is still being updated and improved. If you encounter an error, want to report an issue, get the latest version or join the discussion, visit the [official website](https://fmodd.com/) and this community.

If FMODD helps you enjoy the game more, you can [support the author](https://fmodd.com/donate) from Settings. Your support is the author's greatest motivation to keep updating FMODD.

FMODD is an independent community-made project and is not affiliated with Sports Interactive or SEGA.

FMODD Community Setup • welcome • v1""",
    "rules": """# FMODD Community Rules

**1. Be respectful**
No harassment, hate speech, discrimination, threats, or personal attacks.

**2. Use the correct channels**
Keep problem reports and feature requests in their designated forums.

**3. No spam or unsolicited promotion**
Do not advertise products, servers, referral links, or services without staff approval.

**4. Use official downloads only**
Do not distribute modified FMODD builds, unofficial mirrors, cracks, or suspicious executable files.

**5. Protect private information**
Never post passwords, account tokens, licence keys, personal information, or unredacted private paths.

**6. Share only necessary diagnostic data**
Do not upload Football Manager save files or full memory dumps. Redact personal information from logs and screenshots.

**7. Provide reproducible reports**
Include your exact FM build, platform, FMODD version, reproduction steps, original error, and whether the issue may have affected your save.

**8. Use FMODD responsibly**
Back up your save before write operations. Compatibility is limited to builds listed on the official release page.

FMODD Community Setup • rules • v1""",
    "announcements": """# Official FMODD Updates

Release announcements, compatibility changes, and maintenance notices will be posted here. Download builds only from links published through official FMODD channels.

FMODD Community Setup • announcements • v1""",
    "getting-started": """# Getting Started

**Before you start**
• FMODD currently supports FM26 and FM24 on Steam and Epic Games.
• The available download is for Windows; macOS is not yet available.
• Back up your save before modifying game data.

1. Download the Windows build from the [official Download page](https://fmodd.com/download) and extract it to a folder you can read and write normally.
2. Start Football Manager, open the career save you want to use, and wait for the main save screen to finish loading.
3. Start FMODD and check the top-left corner for the detected game version and connection status.
4. If the game is not detected, close FMODD while keeping Football Manager and the save open, then run FMODD as administrator. Both programs must use the same permission level.
5. After loading another save, use **Change save and refresh** in FMODD and verify the save and manager information again.
6. If something fails, create one post in **Bug Reports** with the original error, FM platform, FMODD version, and reproduction steps.

FMODD Community Setup • getting-started • v1""",
    "faq": """# Frequently Asked Questions

**Which Football Manager versions and platforms are supported?**
FMODD currently supports FM26 and FM24 on Steam and Epic Games. Check the [Download page](https://fmodd.com/download) for the latest build details.

**Is there a macOS version?**
Not yet. The current download is for Windows.

**How do I connect FMODD?**
Open a career save in Football Manager, wait for the main save screen, then start FMODD. If detection fails, keep the game open and run FMODD as administrator at the same permission level as Football Manager.

**How do I switch saves?**
Load the other save in Football Manager, wait for it to finish displaying, then use **Change save and refresh** in FMODD.

**Where should I ask for help or suggest a feature?**
Use **Bug Reports** for problems and **Feature Requests** for ideas. Include the original error and enough version and reproduction information to investigate safely.

**How can I support the project?**
Use Ko-fi or the crypto donation option on the [Donate page](https://fmodd.com/donate).

**Why might some translations sound unnatural?**
FMODD was originally developed in Chinese. Please report awkward wording through feedback so it can be improved.

Never upload passwords, tokens, licence keys, full memory dumps, personal information, or Football Manager save files. See the maintained [official FAQ](https://fmodd.com/faq).

FMODD Community Setup • faq • v1""",
    "general": "Please keep conversations friendly.",
}

MANUAL_FINISH = (
    "Enable Community in Discord Server Settings if the audit reports it disabled.",
    "In Safety Setup, enable Rules Screening and paste the eight rules from #rules.",
    "In Onboarding, add at least seven default channels and keep at least five writable; use the FM and platform roles created by this tool.",
    "In Server Guide, use #welcome, #getting-started, and #faq as resources and add 3-5 new-member tasks.",
)

SIMPLIFY_CHANNELS = (
    ("compatibility", GUILD_TEXT),
    ("support", GUILD_FORUM),
    ("guides-and-tutorials", GUILD_FORUM),
    ("introductions", GUILD_TEXT),
    ("screenshots-and-stories", GUILD_TEXT),
    ("chinese-chat", GUILD_TEXT),
    ("deutsch-chat", GUILD_TEXT),
    ("spanish-chat", GUILD_TEXT),
    ("french-chat", GUILD_TEXT),
    ("portuguese-chat", GUILD_TEXT),
    ("korean-chat", GUILD_TEXT),
    ("japanese-chat", GUILD_TEXT),
    ("russian-chat", GUILD_TEXT),
)


def normalize_name(value: str) -> str:
    return value.strip().casefold()


def index_named(items: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        result.setdefault(normalize_name(str(item.get("name", ""))), []).append(item)
    return result


def build_plan(
    guild: dict[str, Any],
    channels: list[dict[str, Any]],
    roles: list[dict[str, Any]],
) -> Plan:
    plan = Plan(
        guild_id=str(guild["id"]),
        guild_name=str(guild.get("name") or "Unknown server"),
        community_enabled="COMMUNITY" in set(guild.get("features") or []),
    )
    role_index = index_named(roles)
    channel_index = index_named(channels)

    for spec in ROLE_SPECS:
        matches = role_index.get(normalize_name(spec.name), [])
        if not matches:
            plan.operations.append(f"CREATE role: {spec.name}")
        elif len(matches) == 1:
            plan.operations.append(f"REUSE role: {spec.name} ({matches[0]['id']})")
        else:
            plan.conflicts.append(f"Duplicate role name: {spec.name}")

    for category, access in CATEGORY_ACCESS.items():
        matches = channel_index.get(normalize_name(category), [])
        typed = [item for item in matches if int(item.get("type", -1)) == GUILD_CATEGORY]
        if not matches:
            plan.operations.append(f"CREATE category: {category} [{access}]")
        elif len(typed) == 1:
            plan.operations.append(f"REUSE category: {category} ({typed[0]['id']})")
        else:
            plan.conflicts.append(f"Category name/type conflict: {category}")

    for spec in CHANNEL_SPECS:
        matches = channel_index.get(normalize_name(spec.name), [])
        typed = [item for item in matches if int(item.get("type", -1)) == spec.channel_type]
        kind = "forum" if spec.channel_type == GUILD_FORUM else "channel"
        if not matches:
            plan.operations.append(f"CREATE {kind}: #{spec.name} in {spec.category}")
        elif len(typed) == 1:
            plan.operations.append(f"REUSE {kind}: #{spec.name} ({typed[0]['id']})")
        else:
            plan.conflicts.append(f"Channel name/type conflict: #{spec.name}")

    if not plan.community_enabled:
        plan.warnings.append(
            "Community is not enabled. Discord forum creation may fail until it is enabled manually."
        )
    plan.warnings.extend(MANUAL_FINISH)
    return plan


def render_plan(plan: Plan) -> str:
    lines = [
        f"Target: {plan.guild_name} ({plan.guild_id})",
        f"Community enabled: {'yes' if plan.community_enabled else 'no'}",
        "",
        "Planned additive operations:",
    ]
    lines.extend(f"  - {item}" for item in plan.operations)
    if plan.conflicts:
        lines.extend(("", "Blocking conflicts:"))
        lines.extend(f"  - {item}" for item in plan.conflicts)
    if plan.warnings:
        lines.extend(("", "Manual/safety notes:"))
        lines.extend(f"  - {item}" for item in plan.warnings)
    lines.append("")
    lines.append("No existing Discord resource will be deleted, renamed, or moved.")
    lines.append("Apply may merge required permissions into the bot's own channel overwrite only.")
    return "\n".join(lines)


class DiscordClient:
    def __init__(self, token: str) -> None:
        token = token.strip()
        if not token:
            raise ValueError("Bot token is empty")
        self._token = token

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | list[dict[str, Any]] | None = None,
        *,
        reason: str | None = None,
    ) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bot {self._token}",
            "User-Agent": "FMODD-Community-Setup/1.0",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        if reason:
            headers["X-Audit-Log-Reason"] = quote(reason, safe="")

        for attempt in range(4):
            request = Request(API_BASE + path, data=data, headers=headers, method=method)
            try:
                with urlopen(request, timeout=30) as response:
                    raw = response.read()
                    return json.loads(raw.decode("utf-8")) if raw else None
            except HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                if exc.code == 429 and attempt < 3:
                    try:
                        retry_after = float(json.loads(raw).get("retry_after", 1.0))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        retry_after = 1.0
                    time.sleep(min(max(retry_after, 0.2), 10.0))
                    continue
                message = raw
                try:
                    parsed = json.loads(raw)
                    message = str(parsed.get("message") or parsed)
                except json.JSONDecodeError:
                    pass
                raise DiscordApiError(f"Discord API {method} {path} failed ({exc.code}): {message}") from exc
            except URLError as exc:
                raise DiscordApiError(f"Could not reach Discord API: {exc.reason}") from exc
        raise DiscordApiError("Discord API rate-limit retry budget exhausted")


def get_token() -> str:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        return token
    if not sys.stdin.isatty():
        raise RuntimeError(
            f"No interactive terminal. Set {TOKEN_ENV} locally without printing or committing it."
        )
    return getpass.getpass("Paste the FMODD Assistant bot token (hidden): ").strip()


def bot_permissions(
    guild: dict[str, Any], current_member: dict[str, Any]
) -> int:
    role_ids = set(str(item) for item in current_member.get("roles") or [])
    role_ids.add(str(guild["id"]))
    permissions = 0
    for role in guild.get("roles") or []:
        if str(role.get("id")) in role_ids:
            permissions |= int(role.get("permissions") or 0)
    return permissions


def missing_permission_names(permissions: int) -> list[str]:
    return [
        name
        for name, bit in REQUIRED_BOT_PERMISSIONS.items()
        if permissions & bit != bit
    ]


def permission_overwrites(access: str, guild_id: str, bot_id: str) -> list[dict[str, Any]]:
    if access == "read_only":
        everyone_allow = VIEW_CHANNEL | READ_MESSAGE_HISTORY
        everyone_deny = SEND_MESSAGES | CREATE_PUBLIC_THREADS | SEND_MESSAGES_IN_THREADS
    elif access == "forum":
        everyone_allow = (
            VIEW_CHANNEL
            | SEND_MESSAGES
            | READ_MESSAGE_HISTORY
            | CREATE_PUBLIC_THREADS
            | SEND_MESSAGES_IN_THREADS
        )
        everyone_deny = 0
    else:
        everyone_allow = VIEW_CHANNEL | SEND_MESSAGES | READ_MESSAGE_HISTORY
        everyone_deny = 0
    return [
        {
            "id": guild_id,
            "type": 0,
            "allow": str(everyone_allow),
            "deny": str(everyone_deny),
        },
        {
            "id": bot_id,
            "type": 1,
            "allow": str(BOT_CHANNEL_ALLOW),
            "deny": "0",
        },
    ]


def resolve_single(
    items: Iterable[dict[str, Any]], name: str, expected_type: int | None = None
) -> dict[str, Any] | None:
    matches = [item for item in items if normalize_name(str(item.get("name", ""))) == normalize_name(name)]
    if expected_type is not None:
        matches = [item for item in matches if int(item.get("type", -1)) == expected_type]
    if len(matches) > 1:
        raise RuntimeError(f"More than one matching Discord resource exists: {name}")
    return matches[0] if matches else None


def create_missing_roles(
    client: DiscordClient, guild_id: str, roles: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    for spec in ROLE_SPECS:
        if resolve_single(roles, spec.name) is not None:
            continue
        created = client.request(
            "POST",
            f"/guilds/{guild_id}/roles",
            {
                "name": spec.name,
                "permissions": "0",
                "color": spec.color,
                "hoist": False,
                "mentionable": False,
            },
            reason=AUDIT_REASON,
        )
        roles.append(created)
        print(f"Created role: {spec.name}")
    return roles


def create_missing_categories(
    client: DiscordClient,
    guild_id: str,
    bot_id: str,
    channels: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    for name, access in CATEGORY_ACCESS.items():
        if resolve_single(channels, name, GUILD_CATEGORY) is not None:
            continue
        created = client.request(
            "POST",
            f"/guilds/{guild_id}/channels",
            {
                "name": name,
                "type": GUILD_CATEGORY,
                "permission_overwrites": permission_overwrites(access, guild_id, bot_id),
            },
            reason=AUDIT_REASON,
        )
        channels.append(created)
        print(f"Created category: {name}")
    return channels


def channel_payload(
    spec: ChannelSpec, parent_id: str, guild_id: str, bot_id: str
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": spec.name,
        "type": spec.channel_type,
        "topic": spec.topic,
        "parent_id": parent_id,
        "permission_overwrites": permission_overwrites(spec.access, guild_id, bot_id),
    }
    if spec.channel_type == GUILD_FORUM:
        payload.update(
            {
                "available_tags": [
                    {"name": tag.name, "moderated": tag.moderated}
                    for tag in spec.tags
                ],
                "default_sort_order": 0,
                "default_forum_layout": 1,
                "default_auto_archive_duration": 10080,
            }
        )
    return payload


def create_missing_channels(
    client: DiscordClient,
    guild_id: str,
    bot_id: str,
    channels: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    for spec in CHANNEL_SPECS:
        if resolve_single(channels, spec.name, spec.channel_type) is not None:
            continue
        parent = resolve_single(channels, spec.category, GUILD_CATEGORY)
        if parent is None:
            raise RuntimeError(f"Missing required category after creation: {spec.category}")
        created = client.request(
            "POST",
            f"/guilds/{guild_id}/channels",
            channel_payload(spec, str(parent["id"]), guild_id, bot_id),
            reason=AUDIT_REASON,
        )
        channels.append(created)
        print(f"Created {'forum' if spec.channel_type == GUILD_FORUM else 'channel'}: #{spec.name}")
    return channels


def migrate_simplified_layout(
    client: DiscordClient, channels: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    feedback = resolve_single(channels, "FEEDBACK", GUILD_CATEGORY)
    legacy_feedback = resolve_single(channels, "SUPPORT", GUILD_CATEGORY)
    if feedback is None and legacy_feedback is not None:
        feedback = client.request(
            "PATCH",
            f"/channels/{legacy_feedback['id']}",
            {"name": "FEEDBACK"},
            reason=AUDIT_REASON,
        )
        channels[channels.index(legacy_feedback)] = feedback
        print("Renamed category: SUPPORT -> FEEDBACK")

    community = resolve_single(channels, "COMMUNITY", GUILD_CATEGORY)
    if community is None:
        raise RuntimeError("Cannot simplify without the COMMUNITY category")
    return channels


def channel_has_member_content(
    client: DiscordClient,
    guild_id: str,
    channel: dict[str, Any],
    bot_id: str,
) -> bool:
    channel_id = str(channel["id"])
    channel_type = int(channel.get("type", -1))
    if channel_type == GUILD_TEXT:
        before = ""
        while True:
            suffix = f"&before={before}" if before else ""
            messages = client.request(
                "GET", f"/channels/{channel_id}/messages?limit=100{suffix}"
            )
            if any(
                int(message.get("type") or 0) == 0
                and str((message.get("author") or {}).get("id")) != bot_id
                for message in messages
            ):
                return True
            if len(messages) < 100:
                return False
            before = str(messages[-1]["id"])

    if channel_type == GUILD_FORUM:
        active = client.request("GET", f"/guilds/{guild_id}/threads/active")
        if any(str(thread.get("parent_id")) == channel_id for thread in active.get("threads", [])):
            return True
        archived = client.request(
            "GET", f"/channels/{channel_id}/threads/archived/public?limit=100"
        )
        return bool(archived.get("threads")) or bool(archived.get("has_more"))

    raise RuntimeError(f"Unsupported prune target type for #{channel.get('name')}")


def prune_simplified_layout(
    client: DiscordClient,
    guild_id: str,
    channels: list[dict[str, Any]],
    bot_id: str,
) -> list[dict[str, Any]]:
    for name, channel_type in SIMPLIFY_CHANNELS:
        channel = resolve_single(channels, name, channel_type)
        if channel is None:
            continue
        if channel_has_member_content(client, guild_id, channel, bot_id):
            print(f"Delete skipped; member content exists: #{name}", file=sys.stderr)
            continue
        client.request(
            "DELETE",
            f"/channels/{channel['id']}",
            reason=AUDIT_REASON,
        )
        channels.remove(channel)
        print(f"Deleted redundant channel: #{name}")

    for category_name in ("LANGUAGE LOUNGES", "SUPPORT"):
        category = resolve_single(channels, category_name, GUILD_CATEGORY)
        if category is None:
            continue
        if any(str(item.get("parent_id")) == str(category["id"]) for item in channels):
            print(f"Delete skipped; category is not empty: {category_name}", file=sys.stderr)
            continue
        client.request(
            "DELETE",
            f"/channels/{category['id']}",
            reason=AUDIT_REASON,
        )
        channels.remove(category)
        print(f"Deleted redundant category: {category_name}")
    return channels


def ensure_seed_channel_permissions(
    client: DiscordClient, channels: list[dict[str, Any]], bot_id: str
) -> None:
    """Merge only the bot member's required permissions on seeded text channels."""
    for spec in CHANNEL_SPECS:
        if not spec.seed_key:
            continue
        channel = resolve_single(channels, spec.name, GUILD_TEXT)
        if channel is None:
            raise RuntimeError(f"Cannot prepare missing channel: #{spec.name}")

        overwrites = list(channel.get("permission_overwrites") or [])
        current = next(
            (
                item
                for item in overwrites
                if str(item.get("id")) == bot_id and int(item.get("type", -1)) == 1
            ),
            None,
        )
        current_allow = int((current or {}).get("allow") or 0)
        current_deny = int((current or {}).get("deny") or 0)
        next_allow = current_allow | BOT_CHANNEL_ALLOW
        next_deny = current_deny & ~BOT_CHANNEL_ALLOW
        if next_allow == current_allow and next_deny == current_deny:
            continue

        client.request(
            "PUT",
            f"/channels/{channel['id']}/permissions/{bot_id}",
            {"type": 1, "allow": str(next_allow), "deny": str(next_deny)},
            reason=AUDIT_REASON,
        )
        print(f"Updated bot-only permissions: #{spec.name}")


def find_seed_message(
    messages: Iterable[dict[str, Any]], seed_key: str, bot_id: str | None = None
) -> dict[str, Any] | None:
    marker = f"FMODD Community Setup • {seed_key} • v1"
    for message in messages:
        if int(message.get("type") or 0) != 0:
            continue
        if marker in str(message.get("content") or ""):
            return message
        if bot_id and str((message.get("author") or {}).get("id")) == bot_id:
            return message
    return None


def seed_exists(
    messages: Iterable[dict[str, Any]], seed_key: str, bot_id: str | None = None
) -> bool:
    return find_seed_message(messages, seed_key, bot_id) is not None


def try_pin_message(
    client: DiscordClient, channel_id: str, message: dict[str, Any], channel_name: str
) -> None:
    if message.get("pinned"):
        return
    try:
        client.request(
            "PUT",
            f"/channels/{channel_id}/pins/{message['id']}",
            reason=AUDIT_REASON,
        )
        print(f"Pinned starter content: #{channel_name}")
    except DiscordApiError as exc:
        print(
            f"Pin skipped: #{channel_name} ({exc})",
            file=sys.stderr,
        )


def seed_resource_messages(
    client: DiscordClient, channels: list[dict[str, Any]], bot_id: str
) -> None:
    for spec in CHANNEL_SPECS:
        if not spec.seed_key:
            continue
        channel = resolve_single(channels, spec.name, GUILD_TEXT)
        if channel is None:
            raise RuntimeError(f"Cannot seed missing channel: #{spec.name}")
        channel_id = str(channel["id"])
        messages = client.request("GET", f"/channels/{channel_id}/messages?limit=100")
        existing = find_seed_message(messages, spec.seed_key, bot_id)
        if existing is not None:
            if str(existing.get("content") or "") != SEED_MESSAGES[spec.seed_key]:
                existing = client.request(
                    "PATCH",
                    f"/channels/{channel_id}/messages/{existing['id']}",
                    {"content": SEED_MESSAGES[spec.seed_key], "allowed_mentions": {"parse": []}},
                )
                print(f"Updated starter content: #{spec.name}")
                try_pin_message(client, channel_id, existing, spec.name)
                continue
            print(f"Reused seeded message: #{spec.name}")
            try_pin_message(client, channel_id, existing, spec.name)
            continue
        try:
            created = client.request(
                "POST",
                f"/channels/{channel_id}/messages",
                {"content": SEED_MESSAGES[spec.seed_key], "allowed_mentions": {"parse": []}},
            )
        except DiscordApiError as exc:
            print(
                f"Starter content skipped: #{spec.name} ({exc})",
                file=sys.stderr,
            )
            continue
        print(f"Posted starter content: #{spec.name}")
        try_pin_message(client, channel_id, created, spec.name)


def load_snapshot(client: DiscordClient, guild_id: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    current_user = client.request("GET", "/users/@me")
    guild = client.request("GET", f"/guilds/{guild_id}")
    channels = client.request("GET", f"/guilds/{guild_id}/channels")
    current_member = client.request("GET", f"/guilds/{guild_id}/members/{current_user['id']}")
    return guild, channels, list(guild.get("roles") or []), current_member


def apply_plan(
    client: DiscordClient,
    guild: dict[str, Any],
    channels: list[dict[str, Any]],
    roles: list[dict[str, Any]],
    current_member: dict[str, Any],
) -> None:
    guild_id = str(guild["id"])
    bot_id = str(current_member["user"]["id"])
    missing = missing_permission_names(bot_permissions(guild, current_member))
    if missing:
        raise RuntimeError("Bot is missing required permissions: " + ", ".join(missing))
    if "COMMUNITY" not in set(guild.get("features") or []):
        raise RuntimeError(
            "Community is not enabled. Enable it in Discord Server Settings before --apply."
        )

    create_missing_roles(client, guild_id, roles)
    create_missing_categories(client, guild_id, bot_id, channels)
    create_missing_channels(client, guild_id, bot_id, channels)
    ensure_seed_channel_permissions(client, channels, bot_id)
    seed_resource_messages(client, channels, bot_id)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit or additively provision the FMODD Discord community."
    )
    parser.add_argument("--guild-id", default=DEFAULT_GUILD_ID)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--audit", action="store_true", help="Print current server facts only")
    mode.add_argument("--apply", action="store_true", help="Create missing resources after confirmation")
    mode.add_argument(
        "--simplify",
        action="store_true",
        help="Safely migrate to FEEDBACK and COMMUNITY, pruning only channels without member content",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not str(args.guild_id).isdigit():
        print("Guild ID must contain digits only.", file=sys.stderr)
        return 2

    try:
        client = DiscordClient(get_token())
        guild, channels, roles, current_member = load_snapshot(client, str(args.guild_id))
        permissions = bot_permissions(guild, current_member)
        missing = missing_permission_names(permissions)
        if args.simplify:
            print(f"Target: {guild.get('name') or 'Unknown server'} ({args.guild_id})")
            print("Simplify: rename SUPPORT to FEEDBACK; move language chats into COMMUNITY.")
            print("Delete if empty: " + ", ".join(f"#{name}" for name, _ in SIMPLIFY_CHANNELS) + ".")
            print("Delete empty legacy categories: LANGUAGE LOUNGES and SUPPORT.")
            print("Channels containing member content will be skipped.")
            print("Bot permission check: " + ("OK" if not missing else "missing " + ", ".join(missing)))
            confirmation = input(f"Type SIMPLIFY {args.guild_id} to continue: ").strip()
            if confirmation != f"SIMPLIFY {args.guild_id}":
                print("Simplify cancelled; no changes were made.")
                return 1
            if missing:
                raise RuntimeError("Bot is missing required permissions: " + ", ".join(missing))
            bot_id = str(current_member["user"]["id"])
            migrate_simplified_layout(client, channels)
            prune_simplified_layout(client, str(args.guild_id), channels, bot_id)
            apply_plan(client, guild, channels, roles, current_member)
            print("\nFMODD Discord community simplification completed.")
            return 0

        plan = build_plan(guild, channels, roles)
        print(render_plan(plan))
        print("Bot permission check: " + ("OK" if not missing else "missing " + ", ".join(missing)))

        if args.audit or not args.apply:
            return 0 if plan.safe_to_apply else 1
        if not plan.safe_to_apply:
            print("Apply refused because blocking name/type conflicts exist.", file=sys.stderr)
            return 1
        confirmation = input(f"Type the server ID {args.guild_id} to apply: ").strip()
        if confirmation != str(args.guild_id):
            print("Apply cancelled; no changes were made.")
            return 1
        apply_plan(client, guild, channels, roles, current_member)
        print("\nFMODD Discord community bootstrap completed.")
        print("Finish the native Discord-only steps listed in the plan above.")
        return 0
    except (DiscordApiError, RuntimeError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
