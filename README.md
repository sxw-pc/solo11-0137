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
python -m portbook group-dispatch --db run.db --file delegations.json
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
| 现场履约 | 双主体见证、实靠、迟到失权 | 未实靠失权释放资源；实靠或相互矛盾的港口见证保留争议 |

`evidence` 保存事件的规范化原始 JSON 字节与 SHA-256；CLI 还保存整个 fixture 文件原始字节。同一事件同内容重放幂等，异内容报错。`decision_log` 留下每次状态转换；`report` 输出当前裁定和证据链。所有写入通过 SQLite 事务提交。

`audit` 按 CLI 输入文件的原始字节与顺序在全新内存数据库重放四账，比较当前公告、安全窗、分配、履约和实靠状态；仅直接调用 Python 模块而未通过 CLI 输入的数据库没有源文件日志，会明确返回 `replay_ok=false`。

当前限制：潮位安全窗使用 fixture 中的连续采样点和明确的起止区间，不推算缺测时段；单机 SQLite 调度，不提供真实 AIS 或港口外部接口。窗口申请按固定优先级和时间裁定，已确认分配不自动被更高优先级申请抢占。

## 一次性成组调度

泊位或拖轮释放后，调度员可在逐条 `promote` 补位之外，先用 `resources.preview_group`（或 `arrival.preview_dispatch`）查看同一时段等待队列的整体可行方案，再通过 `resources.group_dispatch`（CLI `group-dispatch`，文件形如 `{"delegations":[...]}`；亦可随 `arrival-import` 的 `delegations` 字段在同一事务内提交）一次性裁定整组。请求可用 `candidate_ids` 显式指定候选集，或用 `berth`/`tug`/`start`/`end` 圈定等待申请。

两类事先授权的委托是两份不同合同，不是一个排序开关：

| 委托 `kind` | 合同目标 | 裁定规则 |
| --- | --- | --- |
| `emergency` 应急委托 | 优先兑现单船最高等级 | 在通过全部硬约束的候选中取优先级最高（再按开始时间、申请 id 稳定破同） |
| `throughput` 吞吐委托 | 安全边界内兑现最多航次 | 枚举最大可行航序数的组合，同序数再按优先级总和、申请 id 字典序稳定破同 |

硬约束完全沿用已落账的版本资格：公告 `notice_decisions` 必须为 `accepted`、全程必须存在 `safe` 窗口并冻结其版本、泊位水深与拖轮功率/班次满足要求，且任何已确认分配和未结束的现场 `occupancy` 都是不可牺牲的硬边界。组内候选互为占用，整组确认与落选原因（含具体的 `tug occupied`/`berth occupied` 或失效版本原因）作为一次事务写入 `delegations` 与 `delegation_items`，并在 `decision_log` 留痕。任何一船未过硬约束都不会靠挪动已兑现分配或把失效窗口当可用来凑目标。

`delegations` 保存委托类型、证据 SHA、候选集、入选集与理由；同 id 同证据重放返回 `idempotent` 且不会二次确认，同 id 异证据直接报错。`resources.recompute_group` 按当前公告与窗口重算同一委托，返回 `stored_selected_ids` 与新结果及 `changed` 标记，但永不改写已落账的原裁定——公告撤回或安全窗失效后重算结果会变，历史确认保持原样。
