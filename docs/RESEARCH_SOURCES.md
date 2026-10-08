# Public evidence boundary

This repository contains the FMODD application source, version-specific layouts, and tests. Internal investigation logs, proprietary editor/game binaries, recovered assemblies, raw memory captures, session addresses, and personal save data are not published.

For code ownership see [ARCHITECTURE.md](ARCHITECTURE.md); for feature entrypoints see [FEATURE_INDEX.md](FEATURE_INDEX.md). Existing source comments preserve their technical context; they are not an authorization to redistribute a third party's materials.

When contributing a new layout or native operation, record the exact game generation, build, platform, module identity, observed semantics, verification steps, counterexamples, and remaining limits without including proprietary binaries or personal data. Separate an upstream capability, an observed implementation, an FMODD integration, and a live-game verification.

Use independently collected or authorized evidence. Do not silently carry one version's offsets or session addresses into another. Static and mocked tests do not prove live-game behavior or persistence after save/reload.
