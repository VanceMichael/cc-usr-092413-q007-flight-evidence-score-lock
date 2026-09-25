# 竞速飞行证据补齐与成绩锁定

服务接收脱敏飞行证据（轨迹点 + 气压计摘要），完成分段复算、候选成绩冻结、
独立仲裁、榜单锁定/发布，以及发布后补证的影响评估与受控重开。存储使用
进程内 SQLite（`EVIDENCE_DB` 环境变量指定路径，默认 `./evidence.db`）。

## 领域不变量

- **轨迹点完整字段**：设备时间、接收时间、设备会话、位置（经纬度）、高度、摘要。
- **排序不看全局序号**：设备重启会产生新会话并让设备序号回退，跨午夜序号也会归零。
  分段排序键为「会话启动次序 + 完整设备时间」，序号回退仅作为证据事实记录在 `notes`。
- **数据包三态**：
  - `packet_id` + 内容哈希相同 → `duplicate`，幂等归并，水位不变；
  - `packet_id` 相同、哈希不同 → `quarantined`，隔离，不进分段、不抬水位；
  - 新标识在榜单锁定/发布后到达 → `held`，只生成影响评估，不并入水位。
- **分段依据**：起终门穿越（相邻点插值）、有效航段走廊半宽、圆形禁飞区、
  以及逐点当时生效的校准版本（高度偏移/缩放，按 `valid_from/valid_to` 切换）。
  每个航段输出 `adopted` + 中文 `reason`，被排除航段给出具体原因。
- **成绩冻结**：裁判提交候选成绩时，连同各航段（采纳与排除）理由和证据水位/指纹
  一起快照；之后证据推进不改写旧版本。
- **仲裁约束**：选手与证据上传者无自批权限，提交裁判不能自裁；独立仲裁人必须先
  查看证据并登记「所见水位」，只能确认该版本。
- **单一水位事务**：锁定/发布/重开任一事务只让一个证据水位胜出，榜单行全部绑定
  同一 `watermark`，不会出现多版本混合榜单。
- **发布前后**：
  - 发布前新证据并入新水位并排复算任务；
  - 发布后补证先生成 `pending` 影响评估，独立审批人 `approved` 后才建立
    `alternate` 替代成绩；原排名、旧成绩版本与通知事实全部保留；
    替代成绩经仲裁确认后才发布为新榜单版本，并给出相对首榜的 `rank_change`。
- **恢复**：停机期间申诉截止时间按停机时长顺延，已排队复算任务在恢复/进程重启时
  继续执行（`startup_recovery`）。

## 主要接口

身份通过 `X-User` 头传递（证据接收、仲裁查看等写操作必需）。

| 方法 & 路径 | 说明 |
| --- | --- |
| `POST /api/v1/events` | 建赛事（门、走廊、禁飞区、校准版本） |
| `POST /api/v1/events/{id}/flights` | 登记选手飞行 |
| `POST /api/v1/flights/{id}/evidence` | 上传数据包（归并/重复/隔离/暂扣） |
| `GET  /api/v1/flights/{id}` | 数据包状态、当前分段、各成绩版本与航段理由 |
| `POST /api/v1/flights/{id}/scores` | 裁判冻结候选成绩 |
| `GET  /api/v1/flights/{id}/evidence-view?score_id=` | 仲裁人登记所见证据版本 |
| `POST /api/v1/flights/{id}/scores/{sid}/arbitration` | 独立仲裁确认/驳回 |
| `POST /api/v1/events/{id}/lock` · `/publish` | 锁定 / 发布单一水位榜单 |
| `GET  /api/v1/events/{id}/ranking` | 当前榜单、名次变化、历史版本 |
| `GET  /api/v1/assessments/{id}` | 发布后补证的影响评估 |
| `POST /api/v1/assessments/{id}/decision` | 批准重开（建替代成绩）/驳回 |
| `POST /api/v1/flights/{id}/republish` | 替代成绩确认后发布新版榜单 |
| `POST /api/v1/events/{id}/pause` · `/resume` | 停机与恢复（顺延申诉期、续跑队列） |
| `GET  /api/v1/events/{id}/notifications` | 全部通知事实 |

## 开发命令

- 安装依赖：`python3 -m pip install -r requirements.txt`
- 运行测试：`python3 -m pytest -q`
- 编译检查：`python3 -m compileall -q app tests`
- 启动服务：`python3 -m uvicorn app:app --host 0.0.0.0 --port 8000`

测试和构建只使用仓库内数据，不需要连接外部业务服务。

## 代码结构

- `app/models.py`：请求模型与赛事几何/校准配置
- `app/engine.py`：会话排序、门穿越插值、走廊/禁飞区/校准判定、航段理由
- `app/storage.py`：SQLite schema 与单一写锁
- `app/service.py`：证据水位、成绩冻结、仲裁、榜单生命周期、停机恢复
- `app/api.py`：HTTP 接口
