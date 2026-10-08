# Contributing to Football Manager ODD

欢迎通过 Issue 报告问题、提出建议，通过 Pull Request 提交改进。维护者按精力处理，不承诺立即回复、合并或持续更新。

## Issues

请提供 FMODD 版本、Football Manager 精确版本及 Steam/Epic/XGP 平台、复现步骤、预期与实际结果。日志和截图先移除个人信息。不要上传存档、账户文件、凭据、商业软件二进制或内存转储。

## Pull requests

1. Fork 本仓库，在自己的分支中完成小范围修改。
2. 说明解决的问题、改动行为和实际完成的测试；区分静态/模拟检查与实机验证。
3. 保留版本门禁、事务校验和回滚，并同步需要的本地化、文档与相关测试。
4. 提交 PR，等待维护者审查与后续版本安排。

只提交你有权贡献的代码和材料。第三方组件保留原许可证及声明。不要改变官方发布、维护者身份或捐赠入口来冒充官方版本。

## Windows development

先阅读 README 和 DEVELOPMENT。原生编译需要 Windows SDK/MSVC、Rust 工具链；桌面宿主需要 .NET Framework 4.8 开发环境。源码仓库不会包含这些编译器、游戏文件或 SDK 二进制。

恢复桌面 SDK：

```powershell
python scripts/restore_webview2.py
```

选择与你的修改直接相关且隔离的测试。默认不要操作真实 FM 进程或个人数据。
