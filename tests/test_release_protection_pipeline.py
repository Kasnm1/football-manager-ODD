from __future__ import annotations

import tempfile
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 runtime used by the desktop dev server.
    import tomli as tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import build_protected_release
from scripts.verify_release_surface import validate
from tools.build_embedded_web_assets import build_archive


ROOT = Path(__file__).resolve().parents[1]


class ReleaseProtectionPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = tomllib.loads(
            (ROOT / "build" / "protection.toml").read_text(encoding="utf-8")
        )

    def test_protection_list_is_sorted_and_keeps_persistence_boundaries_dynamic(self) -> None:
        protected = list(self.config["protected_modules"])
        self.assertEqual(protected, sorted(set(protected)))
        self.assertGreaterEqual(len(protected), 19)
        for module_name in (
            "tools.betting_account",
            "tools.club_economy",
            "tools.club_reader",
            "tools.player_movement",
            "tools.preview_cup_odds",
        ):
            self.assertNotIn(module_name, protected)
        for module_name in (
            "tools.database_index",
            "tools.game_session",
            "tools.hook_native_core",
            "tools.native_library_loader",
            "tools.refresh_memory_core",
            "tools.rust_native_core",
        ):
            self.assertIn(module_name, protected)

    def test_protected_spec_consumes_single_configuration_source(self) -> None:
        source = (ROOT / self.config["spec"]).read_text(encoding="utf-8")
        self.assertIn('protection["protected_modules"]', source)
        self.assertIn('protection["release_web_output"]', source)
        self.assertNotIn('(str(root / "web"), "web")', source)
        self.assertIn('protection["rust_native_dll"]', source)
        self.assertIn('protection["cpp_hook_dll"]', source)
        self.assertIn('protection["integrity_files"]', source)
        self.assertNotIn('protection["host_output"]), "desktop_host"', source)

    def test_base_protected_spec_runs_integrity_hook_before_desktop_entry(self) -> None:
        source = (ROOT / "build" / "FMODD-V2.4.0-protected.spec").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'runtime_hooks=[str(root / "scripts" / "protected_runtime_hook.py")]',
            source,
        )
        hook = (ROOT / "scripts" / "protected_runtime_hook.py").read_text(
            encoding="utf-8"
        )
        self.assertLess(
            hook.index("verify_packaged_files"),
            hook.index("_fmodd_release_integrity_verified = True"),
        )

    def test_release_surface_forbids_debug_symbols(self) -> None:
        self.assertIn(".pdb", self.config["release_forbidden_suffixes"])

    def test_release_surface_rejects_debug_symbols_in_desktop_host(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            web_root = root / "web"
            web_root.mkdir()
            for name, content in {
                "index.html": "release", "app.css": "body{}", "app.js": "(()=>{})();",
            }.items():
                (web_root / name).write_text(content, encoding="utf-8")
            host_root = root / "host"
            host_root.mkdir()
            (host_root / "FMODD.WebViewHost.pdb").write_bytes(b"debug")
            errors = validate(web_root=web_root, host_root=host_root)
            self.assertTrue(any("desktop host suffix" in error for error in errors))

    def test_release_surface_rejects_internal_paths_in_native_binaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            web_root = root / "web"
            web_root.mkdir()
            for name, content in {
                "index.html": "release", "app.css": "body{}", "app.js": "(()=>{})();",
            }.items():
                (web_root / name).write_text(content, encoding="utf-8")
            native_root = root / "native"
            native_root.mkdir()
            (native_root / "core.dll").write_bytes(b"U:\\Work\\FM\\native\\core.cpp")
            errors = validate(web_root=web_root, native_roots=(native_root,))
            self.assertTrue(any("native output binary marker" in error for error in errors))

    def test_release_surface_rejects_internal_paths_in_protected_binaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            web_root = root / "web"
            web_root.mkdir()
            for name, content in {
                "index.html": "release", "app.css": "body{}", "app.js": "(()=>{})();",
            }.items():
                (web_root / name).write_text(content, encoding="utf-8")
            protected_root = root / "protected"
            protected_root.mkdir()
            (protected_root / "leaked.cp313-win_amd64.pyd").write_bytes(
                b"U:\\Work\\FM\\tools\\leaked.py"
            )

            errors = validate(web_root=web_root, protected_root=protected_root)

            self.assertTrue(any(
                "protected runtime binary marker" in error for error in errors
            ))

    def test_release_pipeline_builds_rust_core_before_tests(self) -> None:
        source = (ROOT / "scripts" / "build_protected_release.py").read_text(encoding="utf-8")
        rust_build = source.index('"scripts/build_rust_native.py"')
        pytest = source.index('sys.executable, "-m", "pytest", "tests", "-q",')
        self.assertLess(rust_build, pytest)
        cpp_build = source.index('"scripts/build_cpp_hook_core.py"')
        self.assertLess(cpp_build, pytest)

    def test_cython_release_disables_source_comments_and_tracing(self) -> None:
        source = (ROOT / "scripts" / "build_protected_release.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"emit_code_comments": False', source)
        self.assertIn('"profile": False', source)
        self.assertIn('"linetrace": False', source)

    def test_release_integrity_embeds_both_native_dll_hashes(self) -> None:
        source = (ROOT / "scripts" / "build_protected_release.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("config['rust_native_dll']", source)
        self.assertIn("config['cpp_hook_dll']", source)
        self.assertIn("def verify_native_library", source)

    def test_release_integrity_embeds_protected_module_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {
                "host/FMODD.WebViewHost.exe": b"host",
                "rust/fmodd_native_core.dll": b"rust",
                "cpp/fmodd_hook_core.dll": b"cpp",
            }
            for relative, content in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            protected_root = root / "protected"
            protected_module = protected_root / "tools" / "secret.cp313-win_amd64.pyd"
            protected_module.parent.mkdir(parents=True)
            protected_module.write_bytes(b"compiled-secret")
            config = {
                "host_output": "host",
                "integrity_files": ["desktop_host/FMODD.WebViewHost.exe"],
                "integrity_file_sources": {},
                "integrity_trees": [],
                "integrity_strict_roots": [],
                "protected_modules": ["tools.secret"],
                "ai_usage_notice": "notice.txt",
                "rust_native_output": "rust",
                "rust_native_dll": "fmodd_native_core.dll",
                "cpp_native_output": "cpp",
                "cpp_hook_dll": "fmodd_hook_core.dll",
            }

            with patch.object(build_protected_release, "ROOT", root):
                expected = build_protected_release.collect_integrity_files(
                    config, protected_root,
                )

            self.assertIn("tools/secret.cp313-win_amd64.pyd", expected)

    def test_release_integrity_covers_trees_and_rejects_unregistered_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {
                "host/FMODD.WebViewHost.exe": b"host",
                "web/index.html": b"web",
                "assets/nation_mappings/nations.json": b"mapping",
                "AI_USAGE_NOTICE.txt": b"notice",
                "build/FMODD-V1.1.ico": b"icon",
                "rust/fmodd_native_core.dll": b"rust",
                "cpp/fmodd_hook_core.dll": b"cpp",
            }
            for relative, content in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            config = {
                "host_output": "host",
                "integrity_files": ["desktop_host/FMODD.WebViewHost.exe"],
                "integrity_file_sources": {
                    "AI_USAGE_NOTICE.txt": "AI_USAGE_NOTICE.txt",
                    "FMODD-V1.1.ico": "build/FMODD-V1.1.ico",
                },
                "integrity_trees": [
                    {"packaged": "web", "source": "web"},
                    {
                        "packaged": "assets/nation_mappings",
                        "source": "assets/nation_mappings",
                    },
                ],
                "integrity_strict_roots": [
                    "assets/nation_mappings", "desktop_host", "web",
                ],
                "ai_usage_notice": "AI_USAGE_NOTICE.txt",
                "rust_native_output": "rust",
                "rust_native_dll": "fmodd_native_core.dll",
                "cpp_native_output": "cpp",
                "cpp_hook_dll": "fmodd_hook_core.dll",
            }
            package = root / "package"
            packaged_files = {
                "desktop_host/FMODD.WebViewHost.exe": b"host",
                "web/index.html": b"web",
                "assets/nation_mappings/nations.json": b"mapping",
                "AI_USAGE_NOTICE.txt": b"notice",
                "FMODD-V1.1.ico": b"icon",
                "tools/fmodd_native_core.dll": b"rust",
                "tools/fmodd_hook_core.dll": b"cpp",
            }
            for relative, content in packaged_files.items():
                path = package / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            with patch.object(build_protected_release, "ROOT", root):
                source_path = build_protected_release.build_integrity_source(
                    config, root / "generated",
                )
            namespace: dict[str, object] = {}
            exec(source_path.read_text(encoding="utf-8"), namespace)
            verify = namespace["verify_packaged_files"]
            self.assertEqual(verify(str(package)), [])
            (package / "web" / "extra.js").write_bytes(b"unexpected")
            errors = verify(str(package))
            self.assertTrue(any("web/extra.js" in error for error in errors))

    def test_protected_parity_receives_built_native_dll_paths(self) -> None:
        source = (
            ROOT / "scripts" / "verify_protected_core_parity.py"
        ).read_text(encoding="utf-8")
        self.assertIn('os.environ["FMODD_RUST_NATIVE_DLL"]', source)
        self.assertIn('os.environ["FMODD_CPP_HOOK_DLL"]', source)

    def test_refresh_memory_algorithms_live_only_in_protected_core(self) -> None:
        source = (ROOT / "tools" / "initial_data_audit.py").read_text(encoding="utf-8")
        self.assertIn("from tools.refresh_memory_core import (", source)
        self.assertNotIn("FIXTURE_DEL_ARRAY", source)
        self.assertNotIn("def scan_fixture_and_result_addresses", source)

    def test_release_pipeline_builds_and_audits_production_assets(self) -> None:
        source = (ROOT / "scripts" / "build_protected_release.py").read_text(encoding="utf-8")
        self.assertIn('sys.executable, "-m", "pytest", "tests", "-q",', source)
        self.assertIn('"--basetemp", str(test_temp)', source)
        self.assertIn('npm, "ci"', source)
        self.assertIn('"build:web:release"', source)
        self.assertEqual(source.count('"scripts/verify_release_surface.py"'), 2)
        self.assertIn('"--executable", str(executable)', source)

    def test_release_surface_rejects_source_maps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            web_root = Path(temporary)
            (web_root / "index.html").write_text("<title>FMODD V2</title>", encoding="utf-8")
            (web_root / "app.css").write_text("body{}", encoding="utf-8")
            (web_root / "app.js").write_text("(()=>{})();", encoding="utf-8")
            self.assertEqual(validate(web_root=web_root), [])
            (web_root / "app.js.map").write_text("{}", encoding="utf-8")
            errors = validate(web_root=web_root)
            self.assertTrue(any("forbidden release web suffix" in error for error in errors))

    def test_embedded_asset_builder_accepts_a_production_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            web_root = Path(temporary)
            (web_root / "index.html").write_text("release", encoding="utf-8")
            archive = build_archive(web_root)
            self.assertIn(b"index.html", archive)


if __name__ == "__main__":
    unittest.main()
