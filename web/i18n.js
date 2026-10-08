(() => {
  "use strict";

  const STORAGE_KEY = "fmodd-ui-locale";
  const DEFAULT_LOCALE = "en-GB";
  const SOURCE_LOCALE = "zh-CN";
  const SUPPORTED_LOCALES = Object.freeze([
    "en-GB", "zh-CN", "zh-TW", "ko-KR", "de-DE", "es-ES", "fr-FR", "ru-RU", "ja-JP",
    "pt-BR", "pt-PT",
  ]);
  const SUPPORTED_SET = new Set(SUPPORTED_LOCALES);
  const ENGLISH_KICKER_LOCALES = new Set(["zh-CN", "zh-TW", "ko-KR", "ja-JP"]);
  const ENGLISH_KICKER_KEY = /(\.eyebrow|_eyebrow|\.kicker|_kicker)(?:$|_)/;
  const localePacks = window.FMODDLocalePacks && typeof window.FMODDLocalePacks === "object"
    ? window.FMODDLocalePacks : {};
  const externalModules = Array.isArray(window.FMODDI18nModules)
    ? window.FMODDI18nModules.filter((entry) => entry && typeof entry === "object")
    : [];
  const sourceTextNodes = new WeakMap();
  const sourceAttributes = new WeakMap();
  const keyedStaticTextValues = new Map();

  const messages = Object.freeze({
    "zh-CN": Object.freeze({
      "settings.language": "界面语言",
      "settings.saving": "正在保存设置",
      "settings.refreshing": "正在刷新数据",
      "home.connecting_save": "正在连接存档",
      "home.reading_identity": "正在读取存档、经理和执教队伍",
      "home.reading_current_save": "正在读取当前存档",
      "home.detected_game": "已检测到 Football Manager",
      "home.not_connected": "尚未连接存档",
      "home.enter_save": "请进入存档后点击连接",
      "home.launch_game": "启动游戏并进入存档后连接",
      "home.game_date": "游戏日期 {date}",
      "home.current_team_record": "执教本队以来",
      "home.matches_played.one": "共 {count} 场",
      "home.matches_played.other": "共 {count} 场",
      "home.record_unavailable": "暂时无法读取执教战绩",
      "home.record_loading": "正在整理执教战绩",
      "home.record_preparing": "执教战绩准备中",
      "home.retry_later": "稍后刷新",
      "home.loading": "读取中",
      "home.waiting": "等待资料",
      "home.connect": "连接存档",
      "home.connecting": "正在连接",
      "home.refreshing": "刷新中",
      "home.reconnect": "重新连接",
      "home.connected": "已连接 · {name}",
      "home.last_detected": "上次识别 · {name}",
      "usage.less_than_minute": "不足 1 分钟",
      "usage.days_hours": "{days} 天 {hours} 小时",
      "usage.hours_minutes": "{hours} 小时 {minutes} 分钟",
      "usage.minutes": "{minutes} 分钟",
      "usage.calculating": "正在统计",
      "settings.locale_saved": "界面语言已应用",
      "settings.locale_failed": "界面语言保存失败",
      "shell.desktop": "FMODD 桌面",
      "page.home.eyebrow": "FOOTBALL MANAGER 伴侣工具",
      "page.shop.eyebrow": "俱乐部经济", "page.shop.title": "商店",
      "page.car_lottery.eyebrow": "汽车抽奖", "page.car_lottery.title": "汽车奖池",
      "page.inventory.eyebrow": "我的物品", "page.inventory.title": "物品栏",
      "page.bank.eyebrow": "FMODD 财务", "page.bank.title": "银行",
      "page.activity.eyebrow": "俱乐部服务", "page.activity.title": "活动中心",
      "page.hospital.eyebrow": "球员健康", "page.hospital.title": "俱乐部医疗中心",
      "page.training.eyebrow": "球员发展", "page.training.title": "训练场",
      "page.canteen.eyebrow": "俱乐部营养", "page.canteen.title": "食堂",
      "page.club.eyebrow": "球队运营", "page.club.title": "执教管理",
      "page.hall_of_fame.eyebrow": "俱乐部传承", "page.hall_of_fame.title": "俱乐部名人堂",
      "page.relations.eyebrow": "关系管理", "page.relations.title": "人物关系网",
      "page.world_clubs.eyebrow": "全球数据库", "page.world_clubs.title": "世界俱乐部",
      "page.world_players.eyebrow": "全球数据库", "page.world_players.title": "世界球员",
      "page.world_nations.eyebrow": "国际数据库", "page.world_nations.title": "世界国家",
      "page.my_clubs.eyebrow": "俱乐部投资组合", "page.my_clubs.title": "我的集团",
    }),
    "en-GB": Object.freeze({
      "settings.language": "Interface language",
      "settings.saving": "Saving settings",
      "settings.refreshing": "Refreshing data",
      "home.connecting_save": "Connecting to save",
      "home.reading_identity": "Reading save, manager and managed teams",
      "home.reading_current_save": "Reading current save",
      "home.detected_game": "Football Manager detected",
      "home.not_connected": "No save connected",
      "home.enter_save": "Open a save, then select Connect",
      "home.launch_game": "Start Football Manager and open a save to connect",
      "home.game_date": "Game date {date}",
      "home.current_team_record": "Since taking charge",
      "home.matches_played.one": "{count} match",
      "home.matches_played.other": "{count} matches",
      "home.record_unavailable": "Manager record is temporarily unavailable",
      "home.record_loading": "Preparing manager record",
      "home.record_preparing": "Manager record pending",
      "home.retry_later": "Refresh later",
      "home.loading": "Loading",
      "home.waiting": "Waiting for data",
      "home.connect": "Connect save",
      "home.connecting": "Connecting",
      "home.refreshing": "Refreshing",
      "home.reconnect": "Reconnect",
      "home.connected": "Connected · {name}",
      "home.last_detected": "Last detected · {name}",
      "usage.less_than_minute": "Less than 1 minute",
      "usage.days_hours": "{days}d {hours}h",
      "usage.hours_minutes": "{hours}h {minutes}m",
      "usage.minutes": "{minutes} min",
      "usage.calculating": "Calculating",
      "settings.locale_saved": "Interface language applied",
      "settings.locale_failed": "Could not save the interface language",
      "shell.desktop": "FMODD desktop",
      "page.home.eyebrow": "FOOTBALL MANAGER COMPANION",
      "page.shop.eyebrow": "CLUB ECONOMY", "page.shop.title": "Shop",
      "page.car_lottery.eyebrow": "CAR LOTTERY", "page.car_lottery.title": "Car Prize Pool",
      "page.inventory.eyebrow": "MY ITEMS", "page.inventory.title": "Inventory",
      "page.bank.eyebrow": "FMODD FINANCE", "page.bank.title": "Bank",
      "page.activity.eyebrow": "CLUB SERVICES", "page.activity.title": "Activity Centre",
      "page.hospital.eyebrow": "PLAYER CARE", "page.hospital.title": "Club Medical Centre",
      "page.training.eyebrow": "PLAYER DEVELOPMENT", "page.training.title": "Training Ground",
      "page.canteen.eyebrow": "CLUB NUTRITION", "page.canteen.title": "Canteen",
      "page.club.eyebrow": "TEAM OPERATIONS", "page.club.title": "Club Management",
      "page.hall_of_fame.eyebrow": "CLUB LEGACY", "page.hall_of_fame.title": "Club Hall of Fame",
      "page.relations.eyebrow": "RELATIONSHIP MANAGEMENT", "page.relations.title": "Relationships",
      "page.world_clubs.eyebrow": "GLOBAL DATABASE", "page.world_clubs.title": "World Clubs",
      "page.world_players.eyebrow": "GLOBAL DATABASE", "page.world_players.title": "World Players",
      "page.world_nations.eyebrow": "INTERNATIONAL DATABASE", "page.world_nations.title": "Nations",
      "page.my_clubs.eyebrow": "CLUB PORTFOLIO", "page.my_clubs.title": "My Group",
    }),
  });

  // Transitional mappings are deliberately scoped to the shell, home page,
  // settings and first-run notice. Domain pages migrate to stable keys in
  // later modules; this adapter never scans or rewrites the full application.
  const legacyEnglish = Object.freeze({
    "展开或收起主菜单":"Expand or collapse main menu",
    "主要应用导航":"Primary application navigation",
    "90分钟":"90 min",
    "主页":"Home", "投注":"Betting", "积分榜":"League tables", "世界":"World",
    "世界俱乐部":"World clubs", "世界球员":"World players", "世界国家":"Nations",
    "我的集团":"My group", "商店":"Shop", "物品栏":"Inventory", "银行":"Bank",
    "活动":"Activities", "球队设施":"Club facilities", "医院":"Medical centre",
    "训练场":"Training ground", "食堂":"Canteen", "执教管理":"Club management",
    "执教队伍":"Managed teams", "执教管理分类":"Club management sections",
    "俱乐部球员":"Players", "职员":"Staff", "合同":"Contracts",
    "人物关系网":"Relationships", "关系总览":"Overview",
    "关系变化时间线":"Relationship timeline", "双向关系对照页":"Compare relationships",
    "经理":"Manager", "切换当前经理":"Switch current manager", "读取中":"Loading",
    "正在刷新 1%":"Refreshing 1%", "正在识别存档":"Detecting save",
    "邮件":"Mail", "设置":"Settings", "历史":"History", "钱包，余额 £0.00":"Wallet, balance £0.00",
    "选择一个应用，开始你的俱乐部工作。":"Choose an app to start managing your club.",
    "累计使用时间":"Total usage", "当前连接":"Current connection", "尚未连接存档":"No save connected",
    "启动 Football Manager 并进入存档后连接":"Start Football Manager and open a save to connect",
    "连接存档":"Connect save", "连接存档进度":"Save connection progress", "尚未开始连接":"Connection not started",
    "正在连接存档":"Connecting to save", "这可能需要2分钟，请耐心等待。":"This may take up to two minutes.",
    "胜":"W", "平":"D", "负":"L", "胜率":"Win rate", "共 0 场":"0 matches",
    "FMODD 应用":"FMODD apps", "投注":"Betting", "积分榜":"League tables",
    "我的集团":"My group", "物品栏":"Inventory", "活动":"Activities", "球队设施":"Club facilities",
    "盘口读取范围":"Market scan scope", "收藏赛事":"Favourite competitions", "知名比赛":"Major matches",
    "全部比赛":"All matches", "盘口时间":"Market window", "14天":"14 days", "7天":"7 days", "3天":"3 days",
    "应用并刷新":"Apply and refresh", "更换存档刷新":"Refresh after changing save",
    "切换存档后完整重建赛程、赛果和盘口缓存":"Rebuild fixtures, results and market caches after switching save.",
    "存档账户":"Save account", "如遇到存档丢失，可尝试切换存档":"Switch accounts if the current save cannot be found.",
    "跟随自动识别":"Use automatic detection", "切换":"Switch", "合并同ID账户":"Merge same-ID accounts",
    "夜间模式":"Dark mode",
    "简略数字":"Compact numbers", "反转盈亏颜色":"Reverse profit/loss colours",
    "开启后盈利显示绿色，亏损显示红色":"When enabled, profit is green and loss is red.",
    "界面字体":"Interface fonts", "中文字体":"Chinese font",
    "简体中文":"Simplified Chinese",
    "英文字体":"Latin font", "界面字号":"Interface text size", "拖动滑块逐级调整，共六档":"Six text-size steps.",
    "调整界面字号":"Adjust interface text size", "最小":"Smallest", "较小":"Smaller", "默认":"Default",
    "稍大":"Larger", "较大":"Large", "最大":"Largest", "标准":"Regular", "加粗":"Bold",
    "微软雅黑":"Microsoft YaHei", "微软正黑体":"Microsoft JhengHei", "黑体":"SimHei", "宋体":"SimSun",
    "新宋体":"NSimSun", "楷体":"KaiTi", "仿宋":"FangSong", "等线":"DengXian",
    "谷歌思源黑体":"Noto Sans CJK SC", "思源黑体":"Source Han Sans SC",
    "作弊模式":"Cheat mode", "设置投注上限":"Set betting limits", "单关":"Singles", "串关":"Accumulators",
    "单关投注上限":"Singles betting limit", "串关投注上限":"Accumulator betting limit",
    "保存投注上限":"Save betting limits", "无上限":"Unlimited", "商店0元购":"Free shop purchases",
    "关闭足协":"Disable FA penalties", "清空赌场钱包":"Clear betting wallet", "清空贷款":"Clear loans",
    "显示隐藏属性":"Show hidden attributes", "数据保存目录":"Data storage location",
    "正在读取路径...":"Reading path…", "正在计算占用...":"Calculating storage…", "选择路径":"Choose folder",
    "清除缓存":"Clear cache", "删除当前存档":"Delete current save", "删除其他存档":"Delete other saves",
    "支持作者👍":"Support the author 👍", "如果这个工具对你有帮助，可以请作者喝杯可乐":"If FMODD helps you, you can support its continued development.",
    "打开二维码":"Open QR code", "关注作者B站UID：":"Follow the author on Bilibili, UID: ", " 获取更新":" for updates",
    "任务与操作记录":"Tasks and operations", "本机保留最近 100 条":"The latest 100 records are kept locally.",
    "清空记录":"Clear records", "操作记录汇总":"Operation summary", "最近任务与操作记录":"Recent tasks and operations",
    "金额单位":"Currency", "英镑 (£)":"Pound sterling (£)", "美元 ($)":"US dollar ($)",
    "欧元 (€)":"Euro (€)", "人民币 (¥)":"Chinese yuan (¥)",
    "较大金额使用 K、M、B 和 T 单位并保留一位小数":"Large amounts use K, M, B and T with one decimal place.",
    "盘口自动刷新":"Automatic market refresh", "手动":"Manual", "是否开启冠军盘":"Enable outright markets",
    "关闭资源占用较大的冠军盘":"Disable the more resource-intensive outright markets.",
    "球员头像包":"Player facepack", "选择 graphics 文件夹":"Choose graphics folder", "选择 config.xml":"Choose config.xml",
    "清除全部选择":"Clear all selections",
    "常规":"General", "盘口":"Markets", "显示":"Display", "作弊":"Cheats", "记录":"Records", "设置分类":"Settings sections",
    "正在保存设置":"Saving settings", "正在刷新数据":"Refreshing data",
    "使用须知":"Usage notice", "我明白":"I understand",
    "FMODD 中的投注、赔率、赌场等相关功能，仅用于丰富 Football Manager 的游戏体验，与现实世界中的赌博、博彩及现金交易无任何关联。严禁将本工具用于任何形式的现实赌博、非法投注或其他违法违规活动。":"FMODD's betting, odds and casino features exist only to enhance the Football Manager experience. Do not use this tool for real-world gambling, illegal betting or cash transactions.",
    "本工具永久免费。如您喜欢本工具，可以在“设置”中留言并投喂作者。":"FMODD is free. If you enjoy it, you can support the author from Settings.",
    "您的支持是 FMODD 持续更新和完善的最大动力。":"Your support helps continued development of FMODD.",
    "请尽量避免在比赛加载、保存游戏或度假过程中使用相关功能，以减少未知错误。":"Avoid using related features while a match is loading, the game is saving, or the manager is on holiday.",
    "严禁任何个人或组织以 FMODD 名义进行收费售卖、付费授权、捆绑销售或其他牟利行为。请勿购买任何所谓的“付费版”或“授权版”，谨防受骗。":"FMODD must not be sold, licensed for a fee, bundled for profit or otherwise monetised by third parties. Do not purchase alleged paid or licensed editions.",
    "因 Football Manager 游戏版本存在差异，部分功能可能失效或导致数据异常。":"Football Manager version differences may cause some features to fail or produce unexpected data.",
    "FMODD 仍在持续更新和完善中。如您遇到错误、需要反馈问题、获取最新版本或参与交流，请加入官方 QQ 群：":"FMODD remains under active development. For errors, feedback, updates and discussion, join the official QQ group: ",
    "进行中":"Running", "成功":"Succeeded", "失败或未确认":"Failed or unconfirmed", "已完成":"Completed",
    "失败":"Failed", "结果未确认":"Result unconfirmed", "未连接存档":"No save connected", "定位：":"Location: ",
    "暂无操作记录":"No operation records", "刷新、购买、训练、医疗、资金和集团操作会显示在这里":"Refreshes, purchases, training, medical, finance and group operations appear here.",
    "世界比赛":"World fixtures", "收藏队伍":"Favourite teams", "收藏队伍赛程":"Favourite team fixtures", "之前":"Previous", "今日":"Today", "明日":"Tomorrow",
    "搜索球队或赛事":"Search teams or competitions", "赛果":"Results", "手动退款":"Manual refund", "赛事":"Competitions",
    "日期":"Date", "全部赛事":"All competitions", "收藏赛事":"Favourite competitions", "联赛赛事":"League competitions",
    "杯赛赛事":"Cup competitions", "国家队赛事":"International competitions", "已隐藏赛事":"Hidden competitions",
    "尚未收藏赛事":"No favourite competitions", "当前没有赛程":"No fixtures", "暂无历史盘口":"No historical markets",
    "友谊赛事":"Friendly fixtures", "冠军盘":"Outright markets", "全部":"All", "隐藏":"Hide", "恢复":"Restore",
    "隐藏该赛事":"Hide competition", "恢复该赛事":"Restore competition", "收藏":"Add to favourites", "取消收藏":"Remove from favourites",
    "游戏时间":"Game time", "游戏已启动":"Game started", "等待连接":"Waiting for connection", "等待盘口":"Waiting for markets",
    "缓存盘口待核对":"Cached markets need review", "已是最新":"Up to date", "没有符合条件的比赛":"No matching fixtures",
    "请先从左侧选择具体赛事":"Select a competition from the left", "未来":"Next", "场":"matches", "项":"items", "天":"days",
    "请选择赛事":"Select a competition", "从左侧展开分类并选择具体赛事":"Expand a category on the left and select a competition.",
    "快速刷新":"Quick refresh", "全量刷新":"Full refresh", "结算赛果":"Settle results", "90分钟盘口":"90-minute markets",
    "投注单":"Bet slip", "项选择":"selections", "0 项选择":"0 selections", "投注方式":"Bet type", "复式":"System", "复用上一轮投注":"Reuse previous bet",
    "过关方式":"Combination method", "等待选择":"Waiting for selections", "总注额":"Total stake", "预计返还":"Potential return",
    "注额":"Stake", "输入注额":"Enter stake", "注额快捷输入":"Quick stake input", "确认下注":"Place bet",
    "本赛季90分钟赛果":"This season's 90-minute results", "选择日期":"Choose date", "删除已结算":"Delete settled",
    "收益分析":"Profit analysis", "列表视图":"List view", "切换为列表视图":"Switch to list view", "切换为卡片视图":"Switch to card view",
    "未结算":"Pending", "已结算":"Settled", "投注历史":"Bet history", "全选":"Select all", "已选 0 笔":"0 selected",
    "确认退款":"Confirm refund", "到账 £0.00":"Return £0.00", "开赛前退款收取投注额 1% 手续费；赛后满 3 天可全额退款（串关以最后一场比赛、冠军投注以结算日为准）":"Refunds before kick-off incur a 1% fee. After three days, settled bets can be refunded in full.",
    "积分将增加到当前玩家球队":"Points will be added to the current managed club", "积分将增加到当前玩家球队":"Points will be added to the current managed club",
    "购物车":"Cart", "今日添加的物品":"Items added today", "关闭购物车":"Close cart", "商品数量":"Items", "合计":"Total", "确认购买":"Confirm purchase",
    "汽车奖池":"Car prize pool", "抽奖次数":"Draw tickets", "购买抽奖次数":"Buy draw tickets", "概率详情":"Odds", "抽奖记录":"Draw history", "单抽":"Single draw", "十连抽":"Ten draws", "消耗":"Cost", "当前次数":"Current tickets", "可用资金":"Available funds", "单次价格":"Unit price", "待配置":"Not configured",
    "未使用":"Unused", "使用中":"In use", "物品状态":"Item status", "银行与钱包统一流水":"Unified bank and wallet ledger", "充值":"Deposit", "提现":"Withdraw", "账单中心":"Statement centre", "资金操作":"Manage funds",
    "活动中心":"Activity centre", "亲密度排行":"Intimacy ranking", "亲密度排行榜":"Intimacy ranking", "当前经理与执教队伍":"Current manager and managed team",
    "俱乐部医疗中心":"Club medical centre", "默认治疗":"Default treatment", "手动选择":"Manual", "理疗师治疗":"Physio treatment", "保守治疗":"Conservative treatment", "激进治疗":"Aggressive treatment",
    "训练场":"Training ground", "训练档案":"Training archive", "可用器材":"Available equipment", "器材商店":"Equipment shop", "器材仓库":"Equipment storage", "场地尚未布置":"No facilities placed", "一键全部转动":"Rotate all", "刷新队伍":"Refresh team", "刷新关系":"Refresh relationships", "训练协作分析":"Training collaboration",
    "世界国家":"World nations", "国家资料":"Nation details", "世界俱乐部":"World clubs", "世界球员":"World players", "扫描俱乐部":"Scan clubs", "扫描世界数据":"Scan world data", "队伍":"Team", "人员名单":"People",
    "球员":"Players", "职员":"Staff", "详细资料":"Details", "编辑球员":"Edit player", "选择球员":"Select player", "选择队伍、球员和指定比赛":"Select a team, player and fixture",
    "俱乐部资料":"Club details", "俱乐部名人堂":"Club hall of fame", "集团工具":"Group tools", "集团名称":"Group name", "修改集团名称":"Rename group", "修改俱乐部简称":"Edit club short name", "修改球场名称":"Rename stadium", "新球场名称":"New stadium name", "新简称":"New short name", "球场资料":"Stadium details",
    "取消":"Cancel", "确定":"OK", "确认":"Confirm", "关闭":"Close", "最大":"Max", "创建计划":"Create plan", "保存修改":"Save changes", "保存投注上限":"Save betting limits", "确认并完整刷新":"Confirm and full refresh", "确认改名":"Confirm rename", "确认更改":"Confirm change", "确认更换":"Confirm replacement", "确认增加积分":"Confirm points", "确认转会":"Confirm transfer", "确认借款":"Confirm loan", "确认使用":"Use item", "确认删除":"Confirm deletion", "国籍":"Nationality", "全部国籍":"All nationalities", "年龄范围":"Age range", "CA 范围":"CA range", "PA 范围":"PA range", "最低":"Min", "最高":"Max", "至":"to", "筛选":"Filter", "重置":"Reset",
    "选择目标":"Choose target", "选择目标语言":"Choose target language", "搜索语言":"Search languages", "搜索主教练":"Search managers", "搜索球员":"Search players", "目标俱乐部":"Target club", "转会球员":"Transfer player", "更换主教练":"Replace manager", "删除物品":"Delete item", "删除数量":"Quantity to delete", "请选择要删除的数量":"Choose a quantity to delete",
    "训练档案":"Training archive", "基础训练":"Basic training", "教练进修":"Coaching development", "综合训练":"Comprehensive training", "修改房间名称":"Rename room", "房间名称":"Room name", "输入自定义房间名称":"Enter a custom room name", "留空保存可恢复默认名称":"Leave blank to restore the default name",
    "使用联赛积分 +1":"Use league points +1", "使用数量":"Quantity", "借款确认":"Confirm loan", "借款金额":"Loan amount", "我已阅读并接受以上游戏内条款":"I have read and accept the in-game terms", "发生了什么？":"What happened?",
    "选择需要提升声望的目标":"Choose a reputation target", "使用宣传册":"Use brochure", "自定义小妖":"Custom youth player", "计划人数":"Number planned", "PA 档位":"PA tier", "主位置 · 20":"Primary position · 20", "副位置 · 12":"Secondary position · 12", "前锋（ST）":"Striker (ST)", "门将（GK）":"Goalkeeper (GK)", "左后卫（DL）":"Left back (DL)", "中后卫（DC）":"Centre back (DC)", "右后卫（DR）":"Right back (DR)", "防守型中场（DM）":"Defensive midfielder (DM)", "中场（MC）":"Central midfielder (MC)", "前腰（AMC）":"Attacking midfielder (AMC)", "左边锋（AML）":"Left winger (AML)", "右边锋（AMR）":"Right winger (AMR)", "左翼卫（WBL）":"Left wing-back (WBL)", "右翼卫（WBR）":"Right wing-back (WBR)", "清道夫（SW）":"Sweeper (SW)", "左中场（ML）":"Left midfielder (ML)", "右中场（MR）":"Right midfielder (MR)",
    "选择一个应用，开始你的俱乐部工作。":"Choose an app to start managing your club.", "当前连接":"Current connection", "读取期间请停留在已加载的游戏存档中。":"Stay in the loaded game save while it is being read.", "正在读取当前存档、赛程和队伍资料":"Reading the current save, fixtures and teams", "正在汇总全部存档":"Summarising all saves",
  });

  function buildMessageCatalog(localeName) {
    const catalog = Object.assign(
      {},
      messages[localeName] || {},
      localeName === "ko-KR" ? (window.FMODDKoreanCatalog?.messages || {}) : {},
    );
    externalModules.forEach((entry) => Object.assign(catalog, entry.messages?.[localeName] || {}));
    Object.assign(catalog, localePacks[localeName]?.messages || {});
    return Object.freeze(catalog);
  }

  const messageCatalogs = Object.freeze(Object.fromEntries(
    SUPPORTED_LOCALES.map((localeName) => [localeName, buildMessageCatalog(localeName)]),
  ));

  function storedLocale() {
    try {
      const value = localStorage.getItem(STORAGE_KEY);
      return SUPPORTED_SET.has(value) ? value : DEFAULT_LOCALE;
    } catch (_error) {
      return DEFAULT_LOCALE;
    }
  }

  let locale = storedLocale();

  function koreanEnding(value) {
    const text = String(value ?? "")
      .replace(/<[^>]*>/g, "")
      .replace(/&[A-Za-z0-9#]+;/g, "")
      .trim();
    const character = [...text].reverse().find((entry) => /[가-힣A-Za-z0-9]/.test(entry));
    if (!character) return {batchim:false, rieul:false};
    const code = character.codePointAt(0);
    if (code >= 0xAC00 && code <= 0xD7A3) {
      const jongseong = (code - 0xAC00) % 28;
      return {batchim:jongseong !== 0, rieul:jongseong === 8};
    }
    if (/\d/.test(character)) {
      const batchimDigits = new Set(["0", "1", "3", "6", "7", "8"]);
      return {batchim:batchimDigits.has(character), rieul:["1", "7", "8"].includes(character)};
    }
    return {batchim:false, rieul:false};
  }

  function koreanParticle(value, pattern) {
    const ending = koreanEnding(value);
    const particles = {
      "을(를)": ending.batchim ? "을" : "를",
      "이(가)": ending.batchim ? "이" : "가",
      "은(는)": ending.batchim ? "은" : "는",
      "과(와)": ending.batchim ? "과" : "와",
      "(으)로": ending.batchim && !ending.rieul ? "으로" : "로",
    };
    return particles[pattern] || pattern;
  }

  function interpolate(template, parameters = {}) {
    const source = locale === "ko-KR"
      ? String(template).replace(
          /\{([A-Za-z0-9_]+)\}([”"'’』」】»]*)(을\(를\)|이\(가\)|은\(는\)|과\(와\)|\(으\)로)/g,
          (match, key, punctuation, particle) => Object.prototype.hasOwnProperty.call(parameters, key)
            ? `${String(parameters[key])}${punctuation}${koreanParticle(parameters[key], particle)}` : match,
        )
      : String(template);
    return source.replace(/\{([A-Za-z0-9_]+)\}/g, (match, key) => (
      Object.prototype.hasOwnProperty.call(parameters, key) ? String(parameters[key]) : match
    ));
  }

  function t(key, parameters = {}) {
    const catalog = messageCatalogs[locale] || {};
    const fallback = messageCatalogs[DEFAULT_LOCALE] || {};
    const keepEnglishKicker = ENGLISH_KICKER_LOCALES.has(locale) && ENGLISH_KICKER_KEY.test(key);
    const template = keepEnglishKicker
      ? (fallback[key] ?? catalog[key] ?? key)
      : (catalog[key] ?? fallback[key] ?? key);
    return interpolate(template, parameters);
  }

  function tp(key, count, parameters = {}) {
    const rule = new Intl.PluralRules(locale).select(Number(count));
    const values = {...parameters, count};
    const resolved = t(`${key}.${rule}`, values);
    return resolved === `${key}.${rule}` ? t(`${key}.other`, values) : resolved;
  }

  function formatNumber(value, options = {}) {
    return new Intl.NumberFormat(locale, options).format(Number(value));
  }

  function formatDate(value, options = {}) {
    const candidate = value instanceof Date ? value : new Date(value);
    return Number.isNaN(candidate.getTime()) ? String(value ?? "") : new Intl.DateTimeFormat(locale, options).format(candidate);
  }

  function compare(left, right, options = {}) {
    return new Intl.Collator(locale, options).compare(String(left ?? ""), String(right ?? ""));
  }

  function catalogKeys(localeName = locale) {
    const resolved = SUPPORTED_SET.has(localeName) ? localeName : DEFAULT_LOCALE;
    return Object.freeze(Object.keys(messageCatalogs[resolved] || {}).sort());
  }

  function isStaticKeyedText(key, value) {
    const current = String(value ?? "");
    let candidates = keyedStaticTextValues.get(key);
    if (!candidates) {
      candidates = Object.freeze([
        key,
        ...new Set(SUPPORTED_LOCALES.map((localeName) => (
          messageCatalogs[localeName]?.[key]
        )).filter((candidate) => typeof candidate === "string")),
      ]);
      keyedStaticTextValues.set(key, candidates);
    }
    return candidates.some((candidate) => (
      current === candidate || current === translateLegacy(candidate)
    ));
  }

  function translateLegacy(value) {
    const text = String(value ?? "");
    if (locale === SOURCE_LOCALE || !text) return text;
    const isPtBR = locale === "pt-BR";
    const isPtPT = locale === "pt-PT";
    const ptCount = (count, singular, plural) => `${count} ${Number(count) === 1 ? singular : plural}`;
    const catalogs = [
      localePacks[locale]?.legacy || {},
      locale === "ko-KR" ? (window.FMODDKoreanCatalog?.legacy || {}) : {},
      ...externalModules.map((entry) => entry.legacy?.[locale] || {}),
    ];
    for (const catalog of catalogs) {
      if (Object.prototype.hasOwnProperty.call(catalog, text)) return catalog[text];
    }
    if (Object.prototype.hasOwnProperty.call(legacyEnglish, text)) return legacyEnglish[text];
    let match = text.match(/^游戏日期\s+(.+)$/);
    if (match) return t("home.game_date", {date:match[1]});
    match = text.match(/^共\s+(\d+)\s+场$/);
    if (match) return tp("home.matches_played", Number(match[1]));
    match = text.match(/^(\d+)\s*场$/);
    if (match) return locale === "ko-KR" ? `${match[1]}경기` : isPtBR ? ptCount(match[1], "partida", "partidas") : isPtPT ? ptCount(match[1], "jogo", "jogos") : `${match[1]} match${Number(match[1]) === 1 ? "" : "es"}`;
    match = text.match(/^(\d+)\s*项$/);
    if (match) return locale === "ko-KR" ? `${match[1]}개` : (isPtBR || isPtPT) ? ptCount(match[1], "item", "itens") : `${match[1]} item${Number(match[1]) === 1 ? "" : "s"}`;
    match = text.match(/^(\d+)\s*天$/);
    if (match) return locale === "ko-KR" ? `${match[1]}일` : (isPtBR || isPtPT) ? ptCount(match[1], "dia", "dias") : `${match[1]} day${Number(match[1]) === 1 ? "" : "s"}`;
    match = text.match(/^(\d+)\s*项选择$/);
    if (match) return locale === "ko-KR" ? `${match[1]}개 선택` : (isPtBR || isPtPT) ? ptCount(match[1], "seleção", "seleções") : `${match[1]} selection${Number(match[1]) === 1 ? "" : "s"}`;
    match = text.match(/^已选\s*(\d+)\s*笔$/);
    if (match) return locale === "ko-KR" ? `${match[1]}개 선택됨` : isPtBR ? `${match[1]} selecionadas` : isPtPT ? `${match[1]} selecionadas` : `${match[1]} selected`;
    match = text.match(/^(\d+)个$/);
    if (match) return `${match[1]}`;
    match = text.match(/^正在刷新\s+(\d+)%$/);
    if (match) return locale === "ko-KR" ? `새로 고치는 중 ${match[1]}%` : isPtBR ? `Atualizando ${match[1]}%` : isPtPT ? `A atualizar ${match[1]}%` : `Refreshing ${match[1]}%`;
    match = text.match(/^游戏时间\s+(.+)$/);
    if (match) return locale === "ko-KR" ? `게임 시간 ${match[1]}` : (isPtBR || isPtPT) ? `Tempo de jogo ${match[1]}` : `Game time ${match[1]}`;
    match = text.match(/^未来(\d+)天\s*[·・]\s*(\d+)\s*场$/);
    if (match) return locale === "ko-KR" ? `향후 ${match[1]}일 · ${match[2]}경기` : isPtBR ? `Próximos ${match[1]} dias · ${ptCount(match[2], "partida", "partidas")}` : isPtPT ? `Próximos ${match[1]} dias · ${ptCount(match[2], "jogo", "jogos")}` : `Next ${match[1]} days · ${match[2]} match${Number(match[2]) === 1 ? "" : "es"}`;
    match = text.match(/^之前\s*[·・]\s*(\d+)\s*场$/);
    if (match) return locale === "ko-KR" ? `이전 · ${match[1]}경기` : isPtBR ? `Anteriores · ${ptCount(match[1], "partida", "partidas")}` : isPtPT ? `Anteriores · ${ptCount(match[1], "jogo", "jogos")}` : `Previous · ${match[1]} match${Number(match[1]) === 1 ? "" : "es"}`;
    match = text.match(/^已是最新\s*[·・]\s*已结算(\d+)个注单$/);
    if (match) return locale === "ko-KR" ? `최신 상태 · 베팅 ${match[1]}건 정산됨` : isPtBR ? `Atualizado · ${ptCount(match[1], "aposta liquidada", "apostas liquidadas")}` : isPtPT ? `Atualizado · ${ptCount(match[1], "aposta liquidada", "apostas liquidadas")}` : `Up to date · ${match[1]} bet${Number(match[1]) === 1 ? "" : "s"} settled`;
    match = text.match(/^钱包，余额\s+(.+)$/);
    if (match) return locale === "ko-KR" ? `지갑, 잔액 ${match[1]}` : (isPtBR || isPtPT) ? `Carteira, saldo ${match[1]}` : `Wallet, balance ${match[1]}`;
    return text;
  }

  function translateTextNode(node) {
    const raw = node.nodeValue || "";
    const match = raw.match(/^(\s*)([\s\S]*?)(\s*)$/);
    const current = match?.[2] || "";
    const previous = sourceTextNodes.get(node);
    const source = previous && current === previous.translated ? previous.source : current;
    const translated = translateLegacy(source);
    sourceTextNodes.set(node, {source, translated});
    if (current !== translated) node.nodeValue = `${match[1]}${translated}${match[3]}`;
  }

  function updatePageHeadingSemantics(root) {
    const pages = root.matches?.(".app-page") ? [root] : [];
    pages.push(...root.querySelectorAll?.(".app-page") || []);
    pages.forEach((page) => {
      const header = page.querySelector(":scope > header");
      const eyebrow = header?.querySelector("small");
      const title = header?.querySelector("h1");
      if (!eyebrow || !title) return;
      const normalize = (value) => String(value || "")
        .normalize("NFKC").toLocaleLowerCase(locale).replace(/[\s\p{P}\p{S}]+/gu, "");
      const eyebrowText = normalize(eyebrow.textContent);
      eyebrow.classList.toggle(
        "i18n-duplicate-eyebrow",
        Boolean(eyebrowText && eyebrowText === normalize(title.textContent)),
      );
    });
  }

  function apply(root) {
    if (!root) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach((node) => {
      const parent = node.parentElement;
      if (!parent || ["SCRIPT", "STYLE", "TEXTAREA"].includes(parent.tagName)) return;
      translateTextNode(node);
    });
    const elements = root.matches?.("[aria-label],[placeholder],[title]") ? [root] : [];
    elements.push(...root.querySelectorAll?.("[aria-label],[placeholder],[title]") || []);
    elements.forEach((element) => {
      ["aria-label", "placeholder", "title"].forEach((attribute) => {
        if (!element.hasAttribute(attribute)) return;
        const current = element.getAttribute(attribute);
        const records = sourceAttributes.get(element) || {};
        const previous = records[attribute];
        const source = previous && current === previous.translated ? previous.source : current;
        const translated = translateLegacy(source);
        records[attribute] = {source, translated};
        sourceAttributes.set(element, records);
        if (current !== translated) element.setAttribute(attribute, translated);
      });
    });
    const keyed = root.matches?.("[data-i18n]") ? [root] : [];
    keyed.push(...root.querySelectorAll?.("[data-i18n]") || []);
    keyed.forEach((element) => {
      const key = element.dataset.i18n;
      if (!isStaticKeyedText(key, element.textContent)) return;
      const translated = t(key);
      if (element.textContent !== translated) element.textContent = translated;
    });
    const keyedLabels = root.matches?.("[data-i18n-aria-label]") ? [root] : [];
    keyedLabels.push(...root.querySelectorAll?.("[data-i18n-aria-label]") || []);
    keyedLabels.forEach((element) => {
      const translated = t(element.dataset.i18nAriaLabel);
      if (element.getAttribute("aria-label") !== translated) element.setAttribute("aria-label", translated);
    });
    [
      ["placeholder", "i18nPlaceholder"],
      ["title", "i18nTitle"],
      ["alt", "i18nAlt"],
    ].forEach(([attribute, datasetKey]) => {
      const selector = `[data-i18n-${attribute}]`;
      const candidates = root.matches?.(selector) ? [root] : [];
      candidates.push(...root.querySelectorAll?.(selector) || []);
      candidates.forEach((element) => {
        const translated = t(element.dataset[datasetKey]);
        if (element.getAttribute(attribute) !== translated) element.setAttribute(attribute, translated);
      });
    });
    updatePageHeadingSemantics(root);
  }

  function applyShell() {
    ["#main-sidebar", ".topbar", "#page-home", "#settings-dialog", "#usage-notice-dialog"]
      .map((selector) => document.querySelector(selector))
      .filter(Boolean)
      .forEach(apply);
  }

  let translationObserver = null;
  let observerFlushScheduled = false;
  const observerRoots = new Set();

  function observe(root = document.body) {
    if (!root || typeof MutationObserver === "undefined") return false;
    translationObserver?.disconnect();
    translationObserver = new MutationObserver((records) => {
      records.forEach((record) => {
        if (record.type === "characterData") {
          const parent = record.target.parentElement;
          if (parent && !parent.closest("[data-i18n]")) observerRoots.add(parent);
          return;
        }
        if (record.type === "attributes") {
          if (record.target instanceof Element) observerRoots.add(record.target);
          return;
        }
        record.addedNodes?.forEach((node) => {
          if (node.nodeType === Node.ELEMENT_NODE) observerRoots.add(node);
          else if (node.parentElement && !node.parentElement.closest("[data-i18n]")) {
            observerRoots.add(node.parentElement);
          }
        });
      });
      if (observerFlushScheduled || !observerRoots.size) return;
      observerFlushScheduled = true;
      queueMicrotask(() => {
        observerFlushScheduled = false;
        const pending = [...observerRoots];
        observerRoots.clear();
        pending.filter((candidate) => candidate?.isConnected).forEach(apply);
      });
    });
    translationObserver.observe(root, {
      subtree:true,
      childList:true,
      characterData:true,
      attributes:true,
      attributeFilter:["aria-label", "placeholder", "title", "alt", "data-i18n"],
    });
    return true;
  }

  function persistBootLocale(value) {
    if (!SUPPORTED_SET.has(value)) return false;
    try {
      localStorage.setItem(STORAGE_KEY, value);
      return localStorage.getItem(STORAGE_KEY) === value;
    } catch (_error) {
      return false;
    }
  }

  function setLocale(value, {persist = true, root = document.body} = {}) {
    if (!SUPPORTED_SET.has(value)) return false;
    if (persist && !persistBootLocale(value)) return false;
    locale = value;
    document.documentElement.lang = locale;
    document.documentElement.dataset.locale = locale;
    if (root) apply(root);
    document.dispatchEvent(new CustomEvent("fmodd:localechange", {detail:{locale}}));
    return true;
  }

  function syncAuthoritativeLocale(value) {
    if (!SUPPORTED_SET.has(value)) return false;
    if (value === locale) return persistBootLocale(value);
    return setLocale(value);
  }

  document.documentElement.lang = locale;
  document.documentElement.dataset.locale = locale;

  window.FMODDI18n = Object.freeze({
    STORAGE_KEY, DEFAULT_LOCALE, SOURCE_LOCALE, SUPPORTED_LOCALES,
    get locale() { return locale; },
    t, tp, formatNumber, formatDate, compare, catalogKeys,
    translateLegacy, apply, applyShell, observe, updatePageHeadingSemantics,
    persistBootLocale, setLocale, syncAuthoritativeLocale,
  });
  apply(document.body);
  observe(document.body);
})();
