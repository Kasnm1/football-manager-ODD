from tools.discord_community_setup import (
    CHANNEL_SPECS,
    DiscordApiError,
    GUILD_CATEGORY,
    GUILD_FORUM,
    GUILD_TEXT,
    PIN_MESSAGES,
    ROLE_SPECS,
    SEED_MESSAGES,
    build_plan,
    channel_payload,
    ensure_seed_channel_permissions,
    migrate_simplified_layout,
    missing_permission_names,
    permission_overwrites,
    prune_simplified_layout,
    seed_resource_messages,
    seed_exists,
)


def _guild(*, community=True):
    return {
        "id": "123",
        "name": "FMODD",
        "features": ["COMMUNITY"] if community else [],
        "roles": [],
    }


def test_empty_server_plan_is_additive_and_warns_when_community_is_disabled():
    plan = build_plan(_guild(community=False), [], [])

    assert plan.safe_to_apply
    assert sum(item.startswith("CREATE role") for item in plan.operations) == len(ROLE_SPECS)
    assert sum(item.startswith("CREATE category") for item in plan.operations) == 3
    assert sum("CREATE channel" in item or "CREATE forum" in item for item in plan.operations) == len(CHANNEL_SPECS)
    assert any("Community is not enabled" in item for item in plan.warnings)


def test_existing_matching_resources_are_reused():
    roles = [{"id": f"r{index}", "name": spec.name} for index, spec in enumerate(ROLE_SPECS)]
    channels = [
        {"id": "c1", "name": "START HERE", "type": GUILD_CATEGORY},
        {"id": "c2", "name": "FEEDBACK", "type": GUILD_CATEGORY},
        {"id": "c3", "name": "COMMUNITY", "type": GUILD_CATEGORY},
    ]
    for index, spec in enumerate(CHANNEL_SPECS):
        channels.append({"id": f"x{index}", "name": spec.name, "type": spec.channel_type})

    plan = build_plan(_guild(), channels, roles)

    assert plan.safe_to_apply
    assert not any(item.startswith("CREATE") for item in plan.operations)
    assert all(item.startswith("REUSE") for item in plan.operations)


def test_same_channel_name_with_wrong_type_blocks_apply():
    channels = [{"id": "1", "name": "bug-reports", "type": GUILD_TEXT}]

    plan = build_plan(_guild(), channels, [])

    assert not plan.safe_to_apply
    assert "Channel name/type conflict: #bug-reports" in plan.conflicts


def test_read_only_overwrite_denies_member_send_but_allows_bot_send():
    everyone, bot = permission_overwrites("read_only", "guild", "bot")

    assert int(everyone["deny"]) != 0
    assert int(bot["allow"]) != 0
    assert everyone["id"] == "guild"
    assert bot["id"] == "bot"
    assert int(bot["allow"]) & PIN_MESSAGES


def test_retired_language_channels_stay_absent_and_general_copy_is_exact():
    language_channels = {
        "chinese-chat",
        "deutsch-chat",
        "spanish-chat",
        "french-chat",
        "portuguese-chat",
        "korean-chat",
        "japanese-chat",
        "russian-chat",
    }
    seeded_keys = {spec.seed_key for spec in CHANNEL_SPECS if spec.seed_key}

    assert not language_channels.intersection(spec.name for spec in CHANNEL_SPECS)
    assert not language_channels.intersection(SEED_MESSAGES)
    assert seeded_keys == set(SEED_MESSAGES)
    assert all(len(message) <= 2000 for message in SEED_MESSAGES.values())
    assert SEED_MESSAGES["general"] == "Please keep conversations friendly."
    assert not any(spec.name == "compatibility" for spec in CHANNEL_SPECS)
    assert "compatibility" not in SEED_MESSAGES
    assert "Never disable security software globally" not in SEED_MESSAGES["getting-started"]
    assert "https://fmodd.com/faq" in SEED_MESSAGES["faq"]
    assert "real-world gambling" in SEED_MESSAGES["welcome"]


def test_forum_payload_contains_tags_and_list_layout():
    spec = next(item for item in CHANNEL_SPECS if item.channel_type == GUILD_FORUM)

    payload = channel_payload(spec, "parent", "guild", "bot")

    assert payload["type"] == GUILD_FORUM
    assert payload["parent_id"] == "parent"
    assert payload["default_forum_layout"] == 1
    assert payload["available_tags"]


def test_seed_marker_detection_is_exact_to_seed_key():
    messages = [{"content": "FMODD Community Setup • welcome • v1"}]

    assert seed_exists(messages, "welcome")
    assert not seed_exists(messages, "rules")


def test_seed_detection_can_reuse_own_message_when_content_intent_is_disabled():
    messages = [{"content": "", "author": {"id": "bot-id"}}]

    assert seed_exists(messages, "welcome", "bot-id")
    assert not seed_exists(messages, "welcome", "another-bot")


def test_seed_detection_ignores_discord_pin_system_messages():
    messages = [
        {"id": "pin-event", "type": 6, "content": "", "author": {"id": "bot"}},
        {
            "id": "starter",
            "type": 0,
            "content": "FMODD Community Setup • welcome • v1",
            "author": {"id": "bot"},
            "pinned": True,
        },
    ]

    assert seed_exists(messages, "welcome", "bot")


def test_seed_resource_messages_continues_when_one_channel_denies_post():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def request(self, method, path, payload=None, reason=None):
            self.calls.append((method, path))
            if method == "GET":
                return []
            if method == "POST" and path == "/channels/rules/messages":
                raise DiscordApiError("Missing Permissions")
            if method == "POST":
                return {"id": "message"}
            return None

    channels = [
        {"id": spec.name, "name": spec.name, "type": GUILD_TEXT}
        for spec in CHANNEL_SPECS
        if spec.seed_key
    ]
    client = FakeClient()

    seed_resource_messages(client, channels, "bot")

    assert ("POST", "/channels/rules/messages") in client.calls
    assert ("POST", "/channels/faq/messages") in client.calls


def test_existing_unpinned_seed_is_pinned_without_reposting():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def request(self, method, path, payload=None, reason=None):
            self.calls.append((method, path))
            if method == "GET":
                channel_id = path.split("/")[2]
                return [
                    {
                        "id": f"message-{channel_id}",
                        "content": "",
                        "author": {"id": "bot"},
                        "pinned": False,
                    }
                ]
            if method == "PATCH":
                return {
                    "id": path.split("/")[-1],
                    "content": payload["content"],
                    "author": {"id": "bot"},
                    "pinned": False,
                }
            return None

    channels = [
        {"id": spec.name, "name": spec.name, "type": GUILD_TEXT}
        for spec in CHANNEL_SPECS
        if spec.seed_key
    ]
    client = FakeClient()

    seed_resource_messages(client, channels, "bot")

    assert not any(method == "POST" for method, _ in client.calls)
    assert any(method == "PATCH" for method, _ in client.calls)
    assert ("PUT", "/channels/welcome/pins/message-welcome") in client.calls


def test_simplify_migrates_feedback_category():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def request(self, method, path, payload=None, reason=None):
            self.calls.append((method, path, payload))
            channel_id = path.split("/")[-1]
            return {"id": channel_id, "name": payload["name"], "type": GUILD_CATEGORY}

    channels = [
        {"id": "legacy", "name": "SUPPORT", "type": GUILD_CATEGORY},
        {"id": "community", "name": "COMMUNITY", "type": GUILD_CATEGORY},
    ]
    client = FakeClient()

    migrate_simplified_layout(client, channels)

    assert any(item["name"] == "FEEDBACK" for item in channels)


def test_simplify_prune_skips_a_text_channel_with_member_content():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def request(self, method, path, payload=None, reason=None):
            self.calls.append((method, path))
            if method == "GET":
                return [{"id": "m1", "type": 0, "author": {"id": "member"}}]
            return None

    channels = [{"id": "intro", "name": "introductions", "type": GUILD_TEXT}]
    client = FakeClient()

    prune_simplified_layout(client, "guild", channels, "bot")

    assert channels
    assert not any(method == "DELETE" for method, _ in client.calls)


def test_seed_permission_merge_updates_only_bot_overwrite():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def request(self, method, path, payload=None, reason=None):
            self.calls.append((method, path, payload))
            return None

    unrelated_allow = 1 << 40
    channels = []
    for spec in CHANNEL_SPECS:
        if not spec.seed_key:
            continue
        channels.append(
            {
                "id": spec.name,
                "name": spec.name,
                "type": GUILD_TEXT,
                "permission_overwrites": [
                    {"id": "guild", "type": 0, "allow": "0", "deny": "0"},
                    {
                        "id": "bot",
                        "type": 1,
                        "allow": str(unrelated_allow),
                        "deny": str(PIN_MESSAGES),
                    },
                ],
            }
        )
    client = FakeClient()

    ensure_seed_channel_permissions(client, channels, "bot")

    assert len(client.calls) == len(channels)
    _, path, payload = client.calls[0]
    assert path.endswith("/permissions/bot")
    assert int(payload["allow"]) & unrelated_allow
    assert int(payload["allow"]) & PIN_MESSAGES
    assert not int(payload["deny"]) & PIN_MESSAGES


def test_required_permission_report_accepts_all_required_bits():
    all_permissions = (1 << 53) - 1

    assert missing_permission_names(all_permissions) == []


def test_manage_server_is_not_required_for_additive_channel_setup():
    permissions_without_manage_server = (1 << 53) - 1 - (1 << 5)

    assert missing_permission_names(permissions_without_manage_server) == []


def test_pin_messages_is_required_for_complete_setup():
    permissions_without_pin_messages = (1 << 53) - 1 - PIN_MESSAGES

    assert missing_permission_names(permissions_without_pin_messages) == ["Pin Messages"]
