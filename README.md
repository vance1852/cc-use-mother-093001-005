# 治理晋级评议与申诉协作基础服务

本项目提供文化创意赛事与成果转化业务共享的服务端基础能力，负责项目机构、业务节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域模块可以在这些稳定边界上扩展自己的状态、规则和接口。

项目内置的**晋级评议与申诉系统**（`advancement` 模块）面向赛事评议组：每个阶段的规则版本、维度权重、有效评委集合、评分事实、扣分依据、赛道配额和跨赛道奖项约束全部落库；榜单先生成可复核的候选榜，由复核人会签达到法定人数后再由相互独立的发布人发布；发布后的榜单不可原地改写，申诉在时限内引用具体评分或资格事实，裁决成立后以新版本说明受影响的名次、名额与后续通知。

## 目录

- src/creative_program_foundation/：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
  - advancement_schema.py：晋级评议模块表结构；
  - advancement_engine.py：榜单计算纯函数引擎（排名、并列、缺评、奖项分配、版本差异）；
  - advancement.py：规则、评分、会签、发布、申诉与时钟推进的领域服务；
  - advancement_api.py / advancement_acceptance.py：模块路由与离线验收；
- tests/：基础规则、事务边界、接口路由、晋级评议和端到端验收测试。

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
    PYTHONPATH=src python3 -m creative_program_foundation.advancement_acceptance

基础验收在临时 SQLite 数据库中登记项目机构、操作者、业务节点和参考资料，核对幂等回执与审计链。晋级评议验收完整走一遍评议组场景：三个赛道使用两版评分细则、评委迟交分数、末位同分、候选榜会签发布、申诉裁决产生新版本、榜单重放以及服务重启后时钟延续；成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m creative_program_foundation.api --database creative_program.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。

## 晋级评议与申诉系统

### 角色

在基础角色（admin、operator、reviewer、auditor）之上扩展：

- participant（参赛人）：只能查询自己的明细（作品、评分、已发布名次与解释、通知）和公开结果；
- judge（评委）：提交与修改本人未封存的评分，只能看到其他人已封存的分数；
- publisher（发布人）：在复核会签达到法定人数后发布榜单，必须与复核人相互独立。

### 评议流程

1. 配置阶段（会签法定人数、会签时限、申诉时限）、赛道、规则版本（维度权重、并列策略、缺评策略、迟交策略、评分截止、及格线）并冻结，按赛道指派规则版本——三个赛道可以使用不同版本；
2. 设置各赛道晋级名额与跨赛道奖项约束（奖项总量、单赛道奖项上限），指派有效评委集合，登记作品；
3. 评委提交评分（可附扣分与扣分依据），运营封存评分后计算候选榜：并列、缺评、评分撤销与资格取消按冻结时采用的规则处理，候选榜保存完整输入快照；
4. 复核人对候选榜会签，达到法定人数后发布人发布，生成版本号、申诉时限与通知记录；
5. 参赛人在时限内引用具体评分或资格事实申诉；裁决成立后自动重算产生新版本候选榜，并附变更说明（哪些名次、名额和后续通知受到影响），新版本仍需会签与发布，旧版本保持不可改写。

### 主要接口

- 配置：POST /advancement/stages、/advancement/tracks、/advancement/rule-versions、/advancement/rule-versions/freeze、/advancement/track-rules、/advancement/quotas、/advancement/award-constraints、/advancement/judges、/advancement/entries、/advancement/entries/disqualify、/advancement/entries/reinstate；
- 评分：POST /advancement/scores、/advancement/scores/revoke、/advancement/scores/seal，GET /advancement/scores（按角色过滤可见性）；
- 榜单：POST /advancement/runs、/advancement/countersigns、/advancement/runs/publish、/advancement/runs/replay，GET /advancement/runs、/advancement/runs/detail、/advancement/runs/explain；
- 申诉：POST /advancement/appeals、/advancement/appeals/adjudicate，GET /advancement/appeals；
- 查询：GET /advancement/stages、/advancement/stages/detail、/advancement/public/results、/advancement/my/entries；
- 时钟：POST /advancement/tick（推进会签与申诉时钟，过期未完成的候选榜与未裁决申诉；各写操作也会惰性推进，服务重启后从持久化的截止时间继续）。

### 管理复核能力

- POST /advancement/runs/replay 依据候选榜保存的输入快照重放一次榜单计算，与存储结果逐项比对，可发现任何事后改动；
- GET /advancement/runs/explain 给出每个作品的晋级或落选原因：计分评委与排除原因（缺评、迟交、撤销、不在有效集合）、维度均分、扣分、并列处理、名额与及格线判定、奖项约束结果；
- 所有写入经过幂等回执与哈希串联审计，GET /audit-events 可追踪每一次规则、评分、会签、发布与裁决。
