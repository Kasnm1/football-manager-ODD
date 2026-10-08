# FMODD 开发手册

本文记录 FMODD 的依赖准备、源码运行、日常修改和验证。文档职责与阅读顺序见 `docs/README.md`，项目结构与模块职责见 `docs/ARCHITECTURE.md`，面向使用者的概览见 `README.md`，自动化代理必须遵守的仓库约定见 `AGENTS.md`。本文是操作手册，不扩大用户对修改、Git 或发布操作的授权范围。

本仓库提供开发版源码及运行说明。现有 Windows 成品可从 [官网](https://fmodd.com/download) 获取。

## 1. 当前基线

- 当前版本：V2.7.0beta。
- 开发服务端口：`7857`。
- 正式桌面服务端口：`7856`。
- 桌面宿主目标框架：`.NET Framework 4.8`（`net48`）。
- GitHub 公共仓库：`https://github.com/Kasnm1/football-manager-ODD`。

当前版本由 `fm_odds_web.py` 的版本常量、README 与桌面源码维护。

## 2. 开发边界

- 修改前先阅读 `AGENTS.md`、`README.md` 和 `docs/ARCHITECTURE.md`，再定位相关模块。
- 只修改当前需求涉及的文件，保留工作区内已有的无关改动。
- `data/` 保存本机运行状态、存档数据、扫描样本和研究资料。默认不扫描、不修改、不清理、不提交；CE Trainer、FMRTE、内存字段、AOB、Hook 和版本差异研究除外，此类任务必须先读 `docs/RESEARCH_SOURCES.md`。
- 不手工修改 `tools/embedded_web_assets.py`；它由 `tools/build_embedded_web_assets.py` 生成且不进入 Git。
- 程序使用的网页图片放在 `web/assets/`；原始美术素材放在 `assets/source/`。
- `dist/`、版本宿主构建目录和其他生成物不进入 Git。
- 未经明确要求，不启动正式 EXE，不读取 FM 刷新结果，也不进行界面截图验证。

## 3. 修改位置

| 需求 | 主要文件 |
| --- | --- |
| 本地服务、刷新、API、状态编排 | `fm_odds_web.py` |
| 投注账户、注单、结算 | `tools/betting_account.py` |
| 异常投注取证与处罚 | `tools/match_integrity.py` |
| 赔率、赛程、盘口范围与实时市场 | `tools/preview_cup_odds.py`、`tools/live_market.py` |
| 联赛积分榜与原生积分写入 | `tools/league_standings.py`、`tools/league_table_memory.py` |
| 冠军盘 | `tools/championship_odds.py` |
| 商店、物品、银行和贷款 | `tools/club_economy.py`、`web/app.js` |
| 球员、职员、合同和属性 | `tools/club_reader.py`、`tools/player_details_fm24.py`、`tools/player_details_fm26.py` |
| 俱乐部人物库与名人堂 | `tools/club_legacy.py` |
| FM24/FM26 与分发版本布局 | `tools/game_layout.py` |
| 常驻进程会话与数据库目录 | `tools/game_session.py`、`tools/database_index.py` |
| 刷新内存核心（受保护） | `tools/refresh_memory_core.py` |
| 转会预算 | `tools/transfer_budget.py` |
| 已收购俱乐部球员转会与租借 | `tools/player_movement.py` |
| 训练场与青训 | `tools/training_ground.py`、`tools/youth_intake.py`、`tools/youth_generation_hook.py` |
| 董事会 Hook 与俱乐部愿景 | `tools/board_listens_hook.py`、`tools/club_vision.py` |
| 比赛道具 Hook | `tools/*hook*.py`、`tools/*nuclear*.py` |
| 存档身份、账户与存储 | `tools/save_context.py`、`tools/app_paths.py`、`tools/app_settings.py`、`tools/storage_*.py` |
| 页面结构 | `web/index.html` |
| 页面行为 | `web/app.js` |
| 页面样式 | `web/app.css` |
| 桌面入口和窗口 | `fmodd_desktop.py`、`desktop/` |

## 4. 日常开发流程

### 4.0 运行开发版

在 Windows 上准备 Python 3.10+；Python 3.10 另需安装 `tomli`。涉及原生核心时，需 Rust MSVC 工具链及 Visual C++ Build Tools。

```powershell
python scripts\build_rust_native.py
python scripts\build_cpp_hook_core.py
python fm_odds_web.py --port 7857 --no-browser --keep-alive
```

在浏览器中打开 `http://127.0.0.1:7857`。开发服务直接读取 `web/`；连接游戏后，功能可用性仍由实际 FM build 与平台校验决定。


### 4.1 修改前检查

```powershell
git status --short
git branch --show-current
```

如工作区已有改动，先确认哪些文件属于当前任务。不要使用 `git reset --hard` 或覆盖用户改动。

### 4.2 修改后重启开发服务

已授权的普通功能修改完成后，按需重建网页资源并重启指定 7857 开发服务；这是 AGENTS.md 的开发收尾例外，无需再次询问是否重启，但不免除工具权限审批。纯文档/技能修改、诊断、静态验证或明确仅模拟/离线验收时不重启。只结束命令行同时匹配脚本与端口参数的 Python 进程，不按端口号或进程名单独杀进程：

```powershell
Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^python(w)?\.exe$' -and
    $_.CommandLine -match 'fm_odds_web\.py' -and
    $_.CommandLine -match '(?:--port\s+|--port=)7857(?:\s|$)'
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Process python -ArgumentList @('fm_odds_web.py', '--port', '7857', '--no-browser', '--keep-alive') -WorkingDirectory (Get-Location).Path -WindowStyle Hidden
```

重启前应确认目标进程的命令行确实包含 `fm_odds_web.py --port 7857`，不能仅凭它是 Python 进程就结束它。启动常驻进程后即结束启动步骤，不要轮询端口、等待监听状态或反复提示“仍在初始化”。

### 4.3 修改网页后刷新界面

开发服务直接读取 `web/`，修改后刷新浏览器；服务端代码变化时按需重启开发服务。

### 4.4 基础检查

只选择本次改动需要的检查，不照抄整个命令块。纯文档/技能调整检查差异、引用及适用的技能格式即可；以下语法检查仅用于对应文件确有修改且能增加有效覆盖时：

```powershell
python -m py_compile fm_odds_web.py fmodd_desktop.py
node --check web\app.js
git diff --check
```

修改 `tools/*.py` 时可检查实际变更模块。优先运行相关行为回归；涉及共享契约时覆盖受影响调用方，不自动运行全套。已通过的检查仅在新增改动、失败或明确未解决风险时重复/扩大。只使用已确认隔离的测试数据；真实 FM、账户数据或外部服务操作另需相应授权。

检查通过不等于已经实机验证 FM 内存功能。涉及新偏移、Hook、伤病、属性或比赛状态时，必须区分 FM24、FM26 以及 Steam、Epic、XGP，不允许在版本未通过校验时复用其他版本地址。

## 5. FM24 与 FM26 修改原则

- 已识别的 FM24/FM26 build 与 Steam、Epic、XGP 发行版默认功能语义、对象字段和业务能力通用；一个版本已验证的功能默认外推到其他已识别版本，不因缺少逐版本实机样本而关闭功能。
- 内存读取、写入和 Hook 必须通过当前 `GameLayout` 分发，不得只凭进程名使用固定偏移。Hook、模块基址、RVA、AOB 和 vtable 仍按版本与平台身份选择。
- FM26 Steam 保留 EXE SHA256 严格识别；FM26 XGP 还需验证 `game_plugin.dll` 身份和运行时结构。
- FM24 Steam 与 Epic 使用各自的 EXE SHA256；平台相关 Hook RVA 分开维护。
- 所有写入保留运行时安全条件：目标进程与模块身份、对象 vtable/UID/反向引用、数值范围、期望旧值、写后回读、失败回滚和 Hook 卸载恢复。
- 新增内存能力时，应先安全读取和校验，再写入；出现零命中、多命中或结构歧义时拒绝执行。
- 进程重启或切换存档后，不复用旧进程地址、模板指针或俱乐部上下文。
- 后续开发默认移除仅由“尚未逐版本实机验证”造成的版本门禁；只有明确反证（布局冲突、AOB 非唯一、原生调用崩溃、字段语义不同或事务无法回滚）才允许保留版本例外，并记录具体证据。

## 6. Git 工作流

`git status`、`git diff` 等只读检查属于常规开发步骤。创建/切换分支、暂存、提交、推送、打标签或创建 Release 只在用户明确要求时执行；下面的命令是操作参考，不是默认授权。

通常在 `main` 上保留稳定快照；较大或风险较高的功能使用 `codex/` 前缀分支：

```powershell
git switch -c codex/功能名称
```

提交前检查：

```powershell
git status --short
git diff --check
git diff --stat
```

只暂存本次改动，避免使用会把所有未知文件一并加入的命令：

```powershell
git add DEVELOPMENT.md 路径\到\本次文件
git diff --cached --check
git commit -m "feat: 简短说明"
git push -u origin 当前分支名
```

不要提交：

- `data/`
- `dist/`
- 本机扫描结果、日志、崩溃转储和临时文件
- 用户存档、账户数据或其他隐私数据
- 仅供研究的未确认内存样本
- `tools/embedded_web_assets.py` 等生成文件

## 7. 常见问题

### 修改网页后仍显示旧内容

刷新浏览器并确认访问的是当前开发服务地址；服务端代码变化时按 4.2 节重启。

### Git 中出现大量运行数据或生成文件

先停止暂存，检查 `.gitignore` 和 `git status --short`。不要删除 `data/` 来解决 Git 状态问题。

### 开发服务无法启动

先检查 `7857` 的占用者；仅当其 Python 命令行同时匹配 `fm_odds_web.py` 和端口参数 `7857` 时，按 4.2 节重启。其他占用者只报告，不自动结束。不要结束所有 Python 或 FM 进程。

## 8. 每次任务的最小交付清单

仅核对本次任务适用项；纯文档/技能修改不要求应用测试、网页重建或服务重启，用户更窄的明确限制优先。

- 需求相关代码已修改，未覆盖无关改动。
- 修改网页时已检查实际页面资源与相关语法。
- 已执行与改动范围相符的语法或编译检查。
- 普通功能修改后已重启 `7857` 开发服务；启动常驻进程后即结束启动步骤。
- 未主动启动正式 EXE、读取 FM 刷新结果或进行界面验证。
- 已说明修改内容、检查结果以及仍需实机验证的风险。
- 只有用户明确要求时才提交、推送或发布。

## 通用只读验证工具与内部编码报告

`tools/fm24_process_diagnostic.py` 同时支持 FM24 与 FM26。工具只读取进程、模块、PE 身份、RTTI、适配签名、当前连接条件和本地 FMODD 状态，不写入游戏内存或存档。

检测完成后，界面正文与“复制检测报告”都只提供 `ODD-C1` 支持包，不向测试者显示内部可读报告。支持包包含可读诊断报告、当前已识别 `GameLayout` 的完整字段快照，以及关键运行时 RVA 的模块边界、可读性和匿名指针目标状态；它不保留运行时绝对指针。外部只看到压缩编码块，不直接显示安装路径、RVA、功能名或异常正文。该格式是便于传递和避免误读的可逆编码，不是加密、匿名化或访问控制。

开发侧解码：

```python
from tools.fm24_process_diagnostic import decode_code_report

payload = decode_code_report(received_text)
print(payload["report"])
print(payload["layouts"])
# 每个已识别布局的结构化结果位于 layouts[*]["address_probes"]。
```

布局字段使用稳定的 `Fxxxxxxxx` 代码，并在解码后的 `codebook` 中映射回字段名。关键地址健康矩阵按 `core` 与 `club` 分组：前者覆盖游戏日期、数据库/经理/存档根、赛程池、比赛会话指针，以及赛程、赛果、球队、赛事、球员、经理和职员的核心 vtable；后者覆盖 Club vtable、俱乐部文化 vtable/构造/初始化入口、合作俱乐部管理器/创建入口、球场 vtable、球员移动 Club 方法和俱乐部政策入口。状态只表示未配置、超出模块、不可读、可读、空指针或匿名目标是否可读。俱乐部对象内部的资料、设施、财政、阵容和职员偏移仍由 `D/F/E/R/S` 结构探针验证，不能用模块 RVA 可读性替代。清单自动来自 `GameLayout` 数据类；新增版本字段后必须运行 `tests/test_diagnostic_code_report.py`，确认所有字段仍被收集且代码无碰撞。内部编码报告只能证明采集到的只读身份、布局配置和探针结果，不能代替写入、Hook、过日、保存与重载验证。

当前内部检测代号包括 `C-C1`（两代通用构造入口形状）、`C-C2`（FM24 初始化入口形状）和 `T-C1`（候选球队类型字节的对象数、Club 分组数、关联梯队组数与值分布）。俱乐部正式只读链使用 `D-C1`（成立年份、入场、球迷、所有权和文化覆盖）、`F-C1`（训练、青训、青训教练和青训招募四项设施各自的有效数与范围）、`F-C2`（体育场容量/草皮与训练场）、`E-C1`（财务核心、收支表、月度摘要、债务与赞助）、`R-C1`（阵容及 CA/PA、位置、体能、锐度、士气字段）和 `S-C1`（职员 UID、主合同、回指球队、职务、工资与日期结构）。这些探针最多抽取 16 个通过 Club/Team UID 与 vtable 校验的俱乐部，只输出计数和设施等级范围，不保存名称、UID、地址或财务原值。

Steam/Epic 的精确布局直接按 EXE 身份选择；XGP 必须先按当前进程动态恢复并校验 RTTI、vtable 和日期，再运行上述读取探针及生成布局清单。动态恢复失败时返回 `M0`、`U0` 或异常类型代号，不回退到其他发行版地址。特征码零命中、多命中、结构样本不足或字段计数为零都只能作为待适配证据，不能自动开启对应写入功能。

球员比赛证据使用 `P-C1/P-C2`。`P-C1` 只汇总最近最多 64 场已校验赛果的比赛数、比分进球数、成功读取事件的比赛数、球员进球/助攻事件数及匿名关联成功数，不保存球员姓名、UID、比赛双方、赛事或地址。`P-C2` 只汇总已由匹配版本原生读取器确认的评分数量及最小/最大值；当前读取链没有可靠评分字段时返回 `R0 N- X-`，不得用 CA、位置评分或球队评分补值。
