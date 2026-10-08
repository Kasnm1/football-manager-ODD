# FMODD 功能索引

用于把“改银行账单、训练场、名人堂”等用户需求映射到代码。不是源码目录重组、完整功能规格或版本支持清单。模块所有权仍以 [ARCHITECTURE.md](ARCHITECTURE.md) 为准，行为以当前实现与测试为准。

## 使用与维护

- 已有明确文件、函数或 API 时直接定位；否则先按下表功能名称、别名或稳定 ID 搜索，只读命中条目及其必要引用，不要求通读索引。
- 总表提供主要入口；银行、训练场、名人堂另有静态核对的详细链路。其他条目是模块级路由，不表示已穷举其子功能、测试或副作用。
- 稳定 ID 是文档检索键，不是产品 API 或持久化字段。功能改名保留 ID 和旧名称别名；拆分时新增子 ID，不把旧 ID 改作无关用途。
- 改动功能入口、函数/API 调用链、数据所有权或相关测试位置时，只同步受影响条目。纯文案、颜色或实现内部变化且定位不变时无需更新。
- 索引与源码不符时以实际调用链为准，修正相关条目；未找到的子功能沿责任模块定向搜索，不猜测符号。函数名与选择器优先于易失效的行号。
- 测试名只是初始候选，按实际 diff 选择并检查 fixture 副作用，不默认整组执行。版本/build 支持和实机证据链接 [RESEARCH_SOURCES.md](RESEARCH_SOURCES.md)，不在这里复制矩阵。

## 主要功能入口

以下界面键来自 [web/index.html](../src/web/index.html)：`data-page` 是页面导航键，`#...` 是 DOM id。界面逻辑默认在 [web/app.js](../src/web/app.js)，API 注册与 `LocalOddsState` 门面在 [fm_odds_web.py](../src/fm_odds_web.py)；路由分派基础设施是 [tools/api_routing.py](../src/tools/api_routing.py)，不要误把它当作全部业务路由的定义文件。

JSON 业务分派只经过上述注册表；HTTP 修改响应来源契约在 [state_patches.py](../src/tools/state_patches.py)，绑定代际在 [app_paths.py](../src/tools/app_paths.py)。相关行为回归见 [test_http_mutation_context.py](../tests/test_http_mutation_context.py)、[test_api_http_dispatch.py](../tests/test_api_http_dispatch.py)。赛果结算链中的纯证据规则由 [result_evidence.py](../src/tools/result_evidence.py) 持有，生涯历史读写由 [result_history.py](../src/tools/result_history.py) 持有，原 `preview_cup_odds` 入口仍兼容；账本回归见 [test_result_history_owner.py](../tests/test_result_history_owner.py)。

设置中的账户切换由 `#apply-saved-account` 调用 `/api/save-account/select`，再通过现有共享同步器请求 fresh 状态；在途旧状态与切换完成顺序的执行回归见 [test_frontend_state_sync.py](../tests/test_frontend_state_sync.py)。

| 稳定 ID / 功能与别名 | 界面定位 | API 或状态入口线索 | 领域实现入口 |
| --- | --- | --- | --- |
| `connection` 主页、连接、换档刷新 | `data-page="home"` | `/api/connect`、`/api/state`、`/api/refresh/status`；`background_reconcile_ready` 区分已发布快照可用与刷新控件仍受门禁 | [game_session.py](../src/tools/game_session.py)、[save_context.py](../src/tools/save_context.py)、[local_state_services.py](../src/tools/local_state_services.py)；公共快照合并由 [state_publication.py](../src/tools/state_publication.py) 负责，轻量状态降级快照由 [refresh_status.py](../src/tools/refresh_status.py) 负责；隔离回归见 [test_public_state_publication.py](../tests/test_public_state_publication.py)、[test_refresh_status_scope.py](../tests/test_refresh_status_scope.py) |
| `page-prewarm` 页面数据预热、切页免刷新 | 所有 `data-page` 导航；`scheduleDataPrewarm`、`schedulePageIntentPrewarm` | `/api/state` 发布可信连接后，按作用域、日期、布局和经理/执教球队执行一轮浅预热；单纯盘口 `data_version` 变化只校准当前页，隐藏页由悬停/聚焦按资源版本深预热，失败资源独立指数退避，窗口隐藏或前台变忙即暂停，点击复用单航班请求 | [app.js](../src/web/app.js)、`LocalOddsState.public_world_players`、`relations_network`；自动阶段只取名人堂索引和关系范围，集团指标、任意详情、转会历史联动及写接口不进入自动预热 |
| `betting` 盘口、投注、早盘、之前 | `data-page="betting"` | `/api/match-markets`、`/api/bets` | [preview_cup_odds.py](../src/tools/preview_cup_odds.py)、[live_market.py](../src/tools/live_market.py)、[betting_account.py](../src/tools/betting_account.py)；读取后纯球队画像由 [odds_profiles.py](../src/tools/odds_profiles.py) 持有，边界回归见 [test_odds_profiles.py](../tests/test_odds_profiles.py) |
| `bet-history` 注单、投注历史、收益分析 | `#history-dialog` | `/api/history`、`/api/history/analysis` | [betting_account.py](../src/tools/betting_account.py)、[match_integrity.py](../src/tools/match_integrity.py)；收益分析只读视图在 [bet_analysis_page.js](../src/web/bet_analysis_page.js)，历史控制器仍在 `app.js`，回归见 [test_bet_analysis_page.py](../tests/test_bet_analysis_page.py) |
| `standings` 积分榜、ELO | `data-page="league"`、`#league-refresh-button` | `/api/state` 的赛事输出；手动按钮调用 `/api/league/refresh`，重读保留联赛并扩大一次新赛季对象发现范围 | [league_standings.py](../src/tools/league_standings.py)、[preview_cup_odds.py](../src/tools/preview_cup_odds.py) |
| `championship` 冠军盘、联赛/杯赛冠军 | `#championship-content` | `/api/championship-bets` | [championship_odds.py](../src/tools/championship_odds.py)、[betting_account.py](../src/tools/betting_account.py) |
| `world-clubs` 世界俱乐部、俱乐部详情 | `data-page="world-clubs"` | `/api/world-clubs`、`/api/world-clubs/detail` | [world_clubs.py](../src/tools/world_clubs.py)、[database_index.py](../src/tools/database_index.py) |
| `owned-clubs-performance` 集团指标、刷新、人员批量读取 | 我的集团刷新与品牌推广 | `/api/world-clubs/metrics?team_ids=...`、`/api/world-clubs/owned-refresh`、`/api/world-clubs/group-people` | `LocalOddsState`、[world_clubs.py](../src/tools/world_clubs.py) 的 `read_native_world_club_group_people`、[club_reader.py](../src/tools/club_reader.py) 的 `_scan_staff_for_teams`；隔离回归见 [test_group_performance.py](../tests/test_group_performance.py)、[test_group_performance_frontend.py](../tests/test_group_performance_frontend.py) |
| `owned-clubs` 我的集团、收购、出售、资金、设施 | `data-page="my-clubs"` | `/api/world-clubs/acquire`、`/api/world-clubs/finance-transfer` | [world_clubs.py](../src/tools/world_clubs.py)、[club_economy.py](../src/tools/club_economy.py)、[transfer_budget.py](../src/tools/transfer_budget.py)；设施/出售/品牌报价在 [club_operation_quotes.py](../src/tools/club_operation_quotes.py)，纯规则回归见 [test_club_operation_quotes.py](../tests/test_club_operation_quotes.py) |
| `people` 执教管理、球员、职员、历史转会、世界球员 | `data-page="club"`、`data-club-tab="transfers"`、`data-page="world-players"` | `/api/club`、`/api/club/transfer-history`、`/api/world-players/detail` | [club_reader.py](../src/tools/club_reader.py)、[player_movement.py](../src/tools/player_movement.py)、[transfer_history.py](../src/tools/transfer_history.py)；FM26 精确 Steam build 与 FM24 精确 Epic build 的历史日期、双方俱乐部和费用由 [future_transfers.py](../src/tools/future_transfers.py) 复用 TransferManager 原生归档读取，历史转会球员跳转复用名人堂人物档案，代际资料模块见架构 |
| `world-nations` 世界国家、国家球员榜 | `data-page="world-nations"` | `/api/world-nations`、`/api/world-nations/players` | `LocalOddsState` 世界国家用例、[database_index.py](../src/tools/database_index.py)、[club_reader.py](../src/tools/club_reader.py) |
| `bank` 银行、钱包、转账、贷款、账单 | `data-page="bank"` | [详细链路](#bank) | [club_economy.py](../src/tools/club_economy.py)、[betting_account.py](../src/tools/betting_account.py) |
| `items` 商店、物品栏、道具 | `data-page="shop"`、`data-page="inventory"`；显示文案由 [i18n.items.js](../src/web/i18n.items.js) 按 SKU 提供 | `/api/state`、`/api/inventory/use`；返老还童丹使用 `/api/inventory/reverse-age`，并通过 [retirement.py](../src/tools/retirement.py) 原子清理与恢复退役状态；账单投影返回 `item_sku` | [club_economy.py](../src/tools/club_economy.py)、[app.js](../src/web/app.js)；具体效果按架构定位，不能把所有道具视为同一写入路径；返老还童丹回归见 [test_age_reversal_pill.py](../tests/test_age_reversal_pill.py)、[test_retirement.py](../tests/test_retirement.py)，本地化契约见 [test_i18n_locale_packs.py](../tests/test_i18n_locale_packs.py) |
| `training` 训练场、器材、位置训练、训练档案 | `data-page="training"` | [详细链路](#training) | [training_ground.py](../src/tools/training_ground.py)、[training_runtime.py](../src/tools/training_runtime.py)、[training_archive.py](../src/tools/training_archive.py) |
| `activity` 活动中心、娱乐、心理辅导、情报 | `data-page="activity"` | `/api/activity`、`/api/activity/batch`、`/api/activity/counseling`、`/api/activity/intelligence` | [club_economy.py](../src/tools/club_economy.py)、[club_reader.py](../src/tools/club_reader.py)；情报候选还涉及 [preview_cup_odds.py](../src/tools/preview_cup_odds.py)；活动/海参批次名册复用及历史金额整理回归见 [test_batch_read_and_money_normalization.py](../tests/test_batch_read_and_money_normalization.py) |
| `activity.departure` 球员离队（活动中心 2F） | `data-page="activity"` → `player_departure`、`data-departure-search` | `GET /api/activity/player-departure`、`POST /api/activity/player-departure/{search,accept,reject}` | [player_departure.py](../src/tools/player_departure.py) 报价与账户轮次；[player_departure_application.py](../src/tools/player_departure_application.py) 所有权与成交编排；[player_movement.py](../src/tools/player_movement.py) 合同/名单及卖方转会预算事务；[player_departure_page.js](../src/web/player_departure_page.js) 报价卡片；回归 [test_player_departure.py](../tests/test_player_departure.py)、[test_player_movement.py](../tests/test_player_movement.py) |
| `canteen` 球队食堂、吃海参、集体吃海参 | `data-page="canteen"` | `/api/canteen/plan`、`/api/canteen/sea-cucumber`、`/api/canteen/sea-cucumbers` | [club_economy.py](../src/tools/club_economy.py)、[club_reader.py](../src/tools/club_reader.py)；海参回归见 [test_canteen_sea_cucumber.py](../tests/test_canteen_sea_cucumber.py)、[test_canteen_page.py](../tests/test_canteen_page.py) |
| `medical` 医院、伤病、治疗 | `data-page="hospital"` | `/api/state`、`/api/inventory/heal-player` | [club_economy.py](../src/tools/club_economy.py)、[club_reader.py](../src/tools/club_reader.py) |
| `youth` 青训计划、青训生成 | 我的集团中的青训操作 | `/api/world-clubs/youth-plan/status` 及同前缀操作 | [youth_intake.py](../src/tools/youth_intake.py)、[youth_generation_hook.py](../src/tools/youth_generation_hook.py)；建立计划的 UID 基线复用世界球员底层 `DatabaseIndex.person_uids_snapshot()`，采集全部已有人物并拒绝部分读取；儿子手动核验通过 `apply_youth_plan` → `finalize_academy_son_attributes(deep_search=True)` 强制刷新人物目录，逐对象校验新增 UID，再验证主合同所属 Club；保留已选候选锁。隔离回归见 [test_youth_son_search.py](../tests/test_youth_son_search.py) |
| `legacy` 名人堂、人物库、持续关注、最佳阵容 | `data-page="hall-of-fame"` | [详细链路](#legacy) | [club_legacy.py](../src/tools/club_legacy.py)、[player_portraits.py](../src/tools/player_portraits.py) |
| `relations` 关系网、双向关系、关系时间线 | `data-page="relations"` | `/api/relations/scopes` | [person_relationships.py](../src/tools/person_relationships.py)、[relationship_network.py](../src/tools/relationship_network.py)、[training_archive.py](../src/tools/training_archive.py) |
| `settings` 设置、存档账户、显示、语言、检查更新 | `#settings-dialog`、`#settings-button`、`#check-for-updates`、`#ignore-update`；启动后静默检查，有更新时“检查”原位替换为官网下载按钮，设置与下载入口显示红点，按版本号忽略本次更新后清除 | `/api/settings/ui-locale`、`/api/update-check`；账户身份看连接/保存用例 | [app_settings.py](../src/tools/app_settings.py)、[update_check.py](../src/tools/update_check.py)、[save_context.py](../src/tools/save_context.py)、[web/i18n.js](../src/web/i18n.js)；版本比较、官网清单校验和启动提示见 [test_update_check.py](../tests/test_update_check.py) |
| `mail` 邮箱、通知、奖励 | `#mail-dialog` | `/api/mail` | `LocalOddsState.mail_page`、[club_economy.py](../src/tools/club_economy.py)、[update_rewards.py](../src/tools/update_rewards.py) |

涉及赔率、下注、派彩、积分和冠军事实时读 [ODDS_ARCHITECTURE.md](ODDS_ARCHITECTURE.md)；账户、并发、训练和经营规则读 [RUNTIME_CONTRACTS.md](RUNTIME_CONTRACTS.md) 的相关节；视觉与交互读 [UI_SYSTEM.md](UI_SYSTEM.md)。总表不替代这些契约。

## championship

冠军盘按原生赛季与阶段定位：`generate_all_odds` → `native_competition_format_snapshots` / `native_terminal_final_fixture` → `build_championship_markets` → `publish_championship_markets` → `settle_championship_bets`。联赛积分事实由 `league_standings` 提供；杯赛决赛、两回合与历史结算规则见 [ODDS_ARCHITECTURE.md](ODDS_ARCHITECTURE.md#6-冠军盘)。当前界面只消费当前赛季，历史市场通过 `settlement_competitions` 保留。

初始测试候选：[test_championship_odds.py](../tests/test_championship_odds.py)、[test_championship_lifecycle.py](../tests/test_championship_lifecycle.py)；原生淘汰轮次另看 [test_knockout_results.py](../tests/test_knockout_results.py)。关闭冠军盘后仍需追踪待结算注单，缓存清理保护杯赛和联赛快照。

## bank

董事会目标位于已收购俱乐部详情的“目标”页：`renderOwnedClubVision` / `createOwnedClubVision` → `/api/world-clubs/vision` → `LocalOddsState.update_owned_world_club_vision` → [club_vision.py](../src/tools/club_vision.py)。接口通过 `_timed_user_memory_operation("owned_club_vision")` 进入前台优先内存操作门；新增与修改借用请求级 `GameOperationSession` 的会话目录。响应中的 `created_item` / `updated_item` 来自写后回读校验，前端只追加或替换命中行并保留原有目标与目录，不再在成功路径重读整份俱乐部及国家/赛事目录。删除仍沿用完整目标响应。隔离回归见 [test_club_vision.py](../tests/test_club_vision.py) 的 `test_create_reuses_operation_directory_and_retains_rollback`、`test_update_reuses_operation_directory_without_process_rediscovery`、`test_update_endpoint_returns_readback_without_post_commit_scans`、`test_updated_vision_receipt_replaces_only_matching_ui_row`；真实 FM 延迟仍需匹配版本实测。

设施升级计划由 `upgradeOwnedClubFacility` → `/api/world-clubs/facility-upgrade` → `upgrade_world_club_facility` 创建。计划创建只通过 `read_native_club_facility_levels` 读取并验证四个设施标量，不再读取包含球场、财政、债务和董事会目标的完整俱乐部快照；响应以 `owned_actions_patch` 仅更新命中设施计划。所有 HTTP POST 请求在线程作用域内标记为前台内存请求，使其内部既有 `with memory_lock` 也进入前台优先队列，后台刷新不能在已排队的用户操作前继续插队。

别名：银行、钱包转账、贷款、统一账本、账单中心。关键区别：账单是投影；转账和贷款是账户事务；向 FM 俱乐部预算/结余调配资金是另一个原生写入边界。

| 层 | 定位与关系 |
| --- | --- |
| 界面与样式 | `#page-bank`、`#bank-content`、`#bank-statement-dialog`；[app.css](../src/web/app.css) 的 `.bank-layout`、`.bank-statement-dialog` |
| 渲染与事件 | `renderBank`；`openBankStatement` → `loadBankStatement` → `renderBankStatementDialog`。筛选/分页状态在 `app.bankStatementFilters`，响应发布受请求键约束，切账户清理旧账单 |
| 账单读取 | `GET /api/bank/statement` → `_get_bank_statement_route` → `club_economy.account_statement_page`，读取已提交的银行与钱包流水，不重新计算派彩 |
| 账户操作保护 | 银行/钱包转账、商店及免费服务开关由 [account_application.py](../src/tools/account_application.py) 通过 `LocalOddsState._account_operation` 执行；持锁覆盖账户绑定至补丁响应，回归见 [test_account_application.py](../tests/test_account_application.py) |
| 转账/贷款 | `POST /api/bank/transfer` → `LocalOddsState.transfer_wallet_funds` → `AccountApplicationService.transfer_wallet` → `club_economy.transfer_wallet`；借贷入口为 `borrow_bank_credit` / `repay_bank_credit` |
| 数据与副作用 | 银行、钱包和贷款属当前经理账户，金额逻辑还涉及 [money.py](../src/tools/money.py)。账单展示不得写余额；转账需账户事务。`/api/bank/transfer-budget`、`/api/bank/club-balance` 不属于纯 ODD 内部转账，实际操作需 FM 身份校验和回滚 |
| 初始测试候选 | [test_wallet_transfer.py](../tests/test_wallet_transfer.py)、[test_money_currency.py](../tests/test_money_currency.py)、[test_club_funds_wallet_top_up.py](../tests/test_club_funds_wallet_top_up.py)；账单具体断言按 `statement` 在相关测试定向定位 |

改账单布局先看前端；改退款幂等沿产生原交易的业务入口回溯，不在账单投影层补钱。约束见运行时契约“账户存档与缓存”和赔率架构“投注与结算”。

## training

别名：训练场、楼层、器材、位置训练、教练进修、训练档案；关系综训是关联子系统，不等同于普通器材训练。

| 层 | 定位与关系 |
| --- | --- |
| 界面与样式 | `#page-training`、`#training-scene-tabs`、`#training-stage`、`#training-archive-dialog`；[app.css](../src/web/app.css) 的 `.training-stage`、`.training-header` |
| 渲染与事件 | `renderTraining`、`initializeTrainingDrag`；位置训练用 `startPositionTrainingAssignments`；档案用 `loadTrainingArchive` → `renderTrainingArchive` |
| 安排与设施 | `POST /api/training/run` → `LocalOddsState.training_run`；`purchase` / `place` / `cancel` 路由分别进入 `training_purchase` / `training_place` / `training_cancel`。普通任务记录与投影由 `training_ground` 负责 |
| 结算 | 自动入口为 `LocalOddsState._request_training_settlement`、`_training_settlement_worker`；页头手动入口 `POST /api/training/settle` → `training_settle` 使用前台优先内存事务，两者共用 `_sync_training_focuses`。人员回绑看 `training_runtime`，属性/位置完成记录看 `training_ground`。不能只改前端天数而遗漏实际积累/结算 |
| 档案与关系 | `GET /api/training/archive/summary`、`POST /api/training/archive/page`；归档由 `training_archive` 负责。综训再看 [relationship_training.py](../src/tools/relationship_training.py)、[person_relationships.py](../src/tools/person_relationships.py)，保留当前开发/冻结门禁 |
| 数据与副作用 | `app.state.training_ground` 是展示快照；设施、任务、消费、进度按经理账户隔离。`client_submission_id` 参与安排幂等；人员地址必须会话复验。到期效果可能写 FM 属性/位置/关系，修改 UI 不授予实机结算权限 |
| 初始测试候选 | [test_training_ground.py](../tests/test_training_ground.py)、[test_training_archive.py](../tests/test_training_archive.py)；只在涉及综训时选 [test_relationship_training.py](../tests/test_relationship_training.py)、[test_relationship_training_frontend.py](../tests/test_relationship_training_frontend.py) |

训练视觉与器材布局、任务进度、原生效果、档案投影分别定位，不把存档数据或后台结算真值放进前端。详细边界见运行时契约“Hook、活动、训练与医疗运行时契约”。

## legacy

别名：名人堂、人物库、球员生涯档案、持续关注、世界关注、最佳阵容、最佳 11 人。`legacy.best-team` 是最佳阵容子 ID，与 `training` 的位置训练只有界面风格关联，不共用业务语义。

| 层 | 定位与关系 |
| --- | --- |
| 界面与样式 | `#page-hall-of-fame`、`#hall-of-fame-content`；[app.css](../src/web/app.css) 的 `.hall-of-fame-page`、`.legacy-best-team-pitch` |
| 渲染与事件 | `loadClubLegacyIndex`、`renderHallOfFame`、`renderClubLegacy`、`bindClubLegacyControls`；最佳阵容用 `renderLegacyBestTeam`、`recommendLegacyBestTeam`、`saveLegacyBestTeam` |
| 读取与偏好 | `GET /api/club-legacy` → `LocalOddsState.public_club_legacy` → `club_legacy` 公共投影；人物档案的转会区同时按球员 UID 与所查看俱乐部消费 `GET /api/club/transfer-history` 的只读有效投影；`POST /api/club-legacy/preference` → `update_club_legacy_preference` → `set_club_legacy_preference` |
| 刷新编排 | `_club_refresh_worker` 先发布球队资料，再经 `local_state_services.ClubLegacyRefreshCoordinator` 串行合并附属归档；`club_legacy_status` 独立驱动人物库完成更新，GET 不触发归档。行为检查：[test_club_legacy_refresh.py](../tests/test_club_legacy_refresh.py)、[test_club_legacy_refresh_frontend.py](../tests/test_club_legacy_refresh_frontend.py) |
| 最佳阵容保存 | `saveLegacyBestTeam` → `POST /api/club-legacy/best-team` → `LocalOddsState.update_club_legacy_best_team` → `club_legacy.set_club_legacy_best_team`；携带 `team_id`、`lineup_kind`、`season_key`、`formation`、`assignments` |
| 数据与副作用 | 观察到的球员快照和赛季事实属生涯；阵容选择与偏好属经理账户，隔离俱乐部/赛季。最佳阵容保存不写 FM 内存；刷新人物资料是另一个读取/归档流程。统计缺失不能伪造为 0，人工记录不冒充原生事实 |
| 初始测试候选 | [test_club_legacy.py](../tests/test_club_legacy.py)、[test_club_legacy_frontend.py](../tests/test_club_legacy_frontend.py)；用 `best_team` / `best-team` 定位最佳阵容相关断言 |

头像来源查 `player_portraits`；原生球员统计按架构进入代际资料模块，build 证据看研究索引。不要因修改阵容卡片而启动训练事务或重读整个世界人员库。

## 核对边界

本索引初版于 2026-09-06 按当前源码入口、路由和符号静态核对；未启动游戏、服务或浏览器，未执行上述业务测试。路径/符号存在不证明运行时行为或所有发行版兼容；后续修改以相关调用链复核和风险相称的验证为准。
