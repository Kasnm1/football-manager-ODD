# FMODD 更新日志

本文只记录历史变化，不是当前版本、代码行为或功能支持边界的权威来源。当前状态以 `README.md`、`docs/ARCHITECTURE.md`、实际代码和验证证据为准；旧条目保留发布当时的表述，后续纠正应写入新条目而非改写历史。

## V2.4.2（2026-08-29）

### 发布

- 将当前工作区同步为 V2.4.2，生成独立版本资源及普通/保护封装配置。
- 延续受保护核心一致性、发布表面和运行文件完整性审计。

## V2.4.1beta（2026-08-27）

### 发布与界面

- 将当前工作区内容同步为 V2.4.1beta，生成独立版本资源及普通/保护封装配置。
- 修正通用 `hidden` 属性可能被按钮布局样式覆盖的问题，确保训练档案、俱乐部名人堂及其他隐藏入口在开发版和封装版中保持隐藏。
- 延续受保护核心一致性、发布表面和运行文件完整性审计。

## V2.4.0beta（2026-08-26）

### 发布与保护

- 将当前 V2.4.0 内容标记为 Beta，重新生成 Beta 版本资源、桌面宿主和普通/保护封装配置。
- 延续受保护核心一致性、发布表面和运行文件完整性审计。

## V2.4.0（2026-08-26）

### 关系、训练与俱乐部经营

- 整合人物双向关系、二维/三维关系网、训练档案和关系变化时间线；8F 关系综训楼与俱乐部名人堂继续遵守正式版隐藏或写入门禁。
- 新增位置训练预设与自定义阵型，训练速度调整为四倍速，并保留按版本布局、人物身份、旧值、回读和失败回滚的写入校验。
- 已收购俱乐部新增分级设施升级计划、财政与转会预算调拨、主席团资助类型读写，以及账户作用域的目标和资助类型回放保护。

### 盘口、结算与情报

- 盘口页新增最近 7 个可信赛前盘口日的“之前”浏览，展示原早盘赔率、开球时间和命中结果，但禁止历史比赛进入投注单。
- 串关与复式串关新增腿级部分结算展示；全部腿完成前仍保持整单待结算且不提前派彩。
- 收紧情报中心候选比赛与当前已发布盘口的交集、开球时间校验和当日锁价规则，避免旧缓存或过期比赛继续出售。

### 兼容、存储与发布

- FM26 Steam 身份识别同时校验 EXE 与 `game_plugin.dll`，游戏日期入口使用唯一 AOB 动态解析并与插件身份交叉校验。
- 增加当前账户双确认重置、人物库月度属性快照和多项账户恢复边界；不删除其他账户、生涯赛果账本或 FM 游戏存档。
- 同步 V2.4.0 运行入口、桌面宿主、版本资源、普通/保护封装配置和发布文档。

## V3.0（2026-08-21）

### 关系生态与界面

- 完成关系生态系统整合，补齐关系数据、二维关系图、三维关系图和 8F 关系综训楼的端到端展示与交互。
- 修复执教管理页空白、8F 训练页结构与容量显示异常，以及关系页面错误常驻导致的页面遮挡。
- 修复移动端关系三维图工具栏和操作区横向溢出，并补充前端回归测试。
- 修复二维关系网本队人数统计口径，并区分三维关系网中的现任/前任主教练及现任/前任队友；补齐银河视图的星座投影。
- 修正 8F 关系综训楼背景资源与存档身份绑定，避免已连接存档反复停留在身份确认状态。
- 修复活动中心娱乐职员列表空白和 `isEntertainmentStaff` 未定义异常，并恢复 V2.1.2 风格的亲密度排行榜。
- 重构执教管理职员页的分组、关键属性与关系信息展示。
- 训练档案补充实际器材或房间与训练内容；关系变化时间线的球员/职员分类支持即时伸缩并移除英文角色标签；执教管理子菜单支持再次点击收回。
- 娱乐中心职员活动改为双向关系原子写入：既有关系保留类别，缺失关系建立朋友；账户保存失败时同步回滚两个方向，并将双向变化写入时间线。
- 重排训练档案单次记录，以紧凑时间带展示实际训练时长和日期，属性变化改为 V2.1.2 风格的起止轴并使用单列记录布局。
- 扩充人物关系网三维荣誉陈列室，在保留现有关系节点、连线、相机、筛选与球衣墙交互的基础上加入中央主奖杯、玻璃侧柜、奖章墙、石材地坪、墙柱和展陈灯光。
- 训练档案统计改为真实设施使用口径：综合训练按房间类别汇总，教练进修按具体桌位实例汇总，并展示次数、游戏日和参与人数等可核对指标。
- 同步 V3.0 运行入口、桌面宿主、版本资源、保护封装配置和发布文档。

## V2.3.3d（2026-08-21）

### 发布与保护

- 同步版本资源、桌面宿主和普通/保护封装配置到 V2.3.3d。
- 延续正式运行文件完整性校验、受保护核心一致性检查和发布表面审计。

## V2.3.3c（2026-08-19）

### 发布与保护

- 同步版本资源、桌面宿主和普通/保护封装配置到 V2.3.3c。
- 延续正式运行文件完整性校验、受保护核心一致性检查和发布表面审计。

## V2.3.3（2026-08-18）

### 发布与保护

- 将 V2.3.3beta 固化为 V2.3.3 稳定版本，同步版本资源、桌面宿主和普通/保护封装配置。

## V2.3.3beta（2026-08-18）

### 发布与保护

- 同步版本资源、桌面宿主和普通/保护封装配置到 V2.3.3beta。
- 延续正式运行文件完整性校验、受保护核心一致性检查和发布表面审计。

## V2.3.2beta（2026-08-17）

### 发布与保护

- 同步版本资源、桌面宿主和普通/保护封装配置到 V2.3.2beta。
- 延续 72 个正式运行文件的完整性校验、受保护核心一致性检查和发布表面审计。

## V2.2.1e（2026-08-12）

- 版本号、Windows 版本资源、桌面宿主和普通/保护封装配置同步到 V2.2.1e。

## V2.2.1c（2026-08-12）

### 发布

- 版本号、Windows 版本资源、桌面宿主和普通/保护封装配置同步到 V2.2.1c。

## V2.2.1（2026-08-12）

### 发布与原生保护

- 版本号、Windows 版本资源、桌面宿主和普通/保护封装配置同步到 V2.2.1。
- P0 盘口数学、布局校验和普通内存写入迁移到 Rust；P1 Hook 签名扫描、相对跳转和可执行页补丁事务迁移到 C++。
- 正式版原生 DLL 增加固定路径加载、加载前 SHA-256 校验、受限依赖搜索和进程生命周期文件锁。

## V2.2.0d（2026-08-12）

### 发布

- 版本号、Windows 版本资源、桌面宿主和普通/保护封装配置同步到 V2.2.0d。

## V2.2.0c（2026-08-11）

### 发布

- 版本号、Windows 版本资源、桌面宿主和普通/保护封装配置同步到 V2.2.0c。

## V2.2.0beta（2026-08-11）

### 发布

- 版本号、Windows 版本资源、桌面宿主和普通/保护封装配置同步到 V2.2.0beta。

## V2.1.7（2026-08-11）

### 发布

- 版本号、Windows 版本资源、桌面宿主和普通/保护封装配置同步到 V2.1.7。

## V2.1.4（2026-08-05）

### 董事会与俱乐部愿景

- 已收购俱乐部董事会目标新增“签下指定国籍球员”：从经数据库根验证的 Nation 表读取稳定 UID 与愿景数据库 ID，写入 `reference_type=9`，不再依赖错误的男子国家容器 vtable；重要性统一只允许 `2/6/8/10` 四档。
- 新增赛事目标类型 37/38 的语义展示（避免在赛事中垫底、取得赛事前指定名次），并按 FMRTE 枚举隐藏 86（只签下巴斯克球员）、91（只能签会说威尔士语的球员）与宿敌排名 184/185 的新增入口。
- 赛事目标新增按“批量新增同赛事互斥目标”实机反证关闭，仅保留原位修改与逻辑删除；杯赛下拉框只提供杯赛适用类型。

### 董事会 Hook

- FM26 Steam 26.3.2 改用已验证固定 RVA 与原始字节校验，安装前不再整模块扫描特征码。
- 增加 FM26 残留 Hook 恢复：能识别旧服务异常退出遗留的跳板与愿景 `NOP`，形态一致时恢复原字节并释放代码洞。
- Hook 安装失败后按相同进程与布局身份抑制 5 分钟重试，避免旧服务残留导致反复失败。

### 球员与退役

- FM24/FM26 球员合同详情改为一次读取合同块并解析，减少逐字段读取；合同归属、球队、周薪、奖金与条款仍按各自布局回读。
- “退役计划交流”增加“十年后退役”与“二十年后退役”选项，已有记录仍只改写退役日并保留原生检查日。

### 存档与存储

- 设置新增“删除其他存档”（需二次确认）：只删除 `saves/` 根目录内除当前账户外的其他 `.fmodd` 文件，并同步清理账户索引、已选经理与作用域引用，不删除 FM 的 `.fm` 游戏存档。
- 存储管理按实际删除结果刷新账户缓存，相关注册表条目同步移除。

### 投注、性能与前端

- 比赛诚信审查新增本队净胜球豁免，不把本队净胜玩法计入异常累计。
- 串关套餐组合上限设为 5000，避免超大组合生成阻塞。
- 实时市场输出只保留必要的阵容画像字段，减小 `/api/state` 负载。
- 前端更新董事会目标选择器（目标/国籍双卡片）、退役选项与删除其他存档确认；同步更新 AGENTS.md、README 与架构文档至 V2.1.4。

## V2.1.2（2026-08-04）

### 人物库与名人堂

- 新增 `tools/club_legacy.py` 与主页一级应用“俱乐部人物库”：按稳定球员 UID 保存名单、名单变化、伤病、外租、CA、合同与退役计划时间线，球员未再出现时标记为历史成员而非转会。
- 名人堂入选、等级、收藏和备注按经理账户保存；同一 `career-*` 内按俱乐部 UID 建立可枚举索引，换队后仍可查看旧俱乐部；游戏日期回退时归档未来快照并隐藏未来事件。

### 董事会目标

- 新增 `tools/club_vision.py` 与已收购俱乐部“目标”子栏：支持结构兼容修改、新增与删除董事会目标，校验球队、Club vtable、愿景容器反向引用、vector 成员关系和期望旧值，失败时恢复原记录或原 vector。
- 新增使用目标进程 UCRT 分配原生记录并在 vector 满载时扩容指针数组；删除通过压缩指针数组逻辑移除，不主动释放游戏生命周期管理的对象。

### 训练场

- 训练场 3F 办公室增加教练考证课程：普通教练或玩家经理通过教练学习桌保存考证课程，到期后触发对应证书结算；2F 保留习惯塑形机独立周期。

### 投注与刷新

- 待结算注单增加“搜索赛果”深度恢复入口：先复用订单保存的赛程地址，再重建赛程索引和短期结果池，仍缺失时立即扫描持久赛果池。
- 赛果恢复、冠军盘与盘口刷新路径进一步优化，待结算订单可手动触发深度恢复。
- 冠军盘与赛果恢复、退休、训练场相关回归测试同步扩充。

## V2.1.1.1（2026-08-03）

- 版本号、版本资源与桌面宿主同步到 V2.1.1.1；无功能变更。

## V2.1.1（2026-08-03）

- 将刷新内存核心从 `tools/initial_data_audit.py` 拆分为受保护的 `tools/refresh_memory_core.py`，覆盖固定/动态赛程池发现、结构校验、同 slab 补全和有界私有内存回退扫描。
- 同步调整读取优化、世界俱乐部与前端相关回归测试。

## V2.1.0（2026-08-03）

### 读取架构

- 新增常驻只读进程会话 `tools/game_session.py`：复用进程句柄与模块身份，业务 Reader 按请求创建以避免缓存易变字段。
- 新增会话级原生数据库对象目录 `tools/database_index.py`：从版本 AOB 定位 Club/Competition/Nation/Person/Stadium/Team 表和人类经理容器，提供 UID/vtable 到实时地址的只读索引，失败时回退原有 slab/有界堆扫描。

### 俱乐部经营

- 世界俱乐部读取与已收购俱乐部快照增强：俱乐部资料缓存按需修补、收购记录从交易恢复、原生世界俱乐部快照读取。
- 联赛积分道具：商店提供单价 £1 的“联赛积分 +1”，服务端复用唯一 Standing 原生写入、操作键幂等和库存审计。
- 已收购俱乐部资金调配只在“联赛无排名、结余为 0、转会预算为 0”三项同时成立时禁止。

### 青训与成长

- 青训生成期 Hook 增加失效低频自愈与动态地址接管；生成期次数只记录进度，最终以正式名单写入回读人数为准。
- 青训计划保底与儿子历练事务继续完善，并同步扩充回归测试。

## V2.0.5beta / V2.0.5Dev（2026-08-02 至 2026-08-03，本地构建）

- 为 V2.1.0 前的本地开发与预览构建，保留对应版本资源与 spec；功能内容并入 V2.1.0，未单独发布源码提交。

## V2.0.4（2026-08-01）

- FM26 已收购俱乐部的球员转会、球员租借、俱乐部改名和球场改名暂未实装，相关按钮已禁用并阻止后续请求；FM24 保持原有功能。
- 移除银行 `+100B` 快捷资金入口，保留正常的银行、钱包、贷款和俱乐部资金流转。
- 更新桌面端版本资源、内嵌网页资源与保护核心，并完成完整回归测试。

## V2.0.0beta（2026-07-28 至 2026-07-29）

### 俱乐部经营

- 重新开放世界俱乐部收购，增加收购价格、已收购俱乐部组合和独立资料刷新。
- 已收购俱乐部可改名、调整财政与转会预算、升级设施、建设球场，并在旗下俱乐部间转会或租借球员。
- 增加俱乐部估值、收购成本、阵容价值和设施评分；增加按月结算的俱乐部分红。
- 银行恢复快速资金入口，保留钱包、贷款、工资、转会预算和俱乐部结余之间的资金流转。

### 球队与球员

- 新增活动中心，集中处理球员活动、交流、退役计划及相关任务。
- 完善青训计划、儿子历练、青训质量、原生青训生成 Hook 和存档隔离。
- 扩展训练场与食堂，增加专项训练、训练进度结算、营养计划、禁食设置和道具联动。
- 完善球员、职员、合同、未来转会、个人习惯、声望宣传册和经理执教记录的读取与展示。

### 投注与赛果

- 完善亚洲盘、半全场、精确比分、球队进球数和冠军盘的定价、下注校验与结算。
- 增强赛果恢复、跨赛季账本、异常波动校验和后台自动结算，减少跳日、切换界面或 FM 暂时不可读时的误判。
- 诚信审查和裁判事件增加更完整的通知、邮件和处罚记录。

### 兼容性与稳定性

- FM24 与 FM26 的 Steam、Epic、XGP 已识别版本共用功能语义，底层地址、AOB、vtable 和 Hook 仍按各自布局选择并执行运行时校验。
- 食堂 2/3 倍 CA 增长 Hook 补充 FM24 Epic 精确入口，并修复刷新时青年队、预备队和外租球员使用旧俱乐部地址的问题。
- FM24 Epic 开放已收购俱乐部的球员转移、租借、解约和挂牌，职员解雇与调动，以及主教练续约、更换和空缺岗位任命。
- 连接、刷新和后台读取状态进一步拆分；短暂时钟读取失败不再立即判定断开。
- 连接后固定 FM 进程 PID，避免同代 Steam/Epic 并行或其他 FM 进程残留时切换读取目标。
- FM24 Steam 精确 build 的董事会 Hook 改用已验证 RVA 与原字节校验，避免首次连接扫描完整模块；FM24 同存档刷新可复用经实时校验的经理与球队地址。
- 完善多经理、经理换队、更换存档和缓存复用时的生涯/账户作用域校验。
- 正式版继续使用 Cython 保护布局、比赛 Hook 和窄核心模块，并保留保护核心与 Python 基准的一致性校验。

## V1.9.2beta（2026-07-27）

- 重做首次连接、缓存核对和完整读取的状态管理，修复“已读到时间或资金，但界面仍显示未连接”以及重复点击后任务无法收尾的问题。
- 连接和刷新过程持续显示进度，区分快速刷新、完整读取、缓存验证和后台核对。
- FM26 在存档身份一致时逐项复核经理 UID、经理地址、球队 UID 和反向引用，命中后跳过遥测与全堆扫描。
- 修正旧存档中 `bool`、`list` 等历史数据与 Cython 编译模块之间的类型兼容问题，新旧存档均使用显式数据规范化。
- 优化 FM24 俱乐部、经理和存档身份的读取路径，减少重复扫描，并完善 Epic/XGP 的动态定位回退。
- 本版临时隐藏世界俱乐部收购入口和银行快速资金入口，后端能力保留。

## V1.9.1（2026-07-24 至 2026-07-27）

- 增加 FM24/FM26 诊断与适配探针，可报告进程身份、核心模块、RTTI、日期、存档根和本地刷新状态，不采集存档内容。
- 修复 FM24/FM26 首次连接、重新连接和旧存档读取中的类型异常，并改善无医疗任务时的伤病容器兼容。
- FM24 停用会在同一存档内变化的旧数值身份，改用本地存档提供器、经理对象和执教球队关系进行存档/账户归属。
- 增加世界俱乐部浏览、资料读取、收购与已收购俱乐部管理的基础能力。
- 扩展球员、职员、合同、转会预算、球员移动、退役、青训、训练场、食堂、医疗、声望和道具系统。
- 保护封装引入 Cython，将布局解析和赔率数学拆分为窄核心模块，增加保护核心与 Python 基准的一致性测试。
- 增加正式版 AI 使用声明与完整性校验；业务数据、缓存、投注与经济模块保持普通 Python 数据格式，避免影响旧用户存档。

## 3.0（2026-07-12）

- 钱包、投注、邮件、盘口快照、赛果历史和赛季账本按存档 ID 隔离。
- 切换职业生涯时整套账户数据随存档切换，并通过经理 ID 与游戏实例 ID 别名避免同一存档分裂。

## 3.1（2026-07-13）

- 增加可收起的投注、商店、物品栏、我的俱乐部和钱包侧边栏。
- 增加存档独立的钱包、投注记录、物品栏和银行转账；增加功能饮料、物品使用分类和俱乐部基础信息。
- 增加球员与职员卡片、合同信息、经理工资和俱乐部读取入口，并为比赛日道具增加目标比赛和安全校验。

## 3.2（2026-07-13）

- 重做投注历史卡片，拆分未结算与已结算页面，显示比赛时间、串关总赔率和净输赢。
- 增加球员完整属性、CA/PA、国籍、年龄、合同及职员业务能力；按守门员和非守门员区分可显示、可修改的属性。
- 增加功能饮料、全队功能饮料和黑哨道具，并加入比赛日 Hook 的安全检查和撤销机制。

## 3.3（2026-07-13）

- 将启动和更换存档刷新改为完整刷新，主页面刷新改为快速增量刷新。
- “我的俱乐部”从盘口刷新中拆离，只在首次打开或点击独立刷新按钮时读取。
- 快速刷新复用比赛与赛果地址索引，后台核对新增、改期和删除的比赛，并重新计算球队强度基准。

## 3.4（2026-07-13）

- 球员按前场、中场、后场、门将分类，卡片使用最高熟练位置；商店按比赛道具和属性道具分组。
- 物品栏的卖出改为无退款销毁；银行、自动周薪和外部钱包暂时移除，投注与商店共用钱包。
- 备份旧版源码和存档经济数据，保留随时恢复所需的文件。

## 3.5（2026-07-13）

- 功能饮料和黑哨使用提供的图标，并建立凡品、中品、上品、仙品、神品的颜色体系。
- 黑哨由一级扩展到三级，三级保留已实测的直接红牌效果，二级使用罪加一等判罚。
- 增加洗髓丹、培元丹、不稳定培元丹、破境丹、潜龙丹及品级限制、批量使用和随机属性提示。

## 3.6（2026-07-13）

- 修正商品图标的正方形比例、比赛中使用提示、属性道具横向排列和小窗口滚动布局。
- 增加属性隐藏开关、启动使用须知、作者二维码、B站链接、FMODD 确认框以及存储空间显示和缓存清理。
- 修复投注重复点击取消、同场比赛禁止自串、下注金额限制和页面字体可读性。

## 3.7（2026-07-13）

- 使用 WebView2 封装为独立桌面窗口，不再依赖外部浏览器。
- 增加高 DPI、应用图标、存档数据目录、空间占用显示和清理全部保存数据功能。
- 正式版数据保存到用户文档的 FMODD 文件夹，并在 FM26 退出后关闭。

## 3.8（2026-07-14 至 2026-07-15）

- 修复跳日后误判更换存档；普通刷新沿用当前固定存档 ID，只有“更换存档刷新”允许切换。
- 增加按存档保存的球员自定义姓名、道具和钱包，并完善属性道具的 CA/PA 边界、批量使用和守门员属性限制。
- 增加赛果保留 1、2、3、5 个赛季或不删除的设置，保护跨赛季未结算订单需要的旧赛果。

## 4.0 开发版（2026-07-15，尚未封装）

- 跳日时先保存并结算已读取的赛果，再发布新盘口和执行清理。
- 比赛超过 7 个游戏日仍无可读赛果时标记为“无赛果”并退回全部虚拟本金；取消、腰斩等无法取得赛果的异常比赛使用相同规则。
- 投注订单和钱包采用可恢复事务日志，写入中断后可同时恢复余额与订单。
- 同一游戏日期只保留最新盘口，同时保护未结算订单引用的盘口；“删除已结算”改为删除全部已结算订单。
- 大小球投注单和新旧历史订单统一显示具体盘口线，例如“大1.5”和“小2.5”。
- 正式版增加内嵌网页资源回退并提供目录兼容版，降低安全软件拦截临时网页文件造成首页 404 的风险。
- 当前源码开发序列为 4.0-dev；本次按用户指定封装为桌面正式版 V1.2。

## 历史版本（2.x 及更早）

### 2.24（2026-07-12）

- Classified competitions primarily from both participants' FM team type, so two national teams always enter National Team Events.
- Corrected the Southeast Asian Championship and expanded fallback recognition for regional national-team cups and qualifiers.
- Applied the same participant-ID classification to historical results.

## 2.23 Web - 2026-07-12

- Added a dollar sign to every displayed monetary value while keeping odds and numeric stake input unprefixed.
- Replaced the ISO `T` separator in displayed mail timestamps with a space.
- Canonicalised FM nation/team ID `380` from `荷属安的列斯` to `库拉索` across fixtures and results.

## 2.22 Web - 2026-07-12

- Published calibrated odds model `fm26-ca-form-dc-v1.5-casino` from 4,704 cleaned forecast/result pairs.
- Reduced the global expected-goal level by `exp(-0.04)` to correct systematic goal inflation.
- Changed Dixon-Coles rho from `-0.08` to `+0.04` to reduce excess low-score draw probability.
- Increased the squad-strength log coefficient conservatively from `0.0180` to `0.0185` and synchronised browser special-market pricing.

## 2.21 Web - 2026-07-12

- Isolated cumulative result history by FM `gameInstanceID` so switching saves no longer merges seasons with matching team IDs and dates.
- Added daily team-conflict resolution for stale result objects retained by FM after an in-process save switch.
- Prioritises verified schedule results and then newer active memory objects when conflicting results involve the same team on the same date.

## 2.20 Web - 2026-07-12

- Made Results open on the previous game day's 90-minute results by default.
- Moved Calendar into the bottom of the Results dialog and replaced the native picker with a filter-aware monthly calendar.
- Calendar dates with results are black, dates without results are grey, and future dates are disabled.

## 2.19 Web - 2026-07-12

- Made World Fixtures the default startup scope; Managed Team is now an explicit manual selection.
- Restored a red Results action beside Refresh.
- Added a season-results dialog that follows the current world/managed-team and all/favourites/competition filters, grouped newest date first.

## 2.18 Web - 2026-07-12

- Added a live game-clock cutoff check to every bet submission before wallet reservation or bet persistence.
- Allows betting only while the current FM date/time is less than or equal to the fixture kickoff date/time.
- Rejects the full slip when any parlay leg has started, is in the past or lacks a verified kickoff time.
- Records new bets with the precise FM game date and time.

## 2.17 Web - 2026-07-12

- Added a two-field FM26 match-engine detector validated over two enter/exit cycles on the same game date.
- Re-checks match-engine state from process memory for every bet submission and rejects bets while the engine is active.
- Fails closed when the engine-state combination is unknown, preventing bets after incompatible FM updates or ambiguous transitions.

## 2.16 Web - 2026-07-12

- Decoded the quarter-hour kickoff slot embedded in FM26 fixture memory and exposed it as `HH:MM`.
- Added kickoff time beside each fixture's competition name.
- Sorted matches within each game date by kickoff time before competition priority.
- Added a live four-byte game clock read to the API and top bar, independently updated without regenerating odds.

## 2.15 Web - 2026-07-12

- Moved Betting History out of Wallet into its own top-right action.
- Added persistent win-settlement mail generated only for newly settled winning bets after a refresh, with a three-second top-right notification and automatic mail archive.
- Simplified Wallet to non-functional Deposit and Withdraw placeholder actions.
- Added an opt-in Cheat Mode under Settings with `+500`, `+5000` and `清空所有金钱`, backed by auditable local wallet transactions.

## 2.14 Web - 2026-07-12

- Restored a direct `全部赛事` entry and added `收藏比赛` above the three collapsible competition groups.
- Added persistent team favourites keyed by FM team ID; home and away teams now have independent stars that remain yellow in every future fixture involving that team.
- Reworked fixture rows into a stable home-left, away-right matchup layout without changing the three quick 1X2 buttons.
- Replaced the top-right account cluster with Mail, Settings and Wallet actions; wallet funding and betting history now live under Wallet.
- Added a functional fair/casino pricing switch under Settings and made bet validation preserve the selected pricing mode.

## 2.13.1 Web - 2026-07-12

- Corrected `结算余额` to show each bet's wallet balance immediately after its sequential settlement, using `balance_after_settlement` rather than the bet payout.
- Kept one decimal place for total-goals and exact-score prices over 10 after rounding them to an integer step, for example `18.7 → 19.0`.

## 2.13 Web - 2026-07-12

- Replaced history-ticket totals with the requested four amounts: stake, expected return, profit and settled balance.
- Made settled losses show zero profit and zero settled balance; pending bets keep their final two values explicitly unsettled.
- Reworked the two-column amount layout so labels and neighbouring values no longer run together.
- Changed total-goals and exact-score display precision: prices over 10 show integers, prices over 5 show only `.0` or `.5`, and lower prices retain two decimals.

## 2.12 Web - 2026-07-12

- Replaced the `全部赛事` and `全部国家队赛事` navigation entries with three collapsible parents: league, cup and national-team competitions.
- Made each parent toggle open on its first click and closed on its second click, while concrete competitions remain the only fixture filters.
- Sorted available child competitions by the live competition reputation returned for the current schedule scope.
- Made fixture rows toggle their detailed betting markets open and closed on repeated clicks.

## 2.11 Web - 2026-07-12

- Replaced the linear team-CA input with a shared mildly convex CA curve for both clubs and national teams: `CA + 0.006 × max(CA - 145, 0)²`.
- Increased CA's role in mixed team strength: starting XI quality now carries 72% of the CA structure, candidate-20 depth 28%, and raw CA carries 82% versus an 18% fitness/sharpness adjustment.
- Kept recent form and morale as bounded pre-match adjustments so short samples cannot overwhelm squad quality.
- Added nonlinear starting-XI CA, candidate-20 CA, raw CA strength and conditioned CA strength to each team profile for later model auditing.

## 2.10 Web - 2026-07-12

- Benchmarked the opening 14-day World Cup window against OddsLab closing consensus prices across the captured 11-25 June market dataset.
- Added casino pricing as the default: 4% 1X2 overround, 4.5% Asian handicap/totals overround and 6% BTTS overround with mild favourite-longshot margin allocation.
- Preserved fair 1X2, BTTS, handicap and totals prices alongside casino prices so a later settings toggle can switch modes without regenerating probabilities.
- Treated World Cup finals fixtures as neutral-site matches and added a separate 2026 host-nation edge for Canada, Mexico and the United States.
- Reduced excessive CA and five-match-form sensitivity after the market comparison showed overconfident prices for several large-CA-gap fixtures.
- Added a reproducible OddsLab closing-price capture utility and stored the 11-25 June research snapshot.

## 2.9.1 Web - 2026-07-12

- Corrected FM nation IDs 5 and 6 to Algeria and Angola; the bundled Chinese NG Regens mapping had incorrectly translated them as Oceania and South America.

## 2.9 Web - 2026-07-12

- Added current-save manager discovery from live FM telemetry, including the save instance, manager ID/name, managed team and club/national-team role.
- Made `gameInstanceID` part of the runtime cache scope so switching saves cannot reuse fixtures, results, profiles or manager identity from the previous career.
- Added periodic save-identity checks and automatic refresh when the active career changes, even if `fm.exe` itself remains running.
- Replaced the low-address telemetry sweep with a newest-memory-first scan, fixing national-team careers such as Argentina while reducing identification time to about one second on the current process.

## 2.8 Web - 2026-07-12

- Excluded fixtures and season results whose national-team names cannot be resolved beyond an internal numeric ID.

## 2.7 Web - 2026-07-12

- Removed cross-save manager-name, manager-ID and previous-club fallbacks after they could misidentify a different player manager.
- Managed fixtures are now shown only after a current-save manager identity can be proven from live FM memory.

## 2.6 Web - 2026-07-12

- Made managed-team confirmation a startup prerequisite: cached fixtures no longer expose a previous save's managed club before the live FM check completes.
- Disabled the old verified-club fallback whenever a player-manager profile exists but cannot yet be confirmed in the active save.

## 2.5 Web - 2026-07-12

- Made managed-club resolution resilient to a new save by matching the live manager profile name when FM assigns a new profile ID.
- Removed the club-competition allowlist: World Fixtures now includes every scanned men's club competition with a scheduled match, including the EFL Championship.

## 2.4 Web - 2026-07-12

- Resolved the managed club through the saved human-manager profile ID and the live team-manager relationship, instead of relying on the active screen's team context.
- Kept telemetry and the last verified profile only as fallbacks when the managed club has no fixture in the current schedule window.

## 2.3 Web - 2026-07-12

- Added managed-team discovery from FM session telemetry with a last-verified team-profile fallback, and made its fixture list the default view.
- Added a World Fixtures switch for all currently scheduled catalogue matches.
- Limited competition navigation to competitions with fixtures in the active scope.
- Corrected national-team names to use the full localized nation name instead of the three-character display code.

## 2.2 Web - 2026-07-12

- Changed available handicap lines from 1.0, 1.5 and 2.5 to 0.5, 1.5 and 2.5.
- Showed the selected team's signed handicap in the betting slip and history, for example `柏太阳神 +1.5`.
- Reworked parlay history tickets around the parlay itself, with every leg showing its own fixture and selection.
- Removed settlement-time and before-settlement-balance display; settlement return and net profit/loss now use signed amounts.
- Added one-click clearing for unsubmitted bet-slip selections.
- Cleared all pending orders and returned their virtual stakes through an auditable wallet transaction.

## 2.1 Web - 2026-07-12

- Replaced the compact bet-history rows with independently arranged betting tickets.
- Recorded each pending bet settlement as its own wallet transaction, including its exact balance before and after settlement.
- Added settlement-balance migration for historical records when their previous aggregate wallet transaction can be resolved reliably.
- Added colour-coded post-settlement balance and return values for won, lost, pending and void bets.

## 2.0 Web - 2026-07-12

- Added a local-only browser workbench alongside the Tk desktop application.
- Reused the existing FM memory reader, odds snapshots, wallet and bet settlement without any external betting or payment integration.
- Reworked the interface around competition navigation, dense odds rows, an expandable market tray and a persistent bet slip.
- Moved expanded markets directly beneath their selected fixture and added slider-priced team goals, total goals and exact-score markets.
- Applied a consistent 15% market margin to the new discrete goal/score markets while retaining uncapped long-shot odds.
- Added J3 League (ID 791180) and standardized discrete-market prices to 0.05 increments.
- Made the web workbench the primary launch target; the Tk desktop window is retained only as legacy code.

## 1.17 - 2026-07-11

- Added immediate startup rendering from the latest local odds snapshot while FM memory refreshes in the background.
- Cached fixture addresses, same-game-date result scans, team profiles and competition baselines to remove redundant memory reads on repeat refreshes.
- Added refresh-stage timing metadata for live performance verification.

## 1.16 - 2026-07-11

- Added persistent season-result recovery for pending bets after FM clears short-lived result objects.
- Rebalanced model v1.2 toward candidate-player CA and reduced five-match scoring noise.
- Added an immutable season forecast ledger with first/latest forecasts and completed scores for post-season calibration.
- Reworked the market detail area into a compact three-column layout with mouse-wheel routing.
- Added a numeric stake keypad to the betting slip.
- Deferred settlement and visible results until the FM game date has passed the fixture date, preventing premature same-day result objects from settling bets.

## 1.15 - 2026-07-11

- Added a cumulative completed-result archive so captured scores remain available after FM clears old memory objects.
- Added game-date monitoring and an automatic background result capture on every date change.
- Refined the main schedule, account header, betting slip and history presentation.

## 1.14 - 2026-07-11

- Limited national-team competitions to World Cup and World Cup qualifying, European Championship, UEFA Nations League, Copa America and Asian Cup.
- Removed automatic seven-game-day refunds for missing results. Bets now remain pending until a read-only memory result is available.

## 1.13 - 2026-07-11

- Merged schedule, odds, completed-result retrieval and settlement into one Refresh action.
- Simplified bet history to game time, selection, odds, 90-minute score, stake, return and status.
- Kept parlay legs expandable with each leg's selection and final score.
- Replaced the 0.5 handicap market with 2.5; available lines are now 1.0, 1.5 and 2.5.
- Added verified-result merging for completed cup replays missing from FM's generic result object pool.
- Added the separate national-team object type, squad parsing and Chinese nation names so Nations League and World Cup qualifiers enter the same 14-day schedule, odds and result pipeline.

## 1.12.1 - 2026-07-11

- Fixed knockout matches that were level after 90 minutes being rejected when FM stored a penalty/advancement outcome code.
- Corrected previously voided bets automatically when the missing completed result is later recovered.
- Kept all 1X2, BTTS, handicap and total settlement based on the 90-minute score only.

## 1.12 - 2026-07-11

- Replaced single-competition generation with one 14-day all-competition refresh.
- Grouped upcoming matches by match date and sorted competitions by type and reputation.
- Kept watched competitions visible when they have no match in the 14-day window.
- Excluded women's competitions and added a persistent national-team filter.
- Added season result refresh/query and automatic bet settlement.
- Migrated legacy bets, recorded bets by FM game date and expanded parlay history details.
- Added automatic void/refund for fixtures still missing a result seven game-days after their scheduled date.

## 1.11 - 2026-07-11

- Reworked team strength using weighted best-11 and best-20 effective CA.
- Increased the response to meaningful CA gaps.
- Replaced the global J1 scoring baseline with a competition-specific baseline.
- Added shrunk recent goal-difference and morale adjustments.
- Added Dixon-Coles low-score correction across all derived markets.

## 1.10 - 2026-07-11

- Added the persistent virtual-money wallet and funding controls.
- Added bet history, automatic result settlement and payouts.
- Added potential return and potential profit to the bet slip.
## 2.25 Web - 2026-07-12

- Added a deterministic recent-form CA fallback when no valid player CA can be read for a team.
- Maps five-match points-per-game into a bounded 20-60 CA range with stable seeded variation; teams without recent results default to 40 CA.
- Applies the same fallback consistently to displayed averages and all squad-strength fields used by the odds model.
## 2.26 Web - 2026-07-12

- 每次成功刷新均在投注结算完成后执行自动空间清理。
- 按 FM 游戏内日期删除超过七天的历史盘口，并始终保留当前页面使用的盘口快照。
- FM 日期进入下一赛季的 7 月 1 日后，直接删除各存档上一赛季的累计赛果。
- 投注记录及下注赔率、钱包和赛季模型账本不参与清理。
## 2.27 Web - 2026-07-12

- 始终通过当前存档的玩家执教球队，实时读取该球队当前主教练人物 ID。
- FM 核心人物对象可能返回人员汉化前的原始姓名，因此改为读取人物 ID 和球队 ID 同时匹配的本地汉化身份。
- 两个 ID 任一不匹配时不显示经理姓名，避免再次显示无关人物或未汉化姓名。
## 2.28 Web - 2026-07-12

- 已验证 FM `PortalScreen` 页面下比赛引擎标志为 `phase=0, mode=0`，且比赛并未启动；该状态现在允许下注。
- `phase=0, mode=4` 仍是比赛引擎内状态，下注前的游戏时间、开球时间和盘口选项校验保持不变。
## 2.29 Web - 2026-07-12

- 仅将 `phase=0, mode=4` 判定为比赛引擎内并禁止下注。
- 其他所有可读取的组合均允许继续执行游戏时间、开球时间和盘口选项校验。
## 2.30 Web - 2026-07-12

- 在投注历史弹窗关闭按钮旁新增“一键清空”。
- 服务端按当前 FM 游戏日期计算一个自然月前的截止日，删除更早的已结算订单；未结算订单和截止日当天订单保留。
## 2.31 Web - 2026-07-12

- 串关严禁加入同一场比赛的多个选项；浏览器端以新选项替换旧选项，服务端再次拒绝重复比赛。
- 手动输入或数字键盘输入的下注金额超过当前钱包余额时，自动显示为钱包全部余额。
## 2.32 Web - 2026-07-12

- 比赛范围第一项调整为“世界比赛”，第二项为“执教球队”。
- 页面首次打开时默认显示并选中世界比赛。
## 2.32.1 Web - 2026-07-12

- 投注历史中的“已赢”和“未命中”统一显示为“已结算”，底层胜负、颜色和金额明细保持不变。
- 后续小型修正使用三段式修订版本号，不再每次递增功能版本号。
## 2.33 Web - 2026-07-12

- 比赛引擎内下注提示改为“检测到比赛引擎正在进行中，请返回到消息界面进行下注”。
- 设置中作弊模式的清零按钮改名为“清空”。
- 打包版持久化数据统一保存到用户文档 `FMODD` 文件夹；源码开发版继续使用项目内数据。
- 默认端口改为 `7856`，启动后自动打开浏览器，检测到 `fm.exe` 关闭后服务与程序自动退出。
- 提供 Windows 单文件 EXE；打包仅能提高逆向成本，无法保证绝对禁止逆向。
## 2.34 Web - 2026-07-12

- 执教球队识别从遥测唯一依赖改为直接读取 FM 内存中的人类主教练 vtable 与球队关系。
- 遥测只用于保留原存档 ID；另一台电脑没有遥测块时，按人类经理人物 ID 隔离数据。
- 多人存档检测到多个 human 主教练时不会武断选择，普通单人存档可跨机器自动识别。
## 3.0 Web - 2026-07-12

- 钱包、投注、邮件、盘口快照、赛果历史和赛季账本全部迁移到 `saves/<存档ID>/`，按职业生涯完整隔离。
- 切换 FM 存档时整套账户视图随之切换，再次打开旧存档时恢复原数据。
- 新增 human 经理回退 ID 到 FM `gameInstanceID` 的持久别名，避免遥测偶尔缺失导致同一存档分裂。
- 现有账户已迁移到存档 `780838`；新识别的存档 `7707270` 使用全新账户。
