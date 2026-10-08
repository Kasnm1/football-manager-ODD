from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tools.i18n_audit import apply_baseline, scan_html


ROOT = Path(__file__).resolve().parents[1]


def _texts(findings):
    return {(item.file, item.kind, item.text) for item in findings}


def test_html_finds_visible_text_and_attribute_but_ignores_comments_and_styles() -> None:
    source = """<!-- 中文注释 -->
<h1 title=\"悬浮说明\">页面标题</h1>
<script>const hidden = \"脚本源文案\";</script>
<style>.x::after { content: \"样式\" }</style>
"""
    findings = scan_html(source, "web/index.html")
    assert _texts(findings) == {
        ("web/index.html", "html-attribute", "悬浮说明"),
        ("web/index.html", "html-text", "页面标题"),
        ("web/index.html", "js-string", "脚本源文案"),
    }
    assert all(item.category in {"user-visible", "uncertain"} for item in findings)


def test_python_and_javascript_extract_literals_and_classify_directives() -> None:
    from tools.i18n_audit import _scan_javascript, scan_python

    js = 'toast("保存成功"); // 注释不应计入\nconst n = `球员 ${name}`;'
    js_findings = _scan_javascript(js, "web/app.js")
    assert {item.text for item in js_findings} == {"保存成功", "球员 ${name}"}
    py = 'raise ValueError("金额无效")\nmessage = f"球员 {name}"\nname = "张三"  # i18n-audit: game-data\n'
    py_findings = scan_python(py, "tools/example.py")
    assert any(item.category == "user-visible" for item in py_findings)
    assert any(item.kind == "python-fstring" and "球员" in item.text for item in py_findings)
    assert any(item.allowlisted and item.category == "game-or-user-data" for item in py_findings)


def test_app_js_long_lines_do_not_hide_frontend_literals() -> None:
    from tools.i18n_audit import _scan_javascript

    source = 'const value = "界面提示";' + ('x' * 1000) + 'node.textContent = value;'
    findings = _scan_javascript(source, "web/app.js")

    assert len(findings) == 1
    assert findings[0].likely_user_visible is True
    assert findings[0].reason == "FMODD frontend source literal"


def test_project_scan_never_includes_data_and_is_sorted(tmp_path: Path) -> None:
    (tmp_path / "web").mkdir()
    (tmp_path / "tools").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "web" / "app.js").write_text('const a = "甲";', encoding="utf-8")
    (tmp_path / "web" / "index.html").write_text('<p>乙</p>', encoding="utf-8")
    (tmp_path / "tools" / "b.py").write_text('x = "丙"', encoding="utf-8")
    (tmp_path / "data" / "ignored.py").write_text('x = "丁"', encoding="utf-8")
    from tools.i18n_audit import scan_project

    report = scan_project(tmp_path)
    assert report["scanned_files"] == ["tools/b.py", "web/app.js", "web/index.html"]
    assert all("data" not in item["file"] for item in report["findings"])


def test_baseline_check_is_location_independent_and_detects_new_occurrence(tmp_path: Path) -> None:
    from tools.i18n_audit import scan_project

    (tmp_path / "web").mkdir()
    source = tmp_path / "web" / "app.js"
    source.write_text('toast("原有");', encoding="utf-8")
    baseline = scan_project(tmp_path)
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline, ensure_ascii=False), encoding="utf-8")
    source.write_text('// moved\ntoast("原有");\ntoast("新增");', encoding="utf-8")
    checked = apply_baseline(scan_project(tmp_path), baseline_path)
    assert checked["check"]["passed"] is False
    assert [item["text"] for item in checked["check"]["new_findings"]] == ["新增"]


def test_cli_outputs_json_to_stdout_and_summary_to_stderr(tmp_path: Path) -> None:
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "index.html").write_text("<p>文字</p>", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "i18n_audit.py"), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(result.stdout)
    assert report["summary"]["occurrences"] == 1
    assert "i18n audit:" in result.stderr


def test_cli_emits_utf8_for_symbols_outside_windows_gbk(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "web").mkdir(parents=True)
    (project / "web" / "index.html").write_text("<button>充值 £100</button>", encoding="utf-8")
    (project / "web" / "app.js").write_text("", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "i18n_audit.py"), "--root", str(project)],
        capture_output=True,
        check=True,
    )
    decoded = result.stdout.decode("utf-8")
    assert "充值 £100" in decoded
