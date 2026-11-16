# 12. 生产化开发落地方案：数据集成与规划域

> 适用对象：产品负责人、架构师、后端、算法、前端、测试、运维
> 目标：把 Aegis-Predict 从比赛原型升级为可接入市场数据、ERP 及其他同步系统的生产系统
> 参考对象：`agentic-aps`（JVS-APS）仓库的架构与治理模式

---

## 0. 结论先行

1. **agentic-aps 的真实价值不在 ERP 同步。** 在其 main 分支、所有远程分支和全部提交记录中检索 ERP、同步、connector、outbox、watermark、webhook，都没有对应实现。唯一像外部数据进入的地方，是报工 Excel 导入 `aps/jvs-aps-mgr/.../controller/TaskReportController.java`：同步读取整个文件，只在同一批数据内用字符串拼接去重（见 `TaskWorkReportFacadeServiceImpl.importBatchWorkReportRefreshTaskProgress`），没有幂等键、游标和对账。这是反面教材。
2. **它真正值得借鉴的是生产治理模式：**
   - 计划先「待确认」，可预览、放弃或确认，确认后才生效；
   - 人工锁定的结果在重排时保留；
   - 策略模板、动态权重，以及用 `randomSeed` 保证结果可复现；
   - 异步求解，可取消，进度按租户隔离；
   - 用 `tenant_id` 拦截器加 `TaskDecorator` 把租户上下文传进异步线程；
   - 按动作定义权限码（如 `@RequirePermission`），关键操作用 `@AuditOperation` 记审计；
   - 文档里「先自我辩证、再克制 scope」的规划方式。
3. **Aegis-Predict 的「玩具感」在外壳，不在算法内核。** 机器学习预测、MILP 分仓、仿真这些内核还能用。玩具的是：数据全靠 JSON 文件、状态放在内存和文件里、只有一个写死的密码、没有多租户和审计、2000 行的 `webapp.py` 混在一起。
4. **路线：保留 Python 和现有算法，逐步替换外壳。** 新建以 PostgreSQL 为中心的「模块化单体 + 独立 Worker」。新系统的核心是一个自研的数据集成子系统，支持幂等、断点续传、对账和回写。整体约 16–17 周，分 6 个阶段，其中第 2–3 阶段做完就能接入真实 ERP 试点。

---

## 1. Aegis-Predict 现状评估

| 维度 | 现状 | 评级 | 证据 |
|---|---|---|---|
| 预测 | LightGBM/XGBoost/RF 集成，加滞后/滚动特征和市场信号 | B- | `forecasting.py` |
| 补货策略 | 公式有结构性问题（见下） | C | [replenishment.py](../src/overseas_inventory/replenishment.py) L52-L57 |
| 分仓优化 | scipy MILP，含库存池、预算、仓容约束，目标是加权打分而非成本 | C+ | `optimizer.py` |
| 数据接入 | 只有 `load_network(json)` 一次性加载 | E | `models.py` |
| 持久化 | `runs/` 目录和 `operations_state.json` | E | `webapp.py` |
| 鉴权/租户/审计 | 单一密码，默认 `globalpool` 写在 compose 里；无 RBAC、无租户 | E | `docker-compose.yml`, `competition_web.py` |
| 工程化 | 无 CI、无配置分层、无迁移工具、测试只覆盖主路径 | D | `pyproject.toml`, `tests/` |

**补货公式的问题**（接真实数据前必须修）：

- 安全库存 `z·σ·√LT` 没考虑提前期本身的波动。跨境海运提前期波动很大，这一项往往比需求波动影响更大。
- `target = p80(30天) + SS` 把不确定性算了两遍，而且 30 天这个周期和「提前期 + 盘点周期」没有关系。
- 库存位置没扣已承诺量，也没算未到货的采购单。
- 缺货期间的 0 销量被当成真实 0 需求。真实数据里这会系统性地低估需求，然后更容易缺货，形成恶性循环。

---

## 2. agentic-aps 分解与提炼

| 能力 | agentic-aps 做法 | 借鉴方式 |
|---|---|---|
| 计划生命周期 | 求解结果写入 `*_pending` 表，预览、确认或放弃 | ✅ 升级为补货计划状态机，并增加按行审批和回写 |
| 人工锁定 | 任务 pinned=true，重排时保留 | ✅ 计划行锁定：人工改过的数量重算时不覆盖 |
| 策略模板 + 种子 | `StrategyConfigDTO` 含权重、终止条件、`randomSeed` | ✅ 策略带版本；每次计划记录策略版本、模型版本、数据快照版本和种子 |
| 异步求解 | `@Async`，进度存 Redis，可取消 | ✅ 但求解放到独立 Worker 进程。它自己的书第 07 章也说「求解器未独立」是技术债 |
| 多租户 | 共享表加 `tenant_id` 拦截器，再用 `AsyncContextConfig` 中的 `TaskDecorator` 传递上下文 | ✅ 沿用共享表方案，再加一层 PostgreSQL 行级安全（RLS）做纵深防御。异步任务显式携带 tenant_id，不依赖线程变量 |
| 权限/审计 | 动作级权限码 + 审计切面 + 数据范围断言 | ✅ 直接采用这一模式 |
| 规划方法论 | `book/12_multitenant_rbac_production_plan.md` 先自我辩证，再列「不做清单」 | ✅ 本方案沿用 |
| OptaPlanner 元启发式 | 车间排程（时序和资源冲突） | ❌ 我们的问题是批量分配和运输方式选择，属于线性结构。MIP 求解器能给出最优性间隙，更合适 |
| Java/Spring 技术栈 | — | ❌ 不迁移，原因见 3.1 |
| Excel 导入 | 整批同步读取，只做批内去重 | ❌ 改为「原始数据落地 + 幂等合并 + 隔离区」 |

---

## 3. 自我辩证：关键决策

**3.1 要不要像 agentic-aps 一样换成 Java？** 不换。Java 的集成生态（Spring Integration、Debezium）确实更成熟，但核心资产（机器学习、优化、仿真）都在 Python 里，跨语言意味着要拆服务、做双份序列化和两套部署，这对 2–4 人的团队负担很重。集成层用 Python 写完全够用，瓶颈在外部 API 的限流，不在语言性能。

**3.2 集成层自研，还是用 Airbyte、dlt 或 iPaaS？**

- Airbyte 太重（多容器，最好跑在 k8s 上），也没有国内跨境 ERP（领星、马帮、易仓等）的连接器。
- 纯 ELT 工具只解决「搬数据」。我们还需要按租户管理凭据、回写 ERP、业务校验、SKU 映射和隔离区。
- **决策：** 自研一个精简的连接器接口（SPI），协议参考 Airbyte 的「数据记录 + 状态检查点」消息模型。对纯分析类的市场数据源（如 Keepa），可以在连接器内部使用 `dlt` 库来省事。

**3.3 现在要不要上 Kafka 或微服务？** 不要。PostgreSQL 的 outbox 表加 `procrastinate`（基于 Postgres 的任务队列，用 `SKIP LOCKED` 取任务）已经能提供事务性的事件发布、定时任务和重试，少维护两套中间件。

- 升级触发条件：事件量持续超过约 1k/s，或出现多个需要独立回放的消费服务。
- 进程上拆成 api、sync-worker、plan-worker、scheduler 四个，代码保持单仓单包，用 `import-linter` 约束模块边界。

**3.4 要不要实时？** 补货决策是天级或小时级，不需要流式计算。webhook 只用来提高数据新鲜度，并且必须配合定期对账，因为 webhook 不保证一定送达。现有的「小时级沙盘」保留为仿真功能，不进入主链路。

**3.5 多租户现在就做吗？** 数据模型从第一天就带 `tenant_id` 和 RLS，因为现在做成本很低，以后补会很贵。这正是 agentic-aps 第 12 章总结的教训：它写死租户为 1，后面要整体返工。部署上，先给单个试点租户上线。

**3.6 算法要重写吗？** 不重写，只修正。新增一个 `NetworkAssembler`，从数据库组装出现有的 `Network` 数据类，这样现有算法和测试可以原样继续跑。然后按第 6 节逐项修正。

---

## 4. 目标架构

```
             ┌──────────── 外部系统 ────────────┐
  ERP(领星/马帮/易仓/金蝶)  Amazon SP-API  Shopify  海外仓WMS(谷仓/万邑通)  FX/节假日/Keepa  CSV/Excel
             └──────┬───────────────▲───────────┘
          拉取/Webhook│               │回写(PO/调拨/入库计划)
┌───────────────────▼───────────────┴───────────────────────────────┐
│ sync-worker: Connector Runtime (限流/重试/租约/检查点)               │
│   Raw Landing(S3/MinIO + raw_record) → Normalize/Map → Validate    │
│   → Canonical Upsert(版本守卫) → Outbox      Writeback Dispatcher   │
├────────────────────────────────────────────────────────────────────┤
│ PostgreSQL 16 (RLS, 按月分区)  — 规范模型 / 状态 / outbox / 审计      │
├────────────────────────────────────────────────────────────────────┤
│ plan-worker: Snapshot → Forecast → Policy → MILP → Simulation      │
│ api (FastAPI /api/v1, RBAC, 审计)    scheduler (procrastinate 周期任务) │
│ Redis: 令牌桶限流/缓存     OTel → Prometheus/Grafana/Loki            │
└────────────────────────────────────────────────────────────────────┘
```

**技术栈：** Python 3.12、FastAPI、SQLAlchemy 2.0 + Alembic、Pydantic v2 + pydantic-settings、procrastinate、PostgreSQL 16、Redis 7、MinIO/S3、httpx + tenacity、structlog、OpenTelemetry；求解用 HiGHS（`highspy`），大规模时可换 OR-Tools；测试用 pytest、testcontainers、hypothesis、respx。

**代码结构**（`overseas_inventory` 包更名为 `aegis`）：

```
src/aegis/
  core/          config, db(session+RLS), tenancy, security, audit, observability
  catalog/       sku, sku_component(组合件), location, channel
  inventory/     snapshot, inbound(PO/在途)
  demand/        order_line, daily_demand(含缺货删失标记)
  integration/
    spi.py  runtime/{runner,state,lease,ratelimit,retry,errors}
    raw/  mapping/  quality/  outbox/  writeback/
    connectors/{file_csv,lingxing,amazon_sp,shopify,goodcang,fx_ecb}/
  planning/
    assembler.py (DB → Network)  forecasting/  policy/  optimizer/  simulation/
    lifecycle.py (PlanRun 状态机)
  api/v1/  workers/{sync,plan,scheduler}.py
migrations/   tests/{unit,contract,integration,property}/
```

`webapp.py`（基于 http.server）标记为废弃；`competition_web.py` 保留为 `demo` 模式，由开关控制。

---

## 5. 数据集成子系统（核心）

### 5.1 分层数据流与不变量

1. **Raw Landing**：原始数据按原样落地，不可修改，带 payload hash。任何下游 bug 都可以从这一层重放修复。
2. **Normalize/Map**：由带版本的映射代码，把源数据转成规范模型（canonical）。
3. **Validate**：做数据质量校验。失败的记录进入隔离区，不丢弃，也不阻塞整批。
4. **Canonical Upsert**：带版本守卫的幂等合并，同一事务内写入 outbox 事件。
5. **检查点只在数据落地后推进**：「至少一次投递 + 幂等合并」，效果上等价于恰好一次。

### 5.2 Connector SPI

```python
class Capabilities(BaseModel):
    streams: list[StreamSpec]           # name, sync_modes, primary_key, cursor_field, kind: event|snapshot|master
    supports_webhook: bool
    writeback_commands: list[str]       # create_po / create_transfer / create_fba_inbound_plan
    rate_limits: dict[str, RateSpec]    # 按 endpoint

class SourceConnector(Protocol):
    kind: ClassVar[str]; version: ClassVar[str]
    def capabilities(self) -> Capabilities: ...
    async def check(self, ctx: ConnCtx) -> HealthResult: ...
    def read(self, ctx: ConnCtx, stream: StreamSpec, state: StreamState,
             window: SyncWindow | None) -> AsyncIterator[RawRecord | Checkpoint]: ...

class WritebackConnector(Protocol):
    async def submit(self, ctx: ConnCtx, cmd: WritebackCommand) -> SubmitResult: ...
    async def lookup(self, ctx: ConnCtx, idempotency_key: str) -> ExternalRef | None: ...  # 超时后先查再重试
    async def status(self, ctx: ConnCtx, ref: ExternalRef) -> ExternalStatus: ...

class Mapper(Protocol):                  # 与 connector 版本绑定，可单独重放
    def to_canonical(self, raw: RawRecord, m: MappingCtx) -> list[CanonicalRow] | Quarantine: ...
```

- 连接器只负责「读出原始数据和检查点」以及「提交、查询外部单据」。
- 限流、重试、租约、落库、推进检查点全部由 runtime 统一处理，连接器开发者只需写拉取逻辑和映射。

### 5.3 按数据语义选同步模式

| 数据 | 语义 | 模式 | 关键点 |
|---|---|---|---|
| SKU、仓库、渠道主数据 | master | 全量（小）或增量 + 每日全量对账 | 按源端软删除做停用处理，不物理删除 |
| 订单行 | event + 后续变更 | 按 `updated_at` 增量，**游标回看重叠窗口**（默认 2h，Amazon 建议 24h） | 订单状态和退款会回溯修改 |
| 库存 | **snapshot** | 定时快照（Amazon 用 Reports API，Shopify 用 inventory_levels） | 同时存 `as_of`（业务时间）和 `ingested_at`（入库时间）；计划只用 `as_of ≤ 截止时间` 的最新快照 |
| PO、头程、在途 | event | 增量 + 状态机 | 决定库存位置；回写后要回读状态形成闭环 |
| Webhook（Shopify） | 提醒 | 收到 → 验签 HMAC → 落地 raw → 触发该实体的增量拉取 | **不信任 webhook 内容本身**，以拉取结果为准 |
| 汇率、节假日、市场信号 | 时间序列 | 按日期增量 | 主键为 `(source, entity, metric, date)` |

### 5.4 状态、游标与并发

- 每个 `(connection, stream)` 只有一行 `sync_stream_state`。用**租约字段** `lease_owner/lease_expires_at` 保证同一流只有一个写入者，不依赖 Redis 锁；Worker 崩溃后租约过期，自动由别的 Worker 接手。
- 推进检查点的写入和 raw 批量插入**在同一个数据库事务内提交**。Worker 被 kill -9 后，从上一个检查点重新拉取，重复数据由唯一约束吸收。
- 回填（backfill）和日常增量使用独立的 job 和窗口，**不移动线上游标**。

### 5.5 幂等、乱序与无变化写入

```sql
-- raw 层：同一版本只落一次
unique (connection_id, stream, external_id, payload_hash)

-- canonical 层：版本守卫 + 内容哈希，防乱序覆盖和无意义写放大
insert into order_line (...) values (...)
on conflict (tenant_id, source_system, external_order_id, external_line_id) do update
  set qty = excluded.qty, status = excluded.status, ..., source_updated_at = excluded.source_updated_at,
      content_hash = excluded.content_hash
  where order_line.source_updated_at <= excluded.source_updated_at
    and order_line.content_hash is distinct from excluded.content_hash;
```

订单行变化后，把受影响的日期标记为「脏」，由下游只对这些日期增量重算 `daily_demand`，不做全表重算。

### 5.6 映射与隔离区（跨境场景最容易翻车的地方）

- `id_mapping(tenant, source_system, id_type[seller_sku|asin|fnsku|msku|erp_sku|wms_code], external_id → sku_id, status[auto|confirmed|pending], confidence)`。
- 组合件和多件装用 `sku_component` 拆解为基础 SKU 的需求。
- 仓库映射：Amazon 履约中心 → 区域，3PL 仓库代码 → location。
- 匹配不上的记录进入 `quarantine_record` 并标注原因，不丢弃。在 UI 中处理完映射后，**从 raw 层重放**这些记录。
- 规范化规则：数量统一为基础单位；金额按交易日汇率转换，同时保留原币；按**站点本地时区**切分自然日，否则日销量会错位。

### 5.7 回写 ERP 或 WMS（第二个最容易翻车的地方）

`writeback_command` 状态机：

```
pending → sending → confirmed
             └─(超时/网络断)→ sent_unconfirmed → lookup(idempotency_key)
                                      ├─ 找到 → confirmed
                                      └─ 未找到 → pending(退避重试) → … → dead(告警, 人工)
```

- `idempotency_key = hash(tenant, plan_run_id, plan_line_id, command_type)`，写进 ERP 单据的外部单号或备注字段。
- **绝不对「创建」类接口做盲目重试。** 超时后一律先 lookup 再决定是否重试。
- 回写命令只由「已批准」的计划行在同一事务里写入 outbox 产生，然后由 dispatcher 投递。

### 5.8 限流、重试与错误分级

| 类别 | 例子 | 处理 |
|---|---|---|
| 临时错误 | 429、5xx、超时 | 指数退避 + 抖动；遵守 `Retry-After` 和速率响应头；Redis 令牌桶按 `(tenant, connection, endpoint)` 限流 |
| 认证错误 | token 失效、授权被撤销 | 先尝试刷新一次；失败则连接置为 `auth_failed` 并暂停，同时告警 |
| 数据错误 | 单条字段非法、映射缺失 | 记录进隔离区，整批继续 |
| 契约错误 | 源端 schema 变更、必填字段消失 | 该流熔断停跑并告警，避免把错误数据写进规范层 |

每个连接都有熔断器：连续 N 次失败就暂停一段时间。

### 5.9 新鲜度、对账与数据质量门禁

- **新鲜度 SLO**（核心指标）：`now - max(source_updated_at)`，按流统计。建议默认值：订单 ≤ 1h，API 库存 ≤ 2h，报表类在站点时间 T+1 06:00 前。
- **计划门禁**：生成计划前检查关键流的新鲜度和隔离区比例。超标时拒绝生成，或在计划上打「数据陈旧」标记，并要求审批人确认。
- **每日对账**：逐日比较源端和本地的订单数、件数、库存总量；差异超过阈值时，自动对该窗口执行回填。
- **数据质量规则**（放在 `quality/`）：负库存、销量突增超过 P99×10、价格为 0、未来日期等。

### 5.10 凭据与合规

- 凭据用信封加密：第一期用 Fernet + 环境变量中的 KEK，之后切到 KMS 或 Vault，支持轮换。数据库里只存 `secret_ref`，日志自动脱敏。
- **数据最小化**：补货不需要买家个人信息。Amazon 不申请 PII 角色，也不用 RDT（受限数据令牌），可以大幅降低 GDPR 和平台审核成本。

### 5.11 首批连接器

| 优先级 | 连接器 | 理由 |
|---|---|---|
| P0 | `file_csv` | 通用兜底；大量卖家只能从 ERP 导出 Excel；也用来导入现有 demo 数据 |
| P0 | ERP #1（**待确认**，例如领星） | 一个 ERP 往往已经聚合了多平台订单、库存和 PO，性价比最高 |
| P1 | Amazon SP-API（Orders + FBA 库存报表） | 主战场数据最权威，也用来和 ERP 数据对账 |
| P1 | 海外仓 WMS #1（谷仓或万邑通） | 第三方海外仓的真实库存和入库状态 |
| P2 | Shopify（GraphQL Bulk + Webhook）、汇率（ECB）、节假日 | 独立站渠道和基础市场数据 |
| P3 | 广告、Keepa 等市场数据 | 作为预测特征，接入前需评估付费成本 |

---

## 6. 规划域生产化

**计划状态机**（参考 agentic-aps 的待确认计划）：

```
DRAFT → SOLVING → PENDING_REVIEW → PARTIALLY_APPROVED/APPROVED → RELEASED(回写) → EXECUTING → CLOSED
                     └→ DISCARDED            (FAILED / CANCELLED 任意时刻)
```

- 每个 `plan_run` 冻结以下信息：数据快照（每个流的高水位线）、策略版本、模型版本、随机种子。**同一输入必须能复现同一结果。**
- `plan_line_pin`：人工改过的数量或运输方式，在重排时作为约束保留。
- 审批阈值由策略配置决定，取代当前写死的 `margin ≥ 1200 or urgency ≥ 0.55`。

**算法修正清单：**

1. 库存位置 = 在库 − 已承诺 + 在途 + 未到货 PO − 欠单。
2. 安全库存 SS = z·√(L·σd² + d²·σL²)，其中 σL 用真实头程数据估计；订货上限 S = d·(L+R) + SS（R 为盘点周期）。
3. 缺货删失：缺货天打标记，从需求估计中剔除这些天，或做插补。
4. 长尾和间歇性需求用 Croston/TSB；新品冷启动按同类目相似品的曲线；需求按 SKU×渠道×区域分层，并保证层级一致。
5. 优化目标改为总成本最小：头程 + 仓储 + 尾程 + 关税 + 缺货惩罚 + 滞销惩罚；加入运输方式（海/空/快递）、MOQ、箱规和整柜装载率约束；求解时间预算按规模配置，超时退回启发式解，并记录最优性间隙。
6. **回测框架**：滚动起点回测，按品类和 ABC 分层输出 WAPE 和偏差（Bias）。CI 中设置回归阈值，模型变更导致指标劣化就阻断合并。

---

## 7. 多租户、权限与审计

- 所有业务表带 `tenant_id`，并开启 PostgreSQL RLS（`current_setting('app.tenant_id')`）。每个请求和每个 Worker 任务在事务开始时 `SET LOCAL`；任务载荷必须带 tenant_id。
- 角色：`TENANT_ADMIN`、`INTEGRATION_ADMIN`、`PLANNER`、`APPROVER`、`VIEWER`。权限码按动作定义：`integration:connection:edit`、`integration:mapping:resolve`、`integration:backfill:run`、`plan:generate`、`plan:adjust`、`plan:approve`、`plan:release`、`audit:view`。数据范围按 渠道/站点/仓库 限定。
- 审计：关键写操作记录操作人、动作、前后差异和 trace_id，用装饰器统一实现，对应 agentic-aps 的 `@AuditOperation`。
- 认证：第一期用本地账号（argon2 加短期会话），接口按 OIDC 预留；不自建完整 IAM。

## 8. 可观测性、测试与部署

- **指标**：同步耗时、读/写/隔离条数、新鲜度滞后、429 比例、回写确认延迟、求解时长和最优性间隙、预测 WAPE；按 sync_run 和 plan_run 全链路追踪。
- **测试金字塔**：
  - 领域单元测试；
  - 连接器契约测试，用录制的真实响应 fixture，每个连接器版本一套；
  - 用 hypothesis 做**性质测试**：同一批数据重放两次结果不变，打乱顺序后最终状态一致；
  - 混沌测试：同步写到一半时 kill Worker，验证无重复、无丢失；
  - 回写故障注入：ERP 已成功但响应超时，验证不会生成重复 PO；
  - 用 testcontainers 起真实 Postgres 做集成测试。
- **CI**：ruff、mypy、pytest、alembic 迁移往返检查、回测阈值。
- **部署**：试点期用 docker compose 运行 api、sync-worker、plan-worker、scheduler、postgres、redis、minio、otel；用 pgBackRest 做 WAL 归档（RPO ≤ 15min）；之后迁到 k8s。
- **仓库整理**：比赛材料（docx/pdf/pptx/png 共约几十 MB）移到单独仓库或 Git LFS。

---

## 9. 实施路线（假设 2–3 名后端、1 名前端、1 名算法）

| 阶段 | 周期 | 交付 | 验收口径 |
|---|---|---|---|
| **P0 方案冻结** | 1 周 | 确认首批 ERP、平台和海外仓；拿到沙箱凭据；评审规范模型；冻结「不做清单」 | 首批连接器的 API 文档和权限都已到位 |
| **P1 地基** | 3 周 | 包重构、Postgres + Alembic、配置分层、认证 + RBAC + RLS、审计、CI、compose；`NetworkAssembler`；`file_csv` 连接器导入 demo 数据 | 现有算法从数据库数据跑出与原 JSON **一致**的结果；跨租户访问测试全部被拦截 |
| **P2 集成框架 + 首批连接器** | 4 周 | runtime（租约、检查点、限流、重试、熔断）、raw 落地、映射和隔离区 UI、新鲜度指标、对账；ERP #1 + Amazon | 试点租户 90 天数据回填完成；对账差异 < 0.5%；kill -9 后续跑无重复；隔离率 < 1% |
| **P3 规划生产化** | 3 周 | 计划状态机、锁定、策略版本、第 6 节算法修正、回测框架、独立 plan-worker | 计划可从快照复现；回测报告分层输出 WAPE；优化在规模目标内（如 5k SKU×10 仓）≤ 60s |
| **P4 回写闭环** | 3 周 | 审批流、PO/调拨回写、状态回读、WMS #1、Shopify webhook | 故障注入下零重复单据；回写状态 100% 可追踪 |
| **P5 市场数据与灰度** | 2–3 周 | 汇率、节假日、广告和市场信号；压测；安全评审；**影子模式**试运行 4 周（系统只出建议，人工照常决策，事后对比） | 影子期建议采纳率和缺货、周转的对比报告达到业务确认的阈值后再放量 |

总计约 **16–17 周**。P2 结束就有可用的数据底座，P3 结束可以开始影子试运行。

**一期不做：** Kafka、微服务、每租户独立数据库、自研 IAM、字段级权限、实时流式计算、自动下单（P4 回写也必须先经人工审批）。

---

## 10. 风险与待确认问题

1. **使用哪家 ERP，哪家海外仓？** 这决定 P0 的连接器选型和工作量。国内 ERP 的 OpenAPI 多数需要申请 appKey、配置 IP 白名单和签名，限流也偏严，经常是秒级 1 次。
2. **业务形态：自用、少数客户私有化部署，还是 SaaS？** 这会影响 RLS 是否够用，以及计费和配额设计。
3. **数据规模**：SKU 数、仓库数、日订单量，决定分区策略以及是否需要引入列式存储做分析。
4. Amazon SP-API 需要开发者注册和角色审核，最好在 P0 就启动，周期不可控。
