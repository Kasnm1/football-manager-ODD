# FMODD contributor and agent rules

These rules apply to this source repository.

- Read `README.md`, `docs/README.md`, `docs/ARCHITECTURE.md`, and the relevant `docs/FEATURE_INDEX.md` entry before changing unfamiliar code.
- Check Git status and the target diff first; preserve other work. Make changes only within the requested scope.
- Keep `data/`, saves, accounts, logs, local configurations, research payloads, and build outputs outside Git. Never commit credentials or proprietary game/editor binaries.
- The runtime remains Windows-only. Do not operate a live Football Manager process, install a hook, change a save/account, or launch an executable without explicit user authorization.
- Use the active `GameLayout` for version-dependent offsets. Preserve object identity checks, expected-old-value checks, readback, and rollback.
- Prefer focused, isolated tests. Do not automatically run all tests, restart services, or build a release for a documentation or source-publication change.
- Development serves `web/` directly. Do not hand-edit generated asset bundles. User-visible font sizes must be at least 13px.
- User-visible copy must support en-GB, zh-CN, zh-TW, ko-KR, de-DE, es-ES, fr-FR, ru-RU, ja-JP, pt-BR, and pt-PT.
- Update architecture, runtime contracts, or feature-index entries when their corresponding behavior or ownership changes.
- This repository provides development source and does not include EXE packaging scripts, configuration, or instructions. A passing static or mock test does not prove live-game behavior.
