# P0 首轮实施、数据契约与真实试点接入手册

> 历史基线：本文记录0.5.0首轮交付。Google认证、平台Mock、累计执行/结算与Linux双架构镜像的最新实现见[0.6.0闭环手册](16-production-closure-and-auth.md)。

- 更新：2026-11-16，Asia/Shanghai
- 软件版本：0.5.0，数据库版本：`0003_foundation`
- 上游方案：[企业级评估与落地方案](13-enterprise-readiness-and-delivery-plan.md)
- 本轮目标：修复测试基线，先关闭会污染试点数据的明显路径，建立可验证的来源、批次与库存基础。
- 当前范围：内部验证与只读影子；**尚未批准真实外部执行或逐市场生产上线**。

## 1. 本轮交付与明确限制

| 项目 | 已实施 | 当前限制 |
| --- | --- | --- |
| 失败测试 | 首页检查跟随 AEGIS-PREDICT 品牌，同时检查登录入口、脚本和健康接口 | 不以删除断言绕过页面验证 |
| 批次完整性 | landing→landed→complete/quarantined/failed；记录成员、数量和内容摘要；只有完整落地的批次能规范化 | 尚非大规模对象存储原始证据/供应商认证平台 |
| 游标安全 | 一个流读取失败不推进最终游标；重试复用已落地 raw，并关联到新的完整批次 | 取舍为失败时重拉整个流窗口，优先保证不丢数据 |
| 数据来源 | 按租户、仓、领域、渠道显式批准权威来源；通配总量与分渠道规则不能重叠，数据库亦设约束 | 未实现主数据领域的完整 MDM、集团组织和有效期历史 |
| 有效订单 | Amazon/TikTok 状态白名单；未支付/不可履约不计入接受需求；未知状态隔离 | 不把退款直接当作实物回库；独立退货事实、组合行删除/替代仍待完善 |
| 需求事实 | 按来源×渠道×日期保存；规划按权威规则选取；保留导入总量但不重复加到分渠道销量 | 旧 `daily_demand` 保留兼容投影，不再作为生产规划权威来源 |
| 缺失与零 | 分开记录 observed/censored/missing；只有有完整时间窗口证据时才能补零 | CSV 日总量必须提供明确的零值行；CSV 订单没有完整窗口证明时不可作为完整日需求依据 |
| 库存完整性 | 同仓一个权威库存来源，规划仅使用该源最新完整批次对应的快照；缺行拒绝规划 | 当前契约是完整快照，不支持把随意增量 CSV 冒充全量库存 |
| 在途去重 | 快照在途逐笔关联 PO 行，要求 SKU/目的仓一致、数量不超额；无法证明重叠时拒绝 | 混合 PO/独立调拨在途若不能完全归属，要整改数据；不能用总量相减猜测 |
| 预占 | 发布事务中按库存键加锁，检查最新源、完整快照和剩余量；幂等预占；并发不超占 | 保留单活动计划保护；尚无执行消耗、对账释放和滚动替代的完整生命周期 |
| 生效保护 | 默认 shadow；执行计划与回写分别默认关闭；发布时重新检查数据和权威规则变化 | 不通过开关直接跳过 G3；当前真实试点保持只读 |
| 演示隔离 | 生产 API 不挂比赛路由；比赛服务在 `AEGIS_ENV=prod` 下拒绝请求/启动 | 比赛前端仍是演示，不是生产工作台 |
| 工程证据 | 迁移往返和结构漂移检查、依赖版本约束、源码/运行时 manifest、CI 更新 | Docker Compose 配置校验通过；本机容器 daemon 未运行，未构建 Linux 镜像；版本约束不等于 wheel 哈希锁或完整 SBOM |

## 2. 市场和账户接入边界

用户要求覆盖主流 Amazon 站点、TikTok 市场及自有 WMS、第三方海外仓、ERP、CSV。因此底座支持多账户和多来源，而不是选择一家供应商的虚构专有 API。

### 2.1 市场目录

`GET /api/v1/integration/markets` 返回可配置市场目录及 `configuration-only` 标识。

- Amazon：23 个站点，覆盖 NA/EU/FE，按官方 marketplace ID 校验端点区域，包括美国、加拿大、墨西哥、巴西，欧洲及中东/印度/非洲站点，以及日本、澳大利亚、新加坡。
- TikTok Shop：20 个市场配置，覆盖美洲、英国/欧洲、东南亚和日本；`UK` 输入统一为 `GB`。
- 每个站点/市场必须分别取得实际商家授权、对应 scope、样例与对账证据；市场目录存在不表示连接器已在该市场联调通过。
- 站点国家不等于仓库时区。美国、加拿大等多时区市场必须由仓库/需求自然日契约指定 IANA 时区，不能自动套一个国家时区。

Amazon 目录与区域取自[Marketplace IDs](https://developer-docs.amazon.com/sp-api/docs/marketplace-ids)及[SP-API Endpoints](https://developer-docs.amazon.com/sp-api/docs/sp-api-endpoints)。TikTok 欧洲扩展取自[官方市场公告](https://newsroom.tiktok.com/tiktok-shop-expands-across-europe?lang=en-150)，其他市场配置结合[官方 Shop 说明](https://ads.tiktok.com/resources/help/article/tiktok-shopping-and-showcase?lang=en)核对。核对日期为2026-11-16；上线前仍须复核目标市场契约。

### 2.2 账户身份

Amazon 连接配置至少包含：

```json
{
  "region": "NA",
  "marketplace_id": "ATVPDKIKX0DER",
  "seller_id": "REAL_SELLER_ID",
  "location_code": "US-FBA",
  "initial_since": "2026-08-01T00:00:00Z"
}
```

TikTok Shop 连接配置至少包含：

```json
{
  "market": "US",
  "shop_id": "REAL_SHOP_ID",
  "shop_cipher": "AUTHORIZED_SHOP_CIPHER",
  "location_code": "US-WMS",
  "initial_since": "2026-08-01T00:00:00Z"
}
```

凭据通过连接 API 的独立 `secrets` 对象加密保存，不写进上述配置、代码、日志或普通文档。同租户的平台账户×市场身份派生稳定来源键，不能换个连接名重复接入；不同租户可各自接入同名账户，数据仍隔离。

TikTok 健康检查核对 shop id、cipher 和市场；Amazon 健康检查核对 marketplace participation。**Amazon seller identity 的 OAuth 回调核验、自助 OAuth、授权吊销运营及各市场真实健康检查证据仍待接通**。配置中的 seller id 不等于已经独立验证的授权身份。

### 2.3 库存系统类型

- 自有 WMS / 第三方海外仓 / ERP：先使用 `generic_rest` 的明确映射契约，或通过各系统导出 CSV/XLSX；专用连接器只有在收到真实接口、签名、配额及响应后实现。
- CSV/XLSX：`file_csv.directory` 必须位于 `<AEGIS_FILE_INBOX_ROOT>/<tenant_uuid>/` 下；不能跨租户读目录或跟随越界文件链接。
- TikTok 当前只接订单需求。平台商品可售量不能直接作为企业实物在库；库存必须从批准的 WMS/ERP/CSV 来源取得。
- Amazon FBA 与第三方仓是不同库存地点，不能因都服务美国市场就把库存相加成同一仓。

## 3. 数据责任人应签署的最小契约

| 事实 | 本轮强制语义 | 需由客户明确的事项 |
| --- | --- | --- |
| 有效需求 | 已接受/可履约的需求，未支付和取消不计；原单据和状态版本保留 | 各系统状态映射、样品/替换、退款和退货的独立口径 |
| `on_hand` | 导入契约中的在库基数，按字段定义只扣一次 allocated/backorder | 原源字段是毛在库还是净可售，是否含冻结/质检；不能混填 |
| `allocated` | 原系统已锁定数量，与本系统 held 预占分别可追溯 | 外部回读反映同一预占时如何对账，不能直接自动释放 |
| `in_transit` | 明确在途数量，不能与 PO 未收量重复计入 | 对应 PO/发运/调拨与收货行，剩余数量及预计到达日 |
| 日需求 | 来源、渠道、当地自然日、观察状态和版本 | 哪些渠道已覆盖，是否全量，缺失日期如何补采 |
| 全量库存 | 一批明确覆盖声明仓的完整快照，零库存显式记录 | 完整范围、business as_of、站点延迟和空快照含义 |
| 权威来源 | 每个仓的库存/入库领域只有一个批准来源；需求可按渠道拆分 | ERP 聚合订单与平台直连谁覆盖谁，不允许隐式累加 |

`source_authority.inbound_mode`：

- `linked`：默认；当 PO 和快照在途同时存在，每个在途单位必须有明确 PO 行关联，且不超过该 PO 未收量。
- `disjoint`：只有业务明确批准“该 PO 未收量与快照在途完全不重叠”时才能使用，必须填写原因与审批人证据。
- 混合、未知或不完整归属不自动推断；先修复数据或扩展单据模型，再进入规划。

规划不再使用旧策略中的 `inventory_source/demand_source/po_source` 全局过滤器。旧 `po_includes_transit` 也不再构成总量防重依据；迁移时应改为按仓/渠道的明确权威规则。

## 4. 本地验证与迁移

安装使用经过测试的版本约束：

```bash
python -m venv .venv
.venv/bin/pip install --constraint requirements/constraints.txt -e '.[dev]'
# 或在已准备的开发环境使用 PYTHONPATH=src .venv/bin/python -m aegis.cli ...
```

配置 owner 与 app 两种数据库角色。API 使用非 owner、非 SUPERUSER、非 BYPASSRLS 的应用角色；启动时同时检查迁移版本。

```bash
aegis db create-role
aegis db upgrade
aegis db grant
```

新增迁移为 `0003_foundation`，增加六张表以及库存批次、订单有效需求标识、计划模式字段。新表均开启 RLS，并通过复合外键约束租户引用。原始证据与冻结输入的删除被数据库触发器阻断。

**现有数据迁移注意：**

1. 不根据旧数据自动推断权威来源；必须显式配置。
2. 不给旧快照伪造“完整批次”证明；重新完整采集或导入。
3. 原始订单/日总量需要重放到新的来源分离投影；旧日总量表不会自动成为新权威数据。
4. 旧计划默认为 shadow，不能在升级后意外发布。
5. 已有客户库先备份、在副本演练、评审覆盖范围；测试库迁移通过不能替代客户存量迁移验收。
6. 小批迁移已验证往返；大表回填、在线约束和长时间锁仍需单独评估。

## 5. 一个可重复的接入顺序

1. `aegis tenant-create` 创建隔离租户，账号密码通过交互或部署密钥管理传入。
2. 通过 `/api/v1/connections` 创建平台、WMS/ERP REST 或 CSV 连接。
3. 导入 SKU、地点及其时区、线路等主数据；未知映射先隔离再修复，不猜测相似 SKU。
4. 用 `/api/v1/connections/{id}/files/{stream}` 上传 CSV/XLSX，随后请求同步；或使用平台增量/回填。
5. 查看 `/api/v1/data/batches` 与 `/api/v1/quarantine`，确认关键批次 complete，修复并重放记录。
6. 用 `/api/v1/data/authorities` 批准库存、需求和 inbound 的来源规则；从通配总量切换分渠道时先移除原规则，再在同一维护窗口配置新规则。
7. 查看库存快照及 PO 行，通过 `/api/v1/inventory/inbound-links` 提供明确关联与证据。
8. 运行 `aegis pilot-check --tenant <uuid>` 或读取 `/api/v1/data/pilot-readiness`。
9. 默认创建 `mode=shadow` 的计划，审核输出与源单据；不调用外部执行。

`pilot-check` 中 `data_ready=true` 仅说明当前已实现的数据门禁通过。报告仍单独输出 `live_pilot_approved=false` 和所需外部证据；它不是自动签署客户 UAT 或 14 天对账的工具。

开发样例入口：

```bash
aegis import-sample --tenant <uuid> --path data/sample_network.json
aegis pilot-check --tenant <uuid>
aegis worker plan --once
```

样例导入使用真实 raw→批次→规范化管线，并为合成样例建立明确来源规则；这些规则不应用于真实客户数据。生成计划需先经 API 请求，Worker 不会凭空生成任务。

## 6. 执行和回写保护

```dotenv
AEGIS_PLAN_EXECUTION_ENABLED=false
AEGIS_WRITEBACK_ENABLED=false
```

- shadow 计划不预占真实库存，也不能发布。
- 执行计划必须显式启用执行开关；外部回写还需要第二个独立开关。
- 发布会重新检查当前数据门禁、源规则是否变更，以及最新库存和 held 预占，而非只信任旧冻结输入。
- 已建立的预占不因 TTL、外部创建 confirmed 或一张新快照而自动释放。
- 当前仍保留同租户一个活动计划约束。执行对账/消耗/释放、分批实际收发和多计划替代尚未完成，真实试点应保持 shadow。
- 回写单据创建成功不等于实物发运、收货或可售；本轮不将两者混同。

## 7. 验证记录

最终结果和复核命令见本节更新；验证仅使用隔离测试数据库和合成/模拟平台响应，无真实商家外部写入。

- 全仓库测试（使用真实 PostgreSQL 16）：**91 项通过，0 失败，0 跳过**；另有 1 条 Starlette/httpx 测试客户端弃用警告（不影响本轮断言）。
- 新迁移：验证空库升级到0003、0003→0002→0003、完全回退到base后再升级。
- 结构一致：Alembic metadata drift 检查、全部业务新表 RLS 检查。
- 数据：有效订单状态、跨渠道来源、不完整批次、缺失与零值、分页失败不推进游标、重试raw复用。
- 库存：单据在途防重、跨租户引用、并发预占、幂等发布、影子和回写开关。
- 完整合成样例：15个SKU、4个需求仓、180天，10,922条记录全部落地并规范化，隔离0条；60条需求序列通过当前数据门禁，shadow计划进入`pending_review`，冻结输入重算的汇总完全一致。该样例只证明管线与复现，不证明真实市场预测精度或经营收益。
- 归档证据：[合成样例结果](artifacts/p0-synthetic-smoke.json)、[源码与运行时manifest](artifacts/p0-release-manifest.json)。CI同时归档回测与manifest；本地manifest没有容器镜像digest。
- 已知环境提醒：本机曾空间不足，临时数据库因此重建为小 WAL 实例；最终测试应以重建后的完整运行结果为准。不得将空间不足的运行算作通过。

```bash
AEGIS_TEST_ADMIN_DSN='<disposable-owner-dsn-ending-_test>' .venv/bin/pytest -q
PYTHONPATH=src .venv/bin/python -m compileall -q src
PYTHONPATH=src .venv/bin/python -m aegis.cli release-manifest \
  --output /tmp/aegis-p0-release-manifest.json
```

测试库中的执行开关仅为故障注入显式打开，应用默认和部署示例均保持关闭。

## 8. 下一步，及尚不能宣称完成的事项

P0 工程与部分数据关键风险已具备实现/测试证据，但以下仍是进入真实试点前的条件：

1. 获得真实 Amazon/TikTok 账户授权、目标市场权限与脱敏 fixture；完成逐市场接口和身份核验，不只检查目录。
2. 与实际 WMS/海外仓/ERP 签署库存、订单和单据关联契约，完成持续源端对账。
3. 补齐组合行删除/替换、来源版本冲突、退款/退货及双时间事实模型；完善批次/窗口历史覆盖证明。
4. 建立完整库存事件、执行累计量、预占消耗/释放及跨计划替代；在此之前不能开放真实连续滚动执行。
5. 完成生产工作台、数据范围权限、配额公平性、SSO、恢复演练与 Linux 镜像/完整发布包验证。
6. 采购与调拨经济语义、时间分桶计划、多提前期模型校准和公平仿真仍按原方案进入后续工作包；本轮没有将这些标记为已完成。

主要实现入口：[批次](../src/aegis/integration/batches.py)、[来源规则](../src/aegis/integration/authority.py)、[需求投影](../src/aegis/integration/observations.py)、[库存服务](../src/aegis/inventory/service.py)、[规划门禁](../src/aegis/planning/assembler.py)、[测试](../tests/production/test_foundation.py)。
