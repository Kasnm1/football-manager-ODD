# FMODD documentation

Start here to find the right guide. Product homepages are available in [English](../README.md), [简体中文](../README.zh-CN.md) and [한국어](../README.ko-KR.md). Detailed technical documents are mainly in Chinese.

## Getting started · 入门 · 시작하기

| Document | Purpose |
| --- | --- |
| Development quick start | [English](guides/GETTING_STARTED.md) · [简体中文](guides/GETTING_STARTED.zh-CN.md) · [한국어](guides/GETTING_STARTED.ko-KR.md) |
| [Product guide](guides/PRODUCT_GUIDE.md) | Detailed features and game compatibility |
| [Development manual](development/DEVELOPMENT.md) | Windows environment, source runtime and focused verification |
| [Contributing](../CONTRIBUTING.md) | Issues and pull requests |
| Support FMODD | [English](support/SUPPORT.md) · [简体中文](support/SUPPORT.zh-CN.md) · [한국어](support/SUPPORT.ko-KR.md) |

## Code and architecture · 代码与架构 · 코드 및 아키텍처

| Document | Purpose |
| --- | --- |
| [Contributor rules](../AGENTS.md) | Contributor and automation boundaries |
| [Architecture](ARCHITECTURE.md) | Module ownership and call chains |
| [Feature index](FEATURE_INDEX.md) | Find code by feature |
| [Runtime contracts](RUNTIME_CONTRACTS.md) | State, storage, transactions and concurrency |
| [Odds architecture](ODDS_ARCHITECTURE.md) | Odds, betting, markets and settlement |
| [UI system](UI_SYSTEM.md) | Interface conventions |
| [Localisation architecture](I18N_ARCHITECTURE.md) | Language ownership and translation |
| [Research sources](RESEARCH_SOURCES.md) | Public evidence boundary |
| [macOS porting guide](development/MACOS_PORTING_GUIDE.md) | Porting plan; does not imply macOS support |

## Project records and notices · 记录与声明 · 기록 및 고지

| Document | Purpose |
| --- | --- |
| [Changelog](../CHANGELOG.md) | Version history |
| [Source and third-party materials notice](notices/SOURCE_NOTICE.md) | Source publication and third-party boundaries |
| [Third-party notices](../THIRD_PARTY_NOTICES.md) | Dependencies, materials and their terms |
| [Third-party licences](licenses/) | Original dependency licence texts |

## Repository layout · 仓库目录 · 저장소 구조

| Path | Contents |
| --- | --- |
| [fm_odds_web.py](../fm_odds_web.py) | Local HTTP service, state coordination and API entrypoint |
| [fmodd_desktop.py](../fmodd_desktop.py) | Desktop entrypoint and service lifecycle |
| [fm_collect.py](../fm_collect.py), [fm_collector/](../fm_collector/) | Collection entrypoint and modules |
| [tools/](../tools/) | Python domain logic, FM readers, transactions and shared helpers; use the [feature index](FEATURE_INDEX.md) to locate a feature |
| [web/](../web/) | HTML, CSS, JavaScript, interface translations and runtime images |
| [desktop/](../desktop/) | C# WebView2 host source |
| [native/](../native/) | Rust core and C++ hook core source |
| [FMDataBridge/](../FMDataBridge/) | C# data bridge source and its own guide |
| [scripts/](../scripts/) | Native core build helpers and WebView2 SDK restoration |
| [tests/](../tests/) | Isolated regression tests; choose tests relevant to the changed feature |
| [assets/](../assets/) | Player-name dictionaries |
| [build/](../build/) | Tracked desktop icon |
| [docs/guides/](guides/) | Quick starts and product details |
| [docs/development/](development/) | Development manual and porting notes |
| [docs/support/](support/) | Donation guides in three languages |
| [docs/assets/](assets/) | Images for documentation and the GitHub homepage |
| [docs/notices/](notices/), [licenses/](licenses/) | Publication notices and third-party licence texts |

Internal raw research records, recovered binaries and local-session logs are outside this public source snapshot. Source, tests and matching-version runtime evidence remain distinct; filenames or feature labels do not prove compatibility.
