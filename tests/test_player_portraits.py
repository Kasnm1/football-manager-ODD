from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from tools import app_settings
from tools.player_portraits import clear_portrait_cache, portrait_path, portrait_roots


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def test_portrait_path_resolves_uid_from_fm_graphics_xml(tmp_path: Path) -> None:
    image = tmp_path / "custom" / "hero_123456.png"
    _write(image, b"png")
    _write(
        tmp_path / "custom" / "config.xml",
        '<record from="hero_123456" to="graphics/pictures/person/123456/portrait" />',
    )

    clear_portrait_cache()
    assert portrait_path(123456, roots=(tmp_path,)) == image.resolve()


def test_portrait_roots_does_not_treat_blank_setting_as_current_directory(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    documents = tmp_path / "home" / "Documents" / "Sports Interactive"
    fm_graphics = documents / "Football Manager 26" / "graphics"
    fm_graphics.mkdir(parents=True)
    with (
        patch.object(app_settings, "SETTINGS_PATH", settings_path),
        patch("tools.player_portraits.Path.home", return_value=tmp_path / "home"),
    ):
        app_settings.set_portrait_sources("", "")
        assert portrait_roots() == (fm_graphics,)


def test_portrait_path_accepts_relative_source_and_common_image_suffix(tmp_path: Path) -> None:
    image = tmp_path / "faces" / "987654.jpg"
    _write(image, b"jpg")
    _write(tmp_path / "pack" / "config.xml", '<record from="../faces/987654" to="" />')

    clear_portrait_cache()
    assert portrait_path(987654, roots=(tmp_path,)) == image.resolve()


def test_explicit_xml_resolves_each_mapping_from_its_own_nested_source_path(
    tmp_path: Path,
) -> None:
    pack = tmp_path / "standalone-pack"
    pacific = pack / "PacificIslanders" / "PP1PacificIslanders0338.png"
    european = pack / "Europe" / "Youth" / "EUPlayer0355.jpg"
    xml = pack / "config.xml"
    _write(pacific, b"pacific")
    _write(european, b"european")
    _write(
        xml,
        "".join((
            '<record from="PacificIslanders/PP1PacificIslanders0338" '
            'to="graphics/pictures/person/2002058303/portrait" />',
            '<record from="Europe/Youth/EUPlayer0355" '
            'to="graphics/pictures/person/2002058304/portrait" />',
        )),
    )

    clear_portrait_cache()
    assert portrait_path(2002058303, roots=(), xml_path=xml) == pacific.resolve()
    assert portrait_path(2002058304, roots=(), xml_path=xml) == european.resolve()


def test_portrait_path_rejects_uid_without_mapping_or_outside_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-222222.png"
    _write(outside, b"png")
    _write(tmp_path / "config.xml", '<record from="222222" to="../outside-222222" />')

    clear_portrait_cache()
    assert portrait_path(222222, roots=(tmp_path,)) is None
    assert portrait_path("not-a-uid", roots=(tmp_path,)) is None  # type: ignore[arg-type]


def test_portrait_path_recursively_finds_xml_below_graphics_root(tmp_path: Path) -> None:
    image = tmp_path / "pack" / "nested" / "faces" / "face_345678.png"
    _write(image, b"png")
    _write(
        tmp_path / "pack" / "nested" / "config.xml",
        '<record from="faces/face_345678" to="graphics/pictures/person/345678/portrait" />',
    )

    clear_portrait_cache()
    assert portrait_path(345678, roots=(tmp_path,)) == image.resolve()


def test_portrait_path_checks_later_xml_after_an_earlier_config(tmp_path: Path) -> None:
    first = tmp_path / "first" / "config.xml"
    second = tmp_path / "second" / "nested" / "config.xml"
    image = second.parent / "face_678901.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"image")
    _write(first, "<record from=\"face_111111\" to=\"graphics/pictures/person/111111/portrait\" />")
    _write(second, "<record from=\"face_678901\" to=\"graphics/pictures/person/678901/portrait\" />")
    clear_portrait_cache()
    assert portrait_path(678901, roots=(tmp_path,)) == image.resolve()


def test_portrait_path_does_not_recursively_probe_unmapped_nested_images(tmp_path: Path) -> None:
    image = tmp_path / "pack" / "league" / "faces" / "face_789012.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"image")
    patterns: list[str] = []
    original_rglob = Path.rglob

    def tracked_rglob(path: Path, pattern: str):
        patterns.append(pattern)
        return original_rglob(path, pattern)

    clear_portrait_cache()
    with patch.object(Path, "rglob", tracked_rglob):
        assert portrait_path(789012, roots=(tmp_path,)) is None
    assert patterns == ["*.xml"]


def test_portrait_path_uses_conventional_file_without_building_xml_catalog(tmp_path: Path) -> None:
    image = tmp_path / "sortitoutsi" / "faces" / "face_789012.png"
    _write(image, b"image")
    clear_portrait_cache()
    with patch(
        "tools.player_portraits._build_index",
        side_effect=AssertionError("conventional image lookup must not scan XML"),
    ):
        assert portrait_path(789012, roots=(tmp_path,)) == image.resolve()


def test_portrait_path_builds_one_shared_catalog_for_multiple_uids(tmp_path: Path) -> None:
    from tools import player_portraits

    for uid in (789012, 789013, 789014):
        _write(tmp_path / "pack" / "avatars" / f"portrait_{uid}.png", b"image")
    _write(
        tmp_path / "pack" / "config.xml",
        "".join(
            f'<record from="avatars/portrait_{uid}" to="graphics/pictures/person/{uid}/portrait" />'
            for uid in (789012, 789013, 789014)
        ),
    )
    clear_portrait_cache()
    with patch(
        "tools.player_portraits._build_index",
        wraps=player_portraits._build_index,
    ) as build:
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(
                lambda uid: portrait_path(uid, roots=(tmp_path,)),
                (789012, 789013, 789014, 789012, 789013, 789014),
            ))
    assert all(result is not None for result in results)
    assert build.call_count == 1


def test_portrait_path_can_use_one_explicit_xml_file(tmp_path: Path) -> None:
    pack = tmp_path / "standalone-pack"
    image = pack / "faces" / "face_456789.webp"
    xml = pack / "mapping.xml"
    _write(image, b"webp")
    _write(xml, '<record from="faces/face_456789" to="" />')

    clear_portrait_cache()
    assert portrait_path(456789, roots=(), xml_path=xml) == image.resolve()


def test_portrait_path_uses_last_matching_portrait_from_added_xmls(tmp_path: Path) -> None:
    first = tmp_path / "pack-a" / "mapping.xml"
    second = tmp_path / "pack-b" / "mapping.xml"
    first_image = first.parent / "face_654321.png"
    second_image = second.parent / "face_654321.png"
    _write(first_image, b"first")
    _write(second_image, b"last")
    _write(first, '<record from="face_654321" to="" />')
    _write(second, '<record from="face_654321" to="" />')
    clear_portrait_cache()
    assert portrait_path(654321, roots=(), xml_path=first) == first_image.resolve()
    # The settings list is tested below; passing both XMLs models the same
    # ordered source list and the later source wins for duplicate UIDs.
    from unittest.mock import patch
    settings_path = tmp_path / "settings.json"
    with patch.object(app_settings, "SETTINGS_PATH", settings_path):
        app_settings.set_portrait_sources("", first)
        app_settings.set_portrait_sources("", second)
        with patch("tools.app_settings.SETTINGS_PATH", settings_path):
            clear_portrait_cache()
            assert portrait_path(654321) == second_image.resolve()


def test_explicit_xml_uses_enclosing_graphics_as_safe_root(tmp_path: Path) -> None:
    graphics = tmp_path / "graphics"
    image = graphics / "pack" / "faces" / "face_567890.png"
    xml = graphics / "pack" / "config" / "mapping.xml"
    _write(image, b"png")
    _write(xml, '<record from="../faces/face_567890" to="" />')

    clear_portrait_cache()
    assert portrait_path(567890, roots=(), xml_path=xml) == image.resolve()


def test_portrait_sources_persist_as_mutually_exclusive_modes(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    graphics = tmp_path / "graphics"
    graphics.mkdir()
    xml = tmp_path / "pack" / "config.xml"
    _write(xml, "<list />")

    with patch.object(app_settings, "SETTINGS_PATH", settings_path):
        folder = app_settings.set_portrait_sources(graphics, "")
        assert folder["portrait_graphics_root"] == str(graphics.resolve())
        assert folder["portrait_xml_path"] == ""

        selected_xml = app_settings.set_portrait_sources("", xml)
        assert selected_xml["portrait_graphics_root"] == ""
        assert selected_xml["portrait_xml_path"] == str(xml.resolve())
        assert selected_xml["portrait_xml_paths"] == [str(xml.resolve())]

        third = tmp_path / "pack" / "second.xml"
        _write(third, "<list />")
        added_xml = app_settings.set_portrait_sources("", third)
        assert added_xml["portrait_xml_paths"] == [str(xml.resolve()), str(third.resolve())]
        duplicate_xml = app_settings.set_portrait_sources("", xml)
        assert duplicate_xml["portrait_xml_paths"] == [str(xml.resolve()), str(third.resolve())]

        cleared = app_settings.set_portrait_sources(clear=True)
        assert cleared["portrait_graphics_root"] == ""
        assert cleared["portrait_xml_path"] == ""
        assert cleared["portrait_xml_paths"] == []


def test_portrait_settings_ui_exposes_two_pickers_and_clear_all() -> None:
    root = (Path(__file__).resolve().parents[1] / "src")
    script = (root / "web" / "app.js").read_text(encoding="utf-8")
    host = (root / "desktop" / "WebViewHost.cs").read_text(encoding="utf-8")
    control = script.split("function ensurePortraitSourceControl()", 1)[1].split(
        "function ensureUpdateCheckControl", 1,
    )[0]

    assert "球员头像包" in control
    assert "选择 graphics 文件夹" in control
    assert "选择 config.xml" in control
    assert "清除全部选择" in control
    assert 'id="clear-portrait-sources"' in control
    assert 'id="choose-portrait-graphics"' in control
    assert 'id="choose-portrait-xml"' in control
    for marker in ("<input", "使用此文件夹", "使用此 XML", "恢复自动查找", "data-portrait-source-status"):
        assert marker not in control

    assert "/api/player-portraits/settings" in script
    assert 'clear_all: mode === "clear"' in script
    assert 'savePortraitSourceSelection("clear", "")' in script
    assert 'desktopBridge.postMessage("choose-portrait-graphics-root")' in script
    assert 'desktopBridge.postMessage("choose-portrait-xml")' in script
    for marker in (
        'message == "choose-portrait-graphics-root"',
        '"portrait-graphics-root::" + selectedGraphics',
        'message == "choose-portrait-xml"',
        '"portrait-xml-path::" + selectedXml',
    ):
        assert marker in host
