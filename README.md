# 治理晋级评议与申诉协作基础服务

本项目提供文化创意赛事与成果转化业务共享的服务端基础能力，负责项目机构、业务节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域模块可以在这些稳定边界上扩展自己的状态、规则和接口。

## 目录

- src/creative_program_foundation/：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- tests/：基础规则、事务边界、接口路由和端到端验收测试。

## 晋级评议与申诉模块

在基础服务之上实现了完整的晋级评议与申诉能力（`review_compute.py` 纯计算、`review_service.py` 领域服务、`review_api.py` 路由、`review_acceptance.py` 离线验收）：

- **规则版本与冻结**：每个阶段可登记多版评分细则（维度权重、迟交/缺评/撤销/并列策略、申诉时限），冻结时选定一版并按其密封分数——迟交分数按冻结规则拒绝或采纳，此后并列、缺评、评分撤销与资格取消均按该冻结规则处理；
- **评分事实**：分数、扣分依据、资格取消、有效评委集合、赛道配额与跨赛道奖项约束（晋级总数、奖项总量）都是持久化事实，撤销与更正只追加新状态，从不原地改写；
- **候选榜与会签**：管理侧生成可复核候选榜（含输入快照哈希与逐作品解释），复核人与发布人必须相互独立且均不得为生成人，双人会签后由发布会签人执行发布；
- **版本化榜单**：发布后榜单不可改写；事实变化或申诉裁决产生新版本，新版本说明哪些名次、名额和后续通知受到影响，旧通知自动标记为受影响；
- **申诉**：参赛人在申诉时限内引用具体评分或资格事实提出申诉，裁决维持时应用纠正措施（采纳迟交分、更正分数、撤销评分、撤回扣分、解除资格取消、恢复/排除评委）并自动生成新候选榜；
- **可见性**：参赛人只能查询自己的明细和公开结果，评委看不到其他人的未封存分数，候选榜仅治理角色可见；
- **管理 API**：`GET /review/rankings/{id}/replay` 重放榜单计算（快照重放核对 + 当前事实漂移检测），`GET .../entries/{id}/explain` 解释每个晋级或落选原因，`GET /review/pending` 在服务重启后继续未完成的会签与申诉时钟。

新增角色：`participant`（参赛人）、`judge`（评委）、`publisher`（发布人）。主要接口前缀为 `/review/`，写入接口同样要求 `request_id` 幂等键与 `X-Actor-Id`。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m creative_program_foundation.acceptance
    PYTHONPATH=src python3 -m creative_program_foundation.review_acceptance

第一条命令验收基础登记链；第二条命令在临时 SQLite 数据库中走通三赛道初赛全流程：两版评分细则、迟交评委分数被拒、末位同分决胜、名额与奖项总量约束、会签发布、通知、申诉裁决产生第二版榜单（含名次/名额/通知影响说明）、榜单重放，以及服务重启后申诉时钟的续跑。成功时各输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m creative_program_foundation.api --database creative_program.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。
