# 港航四账台

离线港口证据工作台。航次公告、航道安全窗、泊位与拖轮联合分配、现场靠泊履约各有独立入口和持久化状态。所有样例均为虚构数据，不访问网络。

## 运行

在仓库根目录执行：

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m portbook demo --db demo.db
python -m unittest discover -s tests -v
```

单独运行四本账：

```powershell
python -m portbook notice-import --db run.db --file fixtures/notices-ok.json
python -m portbook waterway-import --db run.db --file fixtures/waterway-ok.json
python -m portbook resource-plan --db run.db --file fixtures/resources-ok.json
python -m portbook arrival-import --db run.db --file fixtures/arrival-ok.json
python -m portbook pollution-import --db run.db --file fixtures/pollution-qc-ok.json
python -m portbook report --db run.db
python -m portbook audit --db run.db
```

每个入口可用同名 `-bad.json` 观察拒绝、待证或冲突。`resource-plan` 还允许 `formal=false` 的匿名合成申请独立计算容量；正式确认必须引用已采信公告和安全窗。异常证据不会覆盖原始记录。

## 污染采样双轨台账

`pollution-import` 把值班预警与实验室正式签发分开存储，任何一条轨道都不覆盖另一条：

| 轨道 | 表 | 依据 | 状态 |
| --- | --- | --- | --- |
| 值班预警 | `pollution_cases` | 最新原始 reading 直接对签发 ceiling，不做质量修正 | `exceeded` / `within_limit` / `pending` / `disputed` |
| 正式签发 | `pollution_verdicts`（旧签发入 `pollution_verdict_history`） | 同批标准液反推、空白、加标回收、平行样、复测、授权时段 | `exceeded` / `within_limit` / `indeterminate` / `review_required` / `pending` |

离线采录的同批标准液实测值、仪器校准授权时段、空白、加标回收、平行样与标准液复测各自指向样本、批次、指标、单位和签发者。正式读数从标准液实际测量反推：仅接受斜率为正、残差在批允差内的单调校准解释；异常标准点只有在独立复测证据支持时才能隔离或用复测值替代，分数按保留的证据点数计，杜绝为凑结论随意丢点。若两套最大证据量的解释都满足质控、却分别推出必然超限与允许不超限，正式结论为 `review_required` 并同时展示两套证据，不按文件顺序择一。

空白与加标回收在联合可行域上约束浓度：空白响应同时从样本与加标测量中扣除，回收带约束加标增量，极值在联合端点上取，不把每项误差独立取极值拼成物理上不可能的最坏值。校准样本与正式样本跨仪器、跨签发批次或超出授权时段一律不混用；单位不一致或响应落在检出界外同样阻断或修正签发。`report` 的 `pollution_overview` 逐点列出预警与正式签发是 `same`、`different` 还是 `unresolved`，并指向两条轨道分别使用的限值、样本、标准液和质控事件。样本补发或标准液/窗口撤销会重算相关批次，旧签发与源事件仍可追查；同事件同字节重放幂等，异字节整批回滚。

## 四账的决定

| 账 | 独立结果 | 跨账影响 |
| --- | --- | --- |
| 航次公告 | 签发资格、并列修订冲突、撤回或取消 | 所有修订撤回后保留 `withdrawn` 状态并释放未实靠计划 |
| 航道安全窗 | 潮深、测站基准、封闭令、连续采样 | 窗口变化使受影响计划待证 |
| 资源调度 | 泊位独占、拖轮功率与班次、候补 | 冻结公告和安全窗版本供履约核对 |
| 现场履约 | 双主体见证、实靠、迟到失权 | 未实靠失权释放资源；实靠或相互矛盾的港口见证保留争议 |

`evidence` 保存事件的规范化原始 JSON 字节与 SHA-256；CLI 还保存整个 fixture 文件原始字节。同一事件同内容重放幂等，异内容报错。`decision_log` 留下每次状态转换；`report` 输出当前裁定和证据链。所有写入通过 SQLite 事务提交。

`audit` 按 CLI 输入文件的原始字节与顺序在全新内存数据库重放各账（含 `pollution-import`），比较当前公告、安全窗、分配、履约、实靠状态与污染双轨台账；仅直接调用 Python 模块而未通过 CLI 输入的数据库没有源文件日志，会明确返回 `replay_ok=false`。

当前限制：潮位安全窗使用 fixture 中的连续采样点和明确的起止区间，不推算缺测时段；单机 SQLite 调度，不提供真实 AIS 或港口外部接口。窗口申请按固定优先级和时间裁定，已确认分配不自动被更高优先级申请抢占。
