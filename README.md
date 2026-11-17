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
| 现场履约 | 双主体见证、实靠、迟到失权 | 未实靠失权释放资源；实靠或相互矛盾的港口见证保留争议 |

`evidence` 保存事件的规范化原始 JSON 字节与 SHA-256；CLI 还保存整个 fixture 文件原始字节。同一事件同内容重放幂等，异内容报错。`decision_log` 留下每次状态转换；`report` 输出当前裁定和证据链。所有写入通过 SQLite 事务提交。

`audit` 按 CLI 输入文件的原始字节与顺序在全新内存数据库重放四账，比较当前公告、安全窗、分配、履约和实靠状态；仅直接调用 Python 模块而未通过 CLI 输入的数据库没有源文件日志，会明确返回 `replay_ok=false`。

当前限制：潮位安全窗使用 fixture 中的连续采样点和明确的起止区间，不推算缺测时段；单机 SQLite 调度，不提供真实 AIS 或港口外部接口。窗口申请按固定优先级和时间裁定，已确认分配不自动被更高优先级申请抢占。
