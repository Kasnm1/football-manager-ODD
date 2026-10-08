[English](GETTING_STARTED.md) · [简体中文](GETTING_STARTED.zh-CN.md) · [한국어](GETTING_STARTED.ko-KR.md)

# 运行开发版本

[← 项目首页](../../README.zh-CN.md) · [贡献说明](../../CONTRIBUTING.md)

## 准备环境

使用 Windows 与 Python 3.10 或更新版本。Python 3.10 另需 `tomli`。原生核心需要 Rust MSVC 工具链、Visual C++ Build Tools 和 Windows SDK。

克隆或下载仓库，在仓库根目录打开终端。建议为开发创建独立的 Python 环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Python 3.10 需安装：

```powershell
python -m pip install tomli
```

## 构建原生核心并启动本地服务

```powershell
python scripts\build_rust_native.py
python scripts\build_cpp_hook_core.py
python src\fm_odds_web.py --port 7857 --no-browser --keep-alive
```

在浏览器打开 **http://127.0.0.1:7857**。开发服务直接读取 `src/web/` 中的 HTML、CSS、JavaScript 与资源文件。

启动 Football Manager，载入存档，再通过 FMODD 连接。功能可用性取决于具体游戏 build 与发行平台。首次使用原生数据修改功能前，请先备份游戏存档。

## 开始修改

阅读 [贡献者规则](../../AGENTS.md)、[文档导航](../README.md)，以及相关架构或功能索引条目。保持修改范围集中，执行能够覆盖本次改动的检查。

修改前端文件后刷新浏览器；服务端代码变化后重启本地服务，只停止自己启动的服务实例。个人存档、账户数据、凭据与本地生成物保留在提交之外。

详细开发和排错说明见 [开发手册](../development/DEVELOPMENT.md)。

## 运行相关测试

在开发环境中安装测试工具，再按改动选择相关测试。例如：

```powershell
python -m pip install pytest
python -m pytest tests/test_local_icon_assets.py
```

仓库配置会将 `src/` 加入测试的模块搜索路径。
