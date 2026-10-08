# Contributing to Football Manager ODD

Welcome to FMODD! Share a reproducible issue, suggest a feature or submit a focused pull request. The maintainer reviews contributions as time and energy allow.

欢迎通过 Issue 报告问题、提出建议，通过 Pull Request 提交改进。维护者按时间与精力处理反馈和贡献。

## Report an issue

Include the FMODD version, exact Football Manager build, Steam/Epic/XGP distribution, reproduction steps, expected behaviour and actual result. Remove personal information from logs and screenshots. Keep saves, account files, credentials, proprietary binaries and memory dumps private.

请提供 FMODD 版本、Football Manager 精确版本及发行平台、复现步骤、预期与实际结果。日志和截图先移除个人信息；存档、账户文件、凭据、商业软件二进制和内存转储请保留在本机。

## Submit a pull request

1. Fork the repository and make a focused change on your own branch.
2. Explain the problem, the resulting behaviour and the checks you ran. Distinguish static or mock checks from live-game validation.
3. Preserve version gates, transaction checks and rollback. Update the relevant localisation, documentation and tests.
4. Open a pull request for maintainer review and consideration for a future update.

<details>
<summary><strong>中文说明</strong></summary>

1. Fork 本仓库，在自己的分支中完成小范围修改。
2. 说明解决的问题、改动行为和实际完成的测试；区分静态/模拟检查与实机验证。
3. 保留版本门禁、事务校验和回滚，并同步需要的本地化、文档与相关测试。
4. 提交 PR，等待维护者审查与后续版本安排。

</details>

Contribute code and materials you have the right to share. Preserve third-party licences and notices. Represent your fork and its maintainer accurately, including any publication and support links.

只提交你有权贡献的代码和材料，保留第三方许可证及声明。请如实标明分支版本、维护者身份以及相关发布和支持入口。

## Develop on Windows

Start with the [development quick start](docs/GETTING_STARTED.md) and [contributor rules](AGENTS.md). Native compilation uses the Windows SDK, MSVC and Rust toolchain. Changes to the desktop host also require a .NET Framework 4.8 development environment; its WebView2 SDK can be restored with `python scripts/restore_webview2.py`.

先阅读 [快速入门](docs/GETTING_STARTED.zh-CN.md) 和 [贡献者规则](AGENTS.md)。原生编译需要 Windows SDK、MSVC 和 Rust 工具链。桌面宿主修改另需 .NET Framework 4.8 开发环境，可通过 `python scripts/restore_webview2.py` 恢复 WebView2 SDK。

Choose isolated tests relevant to your change. Live FM processes and personal data require separate authorisation.

选择与你的修改直接相关且隔离的测试；真实 FM 进程和个人数据操作需另行授权。
