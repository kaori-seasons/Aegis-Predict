# v0.12 实际经营信号、新品冷启动与公开市场数据

交付日期：2026-11-20。对应落地方案 F08 / E10：独立经营指标契约、滞后特征、规则观察池和按上新实体分组的离线评估。v5 保持显式候选；默认生产 v2、既有 v3/v4 数值路径及 Amazon/TikTok Mock 保留。

## 1. 能力与使用边界

| 交付项 | 当前实现 | 生产使用条件 |
| --- | --- | --- |
| 经营数据 | `commerce_performance` CSV/XLSX、可配置 REST、`mock_commerce` | 核实渠道、当地日、单位、币种、退款/税费口径并版本化；不把平台流量当作库存销量 |
| 上新事件 | `sku_launches` 导入和工作台登记/撤销、修订 CAS | 同款变体、多仓使用同一个 `launch_group`，`cohort_key` 在事件中保存，不回填今日类目 |
| 公开数据 | Frankfurter、Wikimedia、GOV.UK 固定 HTTPS 只读接口及同结构 Mock | 无密钥；显式订阅和 SKU/仓映射；不保证可用性或预测收益 |
| 独立预测 | `commerce-coldstart-v5` 与 mean28、v3、策略模型、当前冠军同任务运行 | 先在「经营信号与新品」选择来源和渠道，再在「预测与模型」选择 v5 |
| 回测 | 存量滚动起点 + 上新实体分组两轨、均值配对对比、消融验证 | 冻结需求快照的离线研究不能替代真实前向实绩 |
| 执行 | 沿用独立前向实绩、分位校准、异人晋级和保护期门禁；新增来源 Mock 检查 | 新品同龄先验仍带执行阻止标记；需要独立新品前向验收后另行放开 |

**尚未声称完成** Amazon Business Reports、广告报表、TikTok 流量/广告报表的真实授权、异步报表下载及逐市场口径验收。现有订单/库存连接器保留；本期经营报表先走明确契约的 CSV/REST 与 Mock。真实商家尚未积累本期新增数据的长期时点历史，因此没有真实商家收益结论。

## 2. 数据契约与知识时间

新增迁移 `0008_commerce_signals`，四张租户表：

- `commerce_performance_revision`：商品、仓、渠道、指标、业务日、契约版本、修订。
- `sku_launch_revision`：商品、渠道、计划上新/首次成交、实体群组、同类新品群组、修订。
- `public_signal_revision`：提供方、序列、指标、日期、修订、原始署名和发布时间证据。
- `public_signal_binding_revision`：来源内 SKU/仓与公开序列的映射、生效日、撤销修订。

四表有 RLS、复合租户外键、不可变修订和数据库事实历史触发器。历史重放只使用检查点中已提交且可见的事实，禁止借用当前数据库行。`received_at` 来自同租户原始记录；`available_at` 由数据库写入实际时钟，客户端不能倒填。导入中出现服务端时钟字段进入隔离队列，格式错误也隔离而不使整个规范化任务崩溃。

每次取特征先限制 `published_at / received_at / available_at <= issue`，然后按业务身份选择最新修订，最后应用撤销。迟到的高版本和撤销不会抹去较早时点的合法版本。原始记录、连接来源、修订 ID、特征摘要、代码摘要随独立预测冻结。

经营指标：`sessions / orders / units` 为无币种整数；`ad_spend / revenue / unit_price` 为显式三位大写币种金额。数据状态为 `observed / missing / censored`，观察值必须有数值，缺失与删失不转换成零。每天的结束时刻由仓库 IANA 时区计算，未完成当地日不能作为实际指标入模。一个契约版本应固定毛/净额、税费、退款、归因窗口和币种定义；发生变化要改版本，不悄悄拼接。

公开 API 未提供可验证的历史发布时间/系统接收时间，本实现统一 `publication_evidence=first_seen`，数据库强制将发布时间设为实际首次接收时刻。今天下载的去年数值不会用于去年的预测起点；上游后续更改数值会生成新修订，完全相同的值不新增修订。手工映射同样从实际记录时刻才可见，即使 `effective_from` 填写过去日期。映射内容和版本进入预测范围摘要；映射变更后，旧预测和待执行计划必须重新生成及审核。

## 3. 滞后模型与回退

v5 生成 1 / 7 / 28 天经营指标均值、覆盖率，以及同渠道同日匹配的订单/会话比。会话为零时比例留空；该比例并非跨平台可直接比较的转化概率。不同币种不在同一滞后窗口混合求均值。

公开汇率和页面热度也只取已结束且已知的滞后日。未来实际汇率、页面热度、广告支出、订单量均不填入预测。已知未来节假日可以作为日历变量；它不是预设的销量倍数。一个 SKU/仓存在同指标多序列有效映射时留空并记录歧义，不自动平均。

存量候选使用带固定正则的线性模型，训练集内拟合中位数填补、缺失指示和标准化。预测按签发时的实际滞后状态、目标星期和步长生成；未来步使用的是签发时的滞后摘要，不冒充未来已实现的指标。最少112天需求日历、足够的时点特征/完整训练标签；固定训练窗口和计算预算。

模型采用三个训练后验证起点，训练标签截止于第一个验证起点前，清除跨界目标窗口。与 v3 及**相同回归器移除经营/公开特征的消融版本**比较，平均 MAE 至少改善2%，并在至少2/3折优于 v3 才启用。门禁未通过、特征缺失或历史不足时精确使用 v3 每日轨迹。直接模型最多预测28天，之后明确接回 v3，不外推未知经营信号。多个验证窗口可能重叠，折数不被当成独立统计显著性；真实晋级另用已有独立前向门禁。

当前冻结需求快照可能包含后来发生的需求修订，内部历史选型属于签发时合法使用过去数据的训练步骤；它不能被描述成那些历史日期真实发行过的预测。生产收益以真实签发后积累的成熟实绩为准。

## 4. 新品观察池与同龄先验

生命周期按当前签发时已知事件计算：未知上新、待上新、待首次成交、0–6天观察、7–27天孵化、28天后常规预测。页面保留事件、修订、可用时间和依据，不输出未经校准的爆款概率。

同龄先验仅使用同租户、同仓、事件中相同 `cohort_key` 的其他上新实体。目标商品所在 `launch_group` 从所有变体和多仓供体中排除；回测额外传入整个测试实体黑名单。每个预测年龄都必须有完整、未缺失、未删失且在起点前成熟的供体需求，至少3个不同实体。先在实体内取中位数，再在实体间取中位数，避免多变体、多仓重复行获得额外权重。

供体不足时回退并明确标记原因。存在自己的首单后历史时，仅允许最多50%的有限收缩；上新前为零，计划日期属于显式业务假设。所有预测记录供体群组、事件、目标年龄、排除实体和原因。该先验目前仍带 `cold_start_requires_separate_forward_validation` 或支持不足标记，真实补货执行会拒绝，不能因存量预测通过而解除新品门禁。

## 5. 免费公开接口

| 来源 | 固定接口与意义 | 限制和许可 |
| --- | --- | --- |
| [Frankfurter](https://frankfurter.dev/) | V2 blended reference rates；`USD/EUR` | 官方说明 blended 接口免费商用，无密钥；不默认使用许可不同的指定提供方接口。参考汇率不是成交价 |
| [Wikimedia Pageviews](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/reference/page-views.html) | 每文章每日 `all-access/user`；`en.wikipedia.org\|USB-C` | [访问政策](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/documentation/access-policy.html)：CC0、标识 User-Agent、合理请求；话题关注度不是店铺流量 |
| [GOV.UK Bank Holidays](https://www.gov.uk/bank-holidays) | [完整 JSON](https://www.gov.uk/bank-holidays.json)，英格兰及威尔士、苏格兰、北爱尔兰 | [OGL v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/)，必须保留署名；仅返回数据覆盖的年份，不把未知年份补零 |

GOV.UK 数据署名：Contains public sector information licensed under the Open Government Licence v3.0.

[本次真实只读冒烟](verification/commerce-public-api-smoke.json)三者已通过，数据未写入租户业务库。Wikimedia 请求使用 `.org` 域名，但返回 `project` 通常省略 `.org`；解析器接受这两个严格等价标识，不接受其他项目。

未选 Nager 为 SaaS 默认免费源，因为[当前条款](https://nagerholidays.com/legal/termsofservice)要求商业使用者持续赞助；未采用仅免费非商用的托管接口或非官方抓取作为生产默认。

连接示例（Mock 改用 `mock_public_market`，无需密钥）：

```json
{
  "name": "公开市场上下文",
  "kind": "public_market",
  "config": {
    "subscriptions": [
      {"provider": "frankfurter", "series_key": "USD/EUR"},
      {"provider": "wikimedia", "series_key": "en.wikipedia.org|USB-C"},
      {"provider": "govuk_holidays", "series_key": "england-and-wales"}
    ],
    "history_days": 90,
    "cache_ttl_seconds": 3600
  }
}
```

最多12个订阅、365天观测历史、2MB解压后响应、固定域名且不跟随重定向；顺序请求、每连接限流、429/5xx重试和 Retry-After，成功完成全部订阅后才推进最终游标。缓存间隔300–86400秒，配置变更和显式回填绕过游标缓存。免费服务失败时任务失败并保留证据，不伪造空成功。上游未返回的日期保持未知。

## 6. 工作台与 API

沿用现有白底、细分隔线和管理导航，增加「经营信号与新品」：选择预测契约、登记/撤销事件、公开序列映射、指标和修订预览。数据源管理增加公开接口与两种 Mock，可导入四个新数据流的模板。

- `GET /api/v1/commerce/summary`：租户内最近修订和公开源目录。
- `POST /api/v1/commerce/launches`、`/bindings`：`source_system / expected_revision / record`，新建版本从0开始。服务器设置发布时间，不接受租户/接收/可用时间。按身份加事务锁并检查 CAS，过期编辑返回409。
- 写入需连接管理权限、全局地点范围、Cookie 会话和 CSRF；读权限和租户 RLS 与原工作台一致。写入落原始记录并审计，之后可从检查点复算。
- `POST /api/v1/decision/forecasts`：`models:["commerce-coldstart-v5"]`，自动增加 mean28、v3、策略模型和当前冠军。单任务最多30条 v5 序列；工作进程异步运行，结果仍使用既有实绩评估接口。

预测数据契约要求来源/渠道配对。页面会回显当前设置、保存时保留其他策略；缺渠道会给出可操作说明，服务端422也返回字段原因。不是自动猜测 Amazon/TikTok 渠道来绕过口径校验。

## 7. 无泄漏验证与配对结果

复现命令：

```sh
PYTHONPATH=src .venv/bin/python scripts/benchmark_commerce.py \
  --output docs/artifacts/commerce-benchmark.json --export-dir /tmp/aegis-commerce-inputs

aegis commerce-backtest --path /tmp/aegis-commerce-inputs/public_predictive.json \
  --horizon 7 --max-folds 4 --output /tmp/public-comparison.json
```

[完整机器可读报告](artifacts/commerce-benchmark.json)包含固定合成样本、1/7/28天配对预测、模型选择、供体、特征摘要和跳过原因。**所有知识时间为受控模拟，非真实市场收益；报告始终 `release_eligible=false`，不进入晋级证据池。**

| 固定场景 / 周期 | 样本 | mean28 MAE | v3 MAE | v5 MAE | v5相对均值改善 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 有效经营滞后 / 1天 | 4 | 13.321 | 13.321 | 1.733 | 86.99% |
| 有效经营滞后 / 7天 | 4 | 28.062 | 29.375 | 18.853 | 32.82% |
| 有效经营滞后 / 28天 | 3 | 20.000 | 22.420 | 22.420 | **-12.10%** |
| 有效公开热度滞后 / 1天 | 4 | 13.321 | 13.321 | 1.733 | 86.99% |
| 有效公开热度滞后 / 7天 | 4 | 28.062 | 29.375 | 18.853 | 32.82% |
| 无关公开信号 / 7天 | 4 | 0 | 0 | 0 | 不定义（分母零） |
| 无信号 / 7天 | 4 | 24.500 | 24.500 | 24.500 | 0% |
| 新品实体隔离 / 7天 | 20 | 107.741 | 107.741 | 63.766 | 40.82% |

28天结果未启用外部回归、使用 v3 仍落后均值，说明门禁不能保证所有真实外层窗口优于均值，因此不默认推广。新品20个窗口来自5个上新实体，不是20个独立商品；其中只有8个窗口满足至少3个成熟供体群组，其他窗口回退。无关信号组未启用外部模型；1天相对均值的收益完全来自保留的 v3，不能归因于公开数据。

自动测试覆盖：各知识时钟晚到、未来指标/未来需求污染不改变此前预测、最新修订筛选顺序、撤销、零会话分母、缺失与删失、公开映射晚到、实体全局隔离与供体不足；数据库时间、RLS、不可变修订、批次隔离、公开源重试/响应预算、真实预测队列和策略表单校验。

## 8. 部署与后续验收

先备份数据库，升级迁移并重新授权应用角色：`aegis db upgrade`、管理员运行 `aegis db grant`；API/forecast-worker/sync-worker/scheduler 使用同一0.12镜像。迁移只是新增表；回退应用时保留四表和审计事实，不在有业务数据时直接 downgrade 删除表。仅改变界面静态文件而不重启旧 API 进程可能造成新字段422，部署必须同时更新 API 与 Worker。

先以 Mock/CSV 验证主数据、时间和渠道契约，再开启真实公开只读连接并显式映射。持续前向积累经营数据、上新实体事件、公开首见修订和真实需求；检查覆盖率和迟到分布。随后运行既有独立预测评分、校准和异人晋级流程。新品真实执行、跨店/跨仓同类迁移、树模型、爆款概率、60/90天外生预测及平台分析报表专用权限均需单独试点验收。

最终验证：

| 检查 | 结果与证据 |
| --- | --- |
| PostgreSQL 16 全量测试 | **198 passed**，1条既有 Starlette/httpx 弃用提示；[测试记录](verification/commerce-tests.json)，包含迁移往返、元数据漂移、RLS、不可变修订、三时钟与执行门禁 |
| 公开接口真实只读请求 | Frankfurter、Wikimedia、GOV.UK 全部通过；[接口证据](verification/commerce-public-api-smoke.json) |
| 浏览器端到端 | Google Mock 认证、两种新 Mock 连接/同步、契约校验和回显、上新登记、公开映射、v5/mean28/v3 独立任务与冻结证据；60个未成熟窗口排除，不伪造收益；[浏览器证据](verification/commerce-browser.json) |
| Linux ARM64 / AMD64 | 两种最终镜像均通过隔离 PostgreSQL 17 迁移、健康检查、Google默认/平台Mock、UID10001、只读根、wheel摘要、v3/v4回测、保护期内核与v5实际回归拟合；[ARM64](verification/commerce-linux-arm64.json) / [AMD64](verification/commerce-linux-amd64.json)，源码摘要与当前工作区一致 |
| 静态检查 | 两个前端脚本 Node 语法、新文件 Ruff F/I、`git diff --check` 通过 |

镜像验证支持 `--postgres-image` 显式指定本地或固定数据库镜像，本次使用已缓存的 PostgreSQL 17；宿主全量回归使用 PostgreSQL 16。两者均为隔离合成环境，不是生产账户验收。
