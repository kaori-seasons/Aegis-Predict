# 0.6.0 生产闭环、Google认证与接入手册

更新：2026-11-16，Asia/Shanghai。作者：kaori-seasons，邮箱：yuluoxinsheng@gmail。

本轮根据用户最新约定：**用户认证默认Google OIDC；Amazon/TikTok授权与数据默认Mock，凭据、真实域名与市场联调留作配置和后续验收。** 本文取代旧文档中“预占尚无消耗/释放/结算”及“本机无法构建Linux镜像”的实施状态；原风险评估仍保留作历史基线。

## 1. 已实现

| 工作包 | 实现与约束 |
| --- | --- |
| Google认证 | 授权码+PKCE，state与HttpOnly浏览器随机量双重绑定，10分钟过期；交换前先持久化消费state，失败须重新开始；RSA/RS256验签、issuer/audience/azp/exp/iat/nonce/email_verified及可选hd验证 |
| 成员身份 | `external_identity`按租户与Google subject绑定已有成员；邮箱相同不自动合并；成员或租户停用立即拒绝登录；成功后签发现有可撤销会话与CSRF令牌 |
| 平台Mock | `mock_amazon`、`mock_tiktok`，复用真实连接器解析代码；内存HTTP传输，无外部网络回退；官方结构夹具覆盖有效/未支付/取消、Amazon分页与FBA库存 |
| 独立源对账 | 批次精确scope，记录键+源版本+payload对比，缺失/多出/内容差异与摘要；同条数不同内容仍失败；一份清单最多50,000条/10MB，较大源需按批次分区 |
| 实物执行 | 累计发运、收货、取消与单调源版本；数量守恒、幂等与冲突检查；不将创建单据confirmed当成实物完成 |
| 结算与关闭 | 发运后保持库存扣减；源库存快照对账明确包含该单据累计量后消除相应重复扣减；有收货还需目的仓快照；最终结算后允许关闭并发起下一轮 |
| WMS/ERP回读 | Generic REST标准累计量回拉；连接级租约/心跳/围栏、每个预占60秒轮询间隔、单据身份核对、最终事件停止轮询；传输错误不会释放库存 |
| 外部歧义 | 创建响应丢失后只查单；查不到继续保持sent_unconfirmed，不自动重新创建；达到重试预算进入人工处理 |
| 镜像 | 固定基础镜像digest，构建wheelhouse，运行阶段离线安装；保存wheel SHA256摘要；UID10001、只读根文件系统、/tmp临时写入；提供独立容器验收脚本 |

新增数据库版本为`0004_execution`：五张租户表及连接Mock标记、预占累计计数和轮询时间字段。新增对账、执行、结算表通过权限与数据库触发器保持不可修改；所有租户引用使用复合外键并开启RLS。

## 2. 配置与首次Google登录

```dotenv
AEGIS_AUTH_MODE=google
AEGIS_GOOGLE_CLIENT_ID=
AEGIS_GOOGLE_CLIENT_SECRET=
AEGIS_GOOGLE_REDIRECT_URI=
AEGIS_GOOGLE_ALLOWED_DOMAINS=[]
AEGIS_INTEGRATION_MODE=mock
AEGIS_REQUIRE_RECONCILIATION=true
AEGIS_PLAN_EXECUTION_ENABLED=false
AEGIS_WRITEBACK_ENABLED=false
```

Google Cloud创建Web客户端，回调精确配置为`https://实际域名/api/v1/auth/google/callback`。开发环境仅允许localhost、127.0.0.1、testserver的HTTP回调。客户端secret通过部署环境/密钥管理注入，不写进仓库。

首次成员绑定：

```bash
aegis tenant-create --slug acme --name Acme --email admin@example.com
aegis identity-bind --tenant TENANT_UUID --email admin@example.com --google-sub VERIFIED_GOOGLE_SUB
```

subject必须由可信Google身份核验获得，不根据邮箱猜测。管理员也可用`POST /api/v1/users/{id}/google-identity`绑定。未实现开放注册、自动邮箱绑定或自助邀请。`hybrid`/`password`必须显式配置后才能使用原密码API；默认Google模式返回403。

登录接口：

1. `POST /api/v1/auth/google/start {"tenant":"acme"}`，设置短期HttpOnly/Lax绑定cookie并返回Google授权URL。
2. 浏览器前往Google，返回固定callback；服务端消费state、交换code并验签。
3. 查找该租户已绑定的subject，签发会话，303跳转固定站内`/`。没有任意return_url或URL令牌。

应用默认关闭Uvicorn访问日志，防止回调查询参数中的code/state进入URL日志。反向代理同样必须记录不带查询串的`$uri`，不得记录`$request_uri`或完整请求行；公开入口还需配置身份接口限流、HTTPS与安全访问边界。

## 3. 平台Mock与对账

Amazon Mock配置遵循`AmazonConfig`；TikTok Mock配置遵循`TikTokConfig`，均使用真实市场目录校验。对Mock连接调用`POST /api/v1/connections/{id}/mock-authorize`生成只用于本地传输的合成凭据。

夹具版本`official-projection-v1`，订单时间固定为2026-10-01 12:00 UTC：

- Amazon：UNSHIPPED 5件、PENDING 2件、CANCELLED 1件；两页订单；FBA可售120件、在途2+3+1件。有效需求为5件，reserved不重复扣除。
- TikTok：AWAITING_SHIPMENT、UNPAID、CANCELLED各1行；有效需求为1件。
- SKU统一为`S`，接入前须导入该商品和目标仓映射；回填窗口需覆盖夹具时间。此夹具是契约样本，不是多月销量分布或实际商家录制证据。

`aegis import-sample`的完整网络样例也始终标记为Mock，即使部署模式为live；使用独立源网络逐批自动对账，只允许作为合成数据证据。

批次完成后：

```text
GET  /api/v1/data/batches/{id}/mock-source-manifest
POST /api/v1/data/batches/{id}/reconcile
GET  /api/v1/data/reconciliations
```

Mock源清单从夹具重新读取并解析，不从raw表复制期望值。真实源清单由WMS/ERP或独立平台导出提供，必须包含：

```json
{
  "source_rows": [
    {"external_id":"SOURCE-ROW-ID","payload":{"sku":"S","location":"W","on_hand":120},"source_updated_at":"2026-11-16T00:00:00Z"}
  ],
  "scope": {},
  "evidence":"独立导出文件/请求/业务验收编号",
  "execution_refs": {}
}
```

`payload`需对应系统落地的规范投影字段，`scope`必须与该批次完全一致；源记录版本须一致。不能把其他站点或另一个时间窗口的相同条数当作通过。scope保留账户、市场、地点、读取时钟和Mock标记。最新权威批次必须有matched结果才能执行发布；live模式拒绝Mock结果。

证据上传者需具有连接管理权限。当前不验证供应商数字签名；自动从各供应商独立导出/报表取数、长期连续对账和差异工单仍需按具体供应商接入。

## 4. 预占、执行与结算

### 累计量语义

对预占数量Q：

- `shipped + cancelled <= Q`
- `received <= shipped`
- 各累计量和版本只能前进，原单据/来源不能改绑。
- `is_final=true`要求`received + cancelled == Q`。
- 当前库存扣减为`Q - released_units - settled_units`。`released_units`来自确认取消；`settled_units`来自已反映发运的源快照结算。

发运量计入consumed，但在源快照结算前继续扣减，避免旧库存快照重复分配。取消已发运单位不被接受；退货需独立业务事实，不以倒退计数表示。

### 执行记录

```text
POST /api/v1/inventory/reservations/{id}/execution
```

```json
{
  "connection_id":"执行来源UUID",
  "source_version":1,
  "external_ref":"WMS-TRANSFER-123",
  "shipped":30,
  "received":20,
  "cancelled":0,
  "is_final":false,
  "occurred_at":"2026-11-16T00:00:00Z",
  "evidence":"源系统读回凭据"
}
```

存在writeback连接时只能接受该连接的执行证据，且所有创建命令必须先confirmed；单命令的external_ref必须一致。没有writeback时，人工执行证据须来自源库存池的权威系统。源版本相同而内容不同拒绝；事件时间不得早于发布或晚于当前时间。

### 库存结算

先完整导入执行后库存快照，并在源清单中明确声明：

```json
{"execution_refs":{"WMS-TRANSFER-123":{"shipped":30,"received":20,"cancelled":0}}}
```

随后调用：

```text
POST /api/v1/inventory/reservations/{id}/settle
```

```json
{"source_batch_id":"源仓最新完整批次UUID","destination_batch_id":"目的仓最新完整批次UUID","evidence":"该单据已反映于两端库存"}
```

两个批次需为对应权威来源的最新完整批次、SKU行存在、as_of不早于事件且最新独立对账matched。发生收货时目的仓必需；只发运未收货可先结算源端，最终事件仍须再次对账。终态不可修改；每个观察事件的结算幂等。

所有预占终态、计数守恒、最新事件已结算后，计划才可close。保留一个活动计划的保护；本轮实现的是顺序滚动闭环。

## 5. 自动执行回拉契约

Generic REST连接新增：

```json
{"execution_readback":{"path":"/execution","query_param":"external_ref","identity":"external_ref","response_path":"data"}}
```

只读GET，使用该连接原有鉴权、网络策略、重试和超时。响应标准化为：

```json
{"data":{"source_version":1,"external_ref":"WMS-TRANSFER-123","shipped":30,"received":20,"cancelled":0,"is_final":false,"occurred_at":"2026-11-16T00:00:00Z","evidence":"WMS回读请求编号"}}
```

没有数据返回`{"data":null}`；不能用空对象表示全量取消。运输拆为多个创建命令时必须配置`identity=plan_line_id`，由源适配层提供整条计划行汇总，external_ref为`line:<plan_line_uuid>`。不能把多个单据的累计量直接叠加在同一个预占上。

sync-worker同时处理创建与回拉；轮询身份不匹配/数量超额会拒绝且保留原库存扣减。生产配置必须明确单据唯一键、查单可见性、累计版本、超时及取消语义。

## 6. 验证证据

最终结果以本节补充记录及`docs/artifacts/`为准。

- 全量PostgreSQL 16回归：**108项通过、0失败、0跳过**（64.63秒）；有1条现有Starlette/httpx测试客户端弃用警告。
- Google签名与声明校验、PKCE/浏览器绑定、callback重放和未绑定subject拒绝；测试使用本地RSA签名与Mock Google端点，未进行真实Google账户登录。
- 官方结构平台Mock端到端：分页、订单状态、规范投影、独立清单对账。
- 库存：部分发运、取消、收货、对账结算、关闭与下一轮；超额、版本冲突、跨租户与不可变证据。
- Linux ARM64原生与AMD64仿真：**最终0.6.0镜像均构建并运行通过**；容器内0004迁移、UID10001、只读根文件系统、API live/ready、Google默认配置、Mock模式与算法回测均通过。验收容器和网络已自动清理。
- 证据：[ARM64容器验收](artifacts/production-container-arm64.json)、[AMD64容器验收](artifacts/production-container-amd64.json)、[测试记录](artifacts/production-verification.json)。两种镜像源码digest相同；尚未推送镜像仓库或部署外部生产环境。

## 7. 仍需外部或后续验收

真实Google客户端及HTTPS域名配置、真实商家应用审核/授权、逐市场实际fixture与源端长期对账、具体WMS/ERP契约、真实UAT和经营周期验证仍未发生。Mock不自动升级为live批准。

全量订单行替换/删除、退款退货事实、双时间模型、并行计划替代、制造MRP、配额计费/公平调度、完整辅助技术审计与企业恢复/容量演练仍列在README路线图。当前业务可视化仍是独立仿真前端；Google登录服务尚未与五个页面统一。
