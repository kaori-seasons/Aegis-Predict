# v0.10.0 外部市场信号：发布时间、修订历史与无未来引用回测

日期：2026-11-17。接入流仍为 `market_signals`，支持通用REST、CSV/XLSX及新增 `mock_market`。市场增强预测版本为 **`market-aware-v4`，按不可变策略显式启用**；生产默认仍为v2，v2/v3冻结计划不更换算法。没有默认接通付费数据商或真实平台市场洞察API。

## 数据契约与兼容边界

原 `market_signal` 是覆盖式投影，没有足够的发布时间和接收历史；它保留用于旧数据兼容，**不迁移成虚构的历史版本、不进入v4**。新流规范化后写入只追加的 `market_signal_revision`，新迁移头为 `0006_market_revisions`。

| 字段 | 规则 |
| --- | --- |
| `sku`, `location`, `platform` | 必填；商品/仓库必须映射；provider由连接的`source_system`标识，不接受记录自行覆盖 |
| `date` | 信号对应目标日，可以是未来计划日期；不同仓库的预测起点使用其本地日历和时区 |
| `signal_kind` | `planned`预先公布的计划，或`observed`事后实际指标；v4本轮只使用planned，observed归档但不充当未来特征 |
| `revision` | 必填正整数；唯一键包含租户、来源、商品、仓库、平台、种类、目标日和版本；必须发布新版本才能更改内容 |
| `published_at` | 必填、明确时区的ISO时间；不能晚于服务器当前时间；未来目标日可提前发布planned；observed须在目标仓库本地日结束后发布（含DST） |
| `received_at` | 原始记录首次进入本系统的服务器时间；不由CSV/REST请求指定 |
| `available_at` | 至少为发布时间、接收时间、规范化时间三者最大值；不接受外部回填伪造过去可用时间 |
| `is_deleted` | 默认false；新版本为true表示撤回，当时已可用的最新撤回阻止回退到更早旧值 |
| 五个指数 | 广告投入、促销、季节、平台活动、价格；默认1，有限非负数，价格须大于0；单位为相对指数而不是货币 |

每个修订都是完整信号快照，省略的指数使用默认1，不是局部PATCH。同一版本同一内容重复投递不会修改首次可用时间；同版本异内容进入`market_revision_conflict`隔离，不能用last-write-wins覆盖。无发布时间/版本的新导入会被拒绝或隔离，旧导出文件须按新契约更新。只读历史API保留所有修订和撤回，未自动替用户猜测供应商发布时间。

表采用租户RLS、复合租户外键、应用角色SELECT/INSERT权限和禁止UPDATE/DELETE触发器。即使owner路径也不能修改某条历史修订；纠错采用新revision。不得通过UPDATE修复已落库证据。

部署时先运行`aegis db upgrade`，再运行`aegis db grant`更新应用角色权限，之后启动API/Worker；新版本启动会检查迁移头。不要把生产降级迁移当作数据回滚：删除修订表会丢失归档，回退模型应创建使用v2/v3的新策略并保留修订档案。

## 时间点读取与冻结

```mermaid
flowchart LR
    A[REST / CSV / 市场Mock] --> B[原始记录与首次接收时间]
    B --> C[映射、发布时间校验、版本冲突隔离]
    C --> D[只追加的修订表 / 服务器可用时间]
    D --> E[按策略选择一个来源和平台]
    E --> F[冻结截止时已可用的全部历史版本]
    F --> G[每个内外层起点先过滤三种时间]
    G --> H[再按目标日选最高revision / 应用撤回]
    H --> I[训练内比较 / 信号不足回退v3]
    I --> J[同折比较v3、固定市场回归、v4门禁版本]
```

- 每个预测起点先要求 `published_at <= origin`、`received_at <= origin`、`available_at <= origin`，**过滤后再选最新修订**。修订号更大但更晚可得的记录不能遮住当时旧版本。
- 冻结输入新增`market_signal_revisions`，含三种时间、版本、种类、来源和行ID；旧的静态`market_signals`示例结构继续供仿真兼容。新流已经进入生产预测，不能再把静态示例信号直接当成生产历史。
- 历史回测不读取实时表；内外层均从冻结版本列表重建自己的as-of视图。每个内层结束时间不超过当前外层训练截止；修订摘要和选择证据写入结果。
- 首次导入过去几个月的信号，系统可用时间仍是今天。今天之前的回测必须回退，不能将provider声称的旧发布时间等同于本系统过去已接收。
- 策略必须指定一个活动连接的`source_system`和一个platform；不同供应商不平均混合。冻结上限100000条修订，超过显式拒绝；交互回测继续限100条序列/30000日观察，离线批量使用CLI。
- 模型版本和策略保存在冻结输入中。新导入、更晚修订、策略新版本不会改变已冻结计划的复算；原缺少model_version的旧计划继续走v2。

## 市场增强算法

固定候选为一套岭回归：最近112天内有效观察，至少56个有信号训练日，训练/未来目标日信号覆盖各至少80%；五个指数取裁剪后的对数，加入星期与线性时间项。中心、尺度和系数只从训练前缀计算，岭系数固定为3，不按外层分数调参。未来缺失信号日沿用v3预测，不填入事后实际值。

v4在训练前缀内比较这套固定市场回归和原v3。最多最近4个有效、不重叠、与预测周期相同的内层窗，至少3窗；MAE须降低2%以上且过半窗获胜，才采用市场回归，否则沿用v3。没有日历、没有可信时间、覆盖不足或信号无变化也回退。外层评分不能倒流到该折的拟合或选择。指标是预测关系，不解释为广告或促销的因果收益。

分位以新点预测为中心，沿用v3历史波动的正态近似；置信分数仍为数据质量启发式，尚未进行市场模型残差校准。本轮重点是点预测与数据可得性，不能把P95解释为已验证的库存履约承诺。真促销计划、预先发布的价格计划可以接入；事后流量、广告实际消耗、竞品实际价格不能冒充未来计划。observed的滞后特征和未来外生变量自身预测仍属后续工作。

## 接入前后比较

全部起点（主要结果）：

| 数据 | 周期 | 有效折 | 接入前v3 WAPE | 固定市场回归 WAPE | 门禁后v4 WAPE | 相对v3改善 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 固定合成矩阵 | 7天 | 416 | 7.222% | 4.257% | 5.093% | +29.48% |
| 固定合成矩阵 | 14天 | 208 | 5.313% | 3.356% | 4.122% | +22.41% |
| 固定合成矩阵 | 30天 | 96 | 4.139% | 2.786% | 3.681% | +11.05% |
| 仓库无信号对照 | 7天 | 1260 | 5.511% | 5.511% | 5.511% | +0.00% |
| 仓库无信号对照 | 14天 | 600 | 6.378% | 6.378% | 6.378% | +0.00% |
| 仓库无信号对照 | 30天 | 300 | 8.008% | 8.008% | 8.008% | +0.00% |

最近8个起点的合成矩阵：

| 周期 | v3 WAPE | v4 WAPE | 相对改善 |
| --- | ---: | ---: | ---: |
| 7天 | 6.821% | 3.990% | +41.50% |
| 14天 | 5.193% | 3.260% | +37.22% |
| 30天 | 4.139% | 3.681% | +11.05% |

全窗口分组（保留负改善）：

| 场景 | 周期 | v3 WAPE | v4 WAPE | 相对改善 |
| --- | --- | ---: | ---: | ---: |
| 有效计划 | 7天 | 8.612% | 4.072% | +52.72% |
| 晚到历史 | 7天 | 7.326% | 7.326% | +0.00% |
| 事后修订 | 7天 | 9.249% | 5.546% | +40.04% |
| 噪声信号 | 7天 | 2.997% | 3.045% | -1.60% |
| 有效计划 | 14天 | 6.299% | 4.129% | +34.46% |
| 晚到历史 | 14天 | 5.294% | 5.294% | +0.00% |
| 事后修订 | 14天 | 7.140% | 4.679% | +34.47% |
| 噪声信号 | 14天 | 1.960% | 2.015% | -2.80% |
| 有效计划 | 30天 | 5.465% | 4.406% | +19.37% |
| 晚到历史 | 30天 | 3.593% | 3.593% | +0.00% |
| 事后修订 | 30天 | 5.626% | 4.926% | +12.45% |
| 噪声信号 | 30天 | 1.430% | 1.427% | +0.23% |

噪声组7天、14天分别退化约1.60%、2.80%。训练内门禁不能保证未来永远胜出，故不因总指标改善而默认推广v4，也不按这些外层结果重调参数。

“接入前”为同一批外层窗的v3，“接入后”为经过训练内门禁的v4。相对改善为`1-v4绝对误差/v3绝对误差`，不是WAPE百分点。比较同时保留固定市场回归，避免只报告门禁版本。完整13模型、分场景、分位覆盖率、回退次数见[最近8个起点](artifacts/market-signal-comparison.json)与[全部起点](artifacts/market-signal-comparison-all.json)。

合成矩阵为16条×210天，4类情形（有效计划信号、噪声、晚到历史、事后修订），固定种子101/103/107/109；时间元数据是明示的模拟可得时间。未向生产导入伪造历史接收时间。仓库样例60条×180天没有可信信号历史，v4必须与v3完全一致，改善为0。这些是验证夹具，不是商家真实精度或上线收益证据；分组退化也保留，不因整体改善就自动推广。

## 接入与试点操作

CSV最小示例：

```csv
sku,location,date,platform,signal_kind,revision,published_at,promotion_lift
S,W,2026-12-01,amazon,planned,1,2026-11-17T00:00:00Z,1.8
```

发布时间须为供应商实际发布时间，日期仅示例，不能在更早时间上传尚未发布的记录。CSV/XLSX进入本租户inbox，复用文件上传与Worker同步；不要提交received_at或available_at字段。

通用REST配置例（按供应商字段可补`field_map`；认证密钥经连接密钥接口注入）：

```json
{"base_url":"https://your-provider.example","streams":{"market_signals":{"path":"/signals","records_path":"items","updated_at_field":"published_at","updated_since_param":"published_since","updated_until_param":"published_until","pagination":{"type":"cursor","next_cursor_path":"next_cursor"}}}}
```

源接口必须返回全部修订而非只保留最新值；增量分页、回填窗口和发布游标需逐供应商联调。历史归档不能仅凭updated_at猜测first available。真实REST仍保留同源、HTTPS、SSRF和认证保护；测试使用MockTransport，不连接上述示例域名。

在数据源管理创建“市场信号 Mock”，或调用：

```json
{"name":"market-pilot","kind":"mock_market","config":{"sku":"S","location_code":"W","platform":"amazon","start_date":"2026-08-01","days":180}}
```

Mock不需要商家OAuth，不访问网络；产生预发布计划和后续修订，通过相同原始落库/校验/规范化管道，系统接收时间仍为今天。Amazon/TikTok既有Mock保留。市场Mock不出现商家Mock授权按钮，也不会使用仅适用平台Mock的独立清单动作；真实执行拒绝混合Mock市场源与真实库存源。

保存现有完整策略配置，再只修改以下三个字段，通过`POST /api/v1/policies`创建新版本（需策略编辑权限、全局范围和CSRF）：

```json
{"forecast_model_version":"market-aware-v4","market_signal_source":"mock_market:market-pilot","market_signal_platform":"amazon"}
```

上面仅列出需修改的字段，不能把它当作保留其他配置的PATCH；该接口省略字段会使用默认值。先做shadow计划，通过租户历史回测、真实授权/字段验收、分组稳定性与分位校准后再考虑执行推广。默认仍为v2。

历史接口：`GET /api/v1/data/market-signal-revisions?source_system=...&platform=amazon&as_of=2026-11-17T00:00:00Z&limit=100&offset=0`，返回历史而非仅最新版本；省略as_of看当前归档，最大每页500条。需要数据查看权限与全局范围，租户由会话决定，不能跨租户指定。带时区参数应通过客户端URL编码。

评估接口：`GET /api/v1/workspace/plans/{id}/evaluation?horizon=7`。v4返回13模型和`market_comparison`（相对v3配对指标）、`market_selection_counts`（通过/回退原因）。前端新增接入前后表；数据源页显示商品、仓库、版本和三种时间，默认10条，可展开本页其余记录。

```bash
python scripts/benchmark_market_signals.py
python scripts/benchmark_market_signals.py --max-folds 0 \
  --output docs/artifacts/market-signal-comparison-all.json
# 对包含calendar_start/timezone及market_signal_revisions的冻结network文件
PYTHONPATH=src python -m aegis.cli backtest --path /tmp/frozen-market-network.json \
  --model-version market-aware-v4 --horizon 7 --output /tmp/market-backtest.json
```

## 验证范围与限制

本轮全仓库159项通过、0失败、0跳过；既有Starlette/httpx弃用警告1项。新增测试覆盖：未来销量扰动、内层时间边界、晚发布/接收/可用的修订扰动、事后实际指标和其他SKU污染、撤回、乱序、来源歧义、无元数据精确回退、本地时区/DST、重复/冲突、伪造服务器时间隔离、Mock/REST/CSV全管道、API权限/RLS、owner/应用角色不可变，以及新修订后旧计划复算一致。

发布时间的供应商真实性仍需外部审计；系统只能可信记录自身接收与规范化。需求事实表的全部历史修订仍未重建；本轮证明的是市场信号的可得时间与冻结序列训练/选模边界，不能声称完整业务数据已经通过历史point-in-time验收。第三方数据授权、供应商历史完整性、额度、迟到监控和真实商家效果还需逐源验收。

方法参考：[预测外生变量可得性](https://otexts.com/fpp3/dynamic-regression.html#forecasting)与[PostgreSQL租户行安全](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)。具体回退和门禁为本项目固定实现，不等同于教材提出的模型。

浏览器流程、6组布局检查及截图见[工作台验证](../design-previews/2026-11-17-market-signals/DESIGN.md)。Linux ARM64/AMD64镜像构建、非root/只读运行、0006迁移及v4回测的最终结果见`artifacts/market-container-*.json`；AMD64为本机仿真，未进行容量测试。源码与验证记录为`market-release-manifest.json`和`market-verification.json`。
