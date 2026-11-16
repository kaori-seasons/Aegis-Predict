# v0.7.0 生产工作台接入与验证

后续v0.8.0管理页面与迁移见[第18篇](18-governance-workspace.md)；本文及其证据保留为v0.7.0历史记录。

日期：2026-11-16。基于 v0.6.0 的数据、认证和库存底座，将五个业务页面与新增数据源管理统一到生产服务。本文替代 README 中原来的“两套入口尚未统一”状态；第16篇文档保留为上一轮历史记录。

## 交付结果

`aegis serve` 的 `/` 是生产工作台，`/login` 为 Google 入口。五个页面共用当前租户、计划选择、权限和会话，新增数据源管理。前端使用同源 Cookie 与 CSRF，不保存 bearer token，不调用 `/student/*`；静态文件随 Python 包和镜像发布，以内容摘要更新缓存版本。

独立仿真工具仍可单独运行，但其密码、场景和演示审批不参与生产链路。Amazon/TikTok 保持显式 Mock：需要创建连接、Mock授权、同步；合成批次和真实来源不能混合执行。

| 页面 | 生产数据与操作 | 核心门禁 |
| --- | --- | --- |
| 仓网总览 | 当前权威批次、按SKU取最新库存、来源完整性、所选冻结计划风险 | 只有完整权威批次可代表库存，缺仓/缺SKU不标为全仓网完整 |
| 销量预测 | 指定计划明细与周期，读取摘要校验后的冻结历史并计算分位；显示缺失/删失与模型证据 | 不从演示数据或固定系数生成预测；分位为近似，未声称校准服务承诺 |
| 补货队列 | 建计划、Worker求解、调整数量、批准/驳回、受控发布 | 人工说明、状态机、权限、来源/批次/对账与原子预占继续由服务端验证 |
| 分仓方案 | 分配/运输、当前计划预占、累计执行、库存结算、关闭计划 | 单调版本、守恒、最终执行、执行引用、双端权威库存对账 |
| 效果评估 | 对冻结输入做滚动留出回测，WAPE/偏差/均值基线及规划成本 | 无真实经营对照时不展示利润、履约率提升；缺有效样本明确提示 |
| 数据源管理 | 新建Mock/CSV/REST连接、授权、暂停/恢复、密钥替换、上传/同步、来源批准/撤销、清单对账、隔离重放 | 密钥不回显；重放不自动接受质量异常；对账差异会持久化并阻止执行 |

工作台写操作串行提交，页面显示进行中状态；401清空页面数据并返回登录，退出撤销服务端会话。5秒会话检查仅在可见页运行；有待计算计划/同步任务时可轮询更新，编辑中或打开对话框时不自动刷新表单。

## 接口变化

新增只读投影均复用 PostgreSQL 租户会话/RLS与角色权限，没有另建绕过业务服务的写入路径。

| 方法与路径 | 用途 / 权限 |
| --- | --- |
| `GET /api/v1/workspace/overview` | 权威库存聚合、租户概况；`data:view` |
| `GET /api/v1/workspace/catalog?limit=500&offset=0` | 活跃SKU分页、地点目录；`data:view` |
| `GET /api/v1/workspace/plans/{id}/forecast?line_id=...&horizon=30` | 冻结计划预测；`plan:view` |
| `GET /api/v1/workspace/plans/{id}/evaluation` | 冻结计划滚动回测；`plan:view` |
| `GET /api/v1/workspace/plans/{id}/execution` | 当前计划预占/最新执行/结算/回写命令及候选批次；`plan:view` |
| `PATCH /api/v1/connections/{id}` | 复用连接配置校验服务，暂停/恢复和更新配置；`integration:connection:edit` |

`/auth/providers` 增加非敏感的CSRF Cookie名称和执行/回写开关；文件上传回执增加 `source_file_version`，便于独立CSV清单使用源文件版本。发布请求新增兼容旧客户端的可选 `note`，写入 `plan.release` 审计；隔离重放也记录说明。关闭计划要求 `plan:release`，取消/丢弃要求 `plan:adjust`。

其余业务写入继续使用原有 `/plans`、`/data/authorities`、`/data/batches/{id}/reconcile`、`/inventory/reservations/{id}/execution` 和 `/settle`。候选批次仅是表单预填，服务端仍重新验证，不因前端展示而信任。

## 可重复验证

### 自动化回归

隔离 PostgreSQL 16 测试库运行全仓库测试：**111通过、0失败、0跳过**。一个依赖库的 Starlette TestClient/httpx 弃用警告，不影响本轮断言；后续升级测试客户端时处理。

新增 `tests/production/test_workspace.py` 覆盖：

1. 生产入口/资源存在，未登录401，生产应用没有仿真路由；权威库存聚合，冻结摘要/预测/回测一致。
2. 第二租户无法读取第一租户的计划预测、执行与评估（404），库存目录隔离。
3. 连接暂停/恢复，缺CSRF拒绝，Viewer可读而不可修改连接或生成计划。
4. HTTP建执行计划 → Worker求解 → 审批 → 发布（含说明审计）→ 累计执行 → 提前关闭409 → 文件上传 → Worker规范化 → 从实际源CSV独立重读清单 → 精确对账 → 双端结算 → 关闭。

```bash
AEGIS_TEST_ADMIN_DSN='postgresql+psycopg://OWNER@HOST:5432/aegis_test' pytest -q
python -m compileall -q src
node --check src/aegis/api/static/workspace.js
```

### 真实浏览器流程

Chrome Agent Window + 本地生产API、PostgreSQL及真实规划/同步Worker：

- 从 `/login` 发起Google start，经测试供应商到真实callback；校验state、浏览器绑定、PKCE、nonce与RS256签名后建立真实不透明会话。测试没有注入浏览器登录Cookie。
- 页面创建Amazon US Mock、TikTok US Mock连接，显式Mock授权并同步；使用相同生产解析/规范化链路。
- 生成执行计划，批准并发布195件；记录累计发运195、收货195、取消0，标记最终执行。
- 导入执行后库存：源仓1000→805，目的仓1→196。独立CSV清单声明同一单据执行引用，对账通过，结算195件后关闭计划；页面最终状态 `closed`。
- 退出撤销会话并返回 `/login`。
- 390、768、1440px × 六个页面共18组布局检查：无文档级横向溢出，六个导航入口存在，页面主内容没有错误或异常空态。宽表在自身容器滚动，手机审批为纵向条目。截图不包含登录页，均为合成数据。

Google供应商使用 `scripts/workspace_e2e_server.py` 的本地测试替身，不是已完成Google Cloud公网验收。浏览器原生文件暂存遇到CDP `Not allowed`，因此用已知合成文件构造浏览器File对象附到实际上传控件，再点击页面提交；上传API、Worker、清单解析和对账均实际运行。原生系统文件选择器的自动化上传没有得到验收。

测试替身只接受显式本机 `*_test` 数据库；绑定127.0.0.1，密钥临时生成，测试路由仅由该脚本安装。`aegis serve` 和生产镜像不包含这些测试路由。复现命令：

```bash
.venv/bin/python scripts/workspace_e2e_server.py \
  --database-url 'postgresql+psycopg://OWNER@127.0.0.1:55439/aegis_workspace_test'
# 读取 /tmp/aegis-workspace-e2e.json 的租户标识，在 http://127.0.0.1:8020/login 登录
```

### Linux交付验收

镜像分别标记 `aegis-predict:0.7.0-arm64` 与 `aegis-predict:0.7.0-amd64`。使用 `scripts/verify_container.py` 创建独立数据库/网络，验证数据库升级至 `0004_execution`、非root UID10001、只读根目录、健康检查、默认Google/Mock、回测门限和工作台HTML/JS资源。AMD64在本机通过仿真运行，不代表性能测试。

证据保留在 `docs/artifacts/workspace-container-*.json`、`workspace-release-manifest.json`、`workspace-verification.json`；原 `production-*` 文件继续对应v0.6.0。镜像没有推送到远程registry。依赖wheel摘要记录实际构建字节，不构成跨时间可重复供应链锁定的承诺。

## 部署与试点顺序

1. 按第16篇手册注入数据库、加密密钥、Google客户端与HTTPS回调，执行迁移/授权，创建租户并绑定可信Google `sub`。未配置Google时登录页明确显示配置缺失。
2. 启动API与同步/规划Worker。工作台打开 `/`；外部回写和执行默认关闭，先按影子模式验收。
3. 导入主数据、运输配置、需求和库存，批准逐领域/地点/渠道来源，独立导出清单对账。页面支持通用REST配置JSON；生产端点、签名与字段映射仍需按真实系统契约配置。
4. 人工审核冻结计划，核对门禁与数据时效；受控启用执行后发布。需要外部创建单据时，另行配置写回连接与独立写回开关。
5. 回读或人工提交源系统累计执行证据；导入执行后的完整库存，独立清单携带 `execution_refs`；结算源/目的端后关闭，再生成下一轮。

数据库无新增表结构，本轮不增加迁移。上传使用持久化inbox卷；只读根目录不能替代该卷。回滚至旧版本前先停止新发布，保留已发布计划和执行事实，不能直接删除预占记录。

## 明确边界与下一阶段

- 本轮完成前端与生产服务统一；真实Google公网验收、Amazon/TikTok真实授权及逐市场/WMS/ERP联调仍待具备真实凭据与供应商契约。Mock本轮验证不是全部站点的商业授权覆盖证明。
- 当前业务API要求用户拥有全地点范围；受限地点账号会被拒绝，不会静默扩大范围。细粒度地点工作台仍需实现并专项验收。
- 主目录首次加载500个SKU/地点，名称未加载时回退到代码；连接列表最多500项，计划/批次/任务/隔离记录仅最近100条。大租户仍需服务端检索、分页与异步报表。
- 同步交互回测限制1000条需求序列；更大规模应扩展异步评估、结果缓存和容量基准。目前未完成企业级并发/长稳测试。
- 来源差异细节、主数据映射、历史回填、PO在途关联和用户管理API已有，但尚无完整管理界面；本轮对账页面显示状态与批次范围，复杂差异处置仍需API/运维。
- 发布与执行门禁由服务端强制；前端禁用按钮只是辅助。需要继续做真实计划员UAT、权限矩阵浏览器验收、可访问性专项审查、断网恢复与生产监控告警。
