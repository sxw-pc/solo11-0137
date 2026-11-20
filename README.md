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
python -m portbook report --db run.db
python -m portbook audit --db run.db
```

每个入口可用同名 `-bad.json` 观察拒绝、待证或冲突。`resource-plan` 还允许 `formal=false` 的匿名合成申请独立计算容量；正式确认必须引用已采信公告和安全窗。异常证据不会覆盖原始记录。

## 四账的决定

| 账 | 独立结果 | 跨账影响 |
| --- | --- | --- |
| 航次公告 | 签发资格、并列修订冲突、撤回或取消 | 所有修订撤回后保留 `withdrawn` 状态并释放未实靠计划 |
| 航道安全窗 | 潮深、测站基准、封闭令、连续采样 | 窗口变化使受影响计划待证 |
| 资源调度 | 泊位独占、拖轮功率与班次、候补 | 冻结公告和安全窗版本供履约核对 |
| 现场履约 | 双主体见证、实靠、迟到失权 | 未实靠失权释放资源；实靠或相互矛盾的港口见证保留争议；导入或订正实靠区间即重算岸电归属 |
| 岸电计量 | 专属表独立累计读数；共用变压器按实靠区间分段分摊 | 表计回卷、同时刻冲突、缺复表只污染相关时段；超签约上限或分摊依据不足只给范围与待证 |

`evidence` 保存事件的规范化原始 JSON 字节与 SHA-256；CLI 还保存整个 fixture 文件原始字节。同一事件同内容重放幂等，异内容报错。`decision_log` 留下每次状态转换；`report` 输出当前裁定和证据链。所有写入通过 SQLite 事务提交。

`audit` 按 CLI 输入文件的原始字节与顺序在全新内存数据库重放四账，比较当前公告、安全窗、分配、履约和实靠状态；仅直接调用 Python 模块而未通过 CLI 输入的数据库没有源文件日志，会明确返回 `replay_ok=false`。

## 岸电两本账

岸电证据义务按合同类型区分，`power-import` 同时接受船舶专属表 `readings` 与共用变压器数据（`transformers`、`contracts`、`transformer_readings`、`outages`）：

- **专属电表合同（`dedicated`）**：只接受本船表计的首末累计读数，混表号判 `disputed`，缺开闭锁读数判 `pending`；共用变压器读数不会被默默按比例替代专属实测。
- **共用变压器合同（`shared`）**：仅在每船已签发负荷上限 `load_cap` 与分摊权重 `weight`、变压器读数可信、且 `arrival` 记录的实际接电区间齐备时，才按权重分摊。变压器增量先按接电边界切分段，每段分给船舶与码头（含 `dock` 基荷与 `unallocated` 待证）的电量恰好等于该段可信增量；超出负荷上限的余量不重新指派，只在 `power_account.kwh_min/kwh_max` 给范围并标记 `pending`。

表计回卷、同一时刻冲突读数、断电后缺少复表读数只令相关区间不可信（`power_segment.trusted=0`），此前已核实分段保留。现场补证扩大接电区间时，`arrival-import` 自动触发 `power.decide` 重算同一航次及同变压器其他航次，码头基荷或待证电量随之改归船舶并改写账单。合同、表计、接电起止、权重与逐段归属分别落账于 `power_contract`、`power_transformer`/`transformer_reading`、`occupancy`、`power_segment` 与 `power_attribution`；原始抄表字节始终封存于 `evidence`，泊位计划不能代替实际接电。

当前限制：潮位安全窗使用 fixture 中的连续采样点和明确的起止区间，不推算缺测时段；单机 SQLite 调度，不提供真实 AIS 或港口外部接口。窗口申请按固定优先级和时间裁定，已确认分配不自动被更高优先级申请抢占。
