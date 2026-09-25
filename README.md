# 竞速飞行证据补齐与成绩锁定

服务接收脱敏飞行证据(轨迹点),完成分段、成绩冻结、仲裁确认、排名发布与
补证重开的全流程管理。领域持久化使用内嵌 SQLite,无需外部业务服务。

## 核心语义

- **证据接收**:轨迹点完整记录设备时间、接收时间(服务端盖章)、设备会话、
  位置、高度与摘要。相同数据包/轨迹点重复到达时归并(幂等);标识相同但
  内容不同的轨迹点先隔离(`quarantine`),裁决前不参与计算。
- **分段**:依据起终门、有效航段、禁飞区和飞行当时生效的校准版本计算。
  一律按设备时间排序;设备会话与序号仅用于同刻仲裁和重启诊断——跨午夜或
  设备重启导致序号回退时,绝不继续用全局序号排序。
- **成绩冻结**:裁判提交的候选成绩连同采用/排除的航段及理由一并冻结;
  独立仲裁人只对与自己所见一致的证据版本(waterline)确认才计入法定人数;
  选手与证据上传者没有自批权限,提交裁判也不能兼任独立仲裁。
- **排名与补证**:发布前新证据触发重算;发布后补证只生成影响评估
  (would_change + 逐航班名次变化),获准重开才建立替代成绩,原排名与
  通知事实保留。锁定/发布与补证并发时,单写事务保证只有一个证据水位胜出,
  不会出现多版本混合榜单。
- **恢复**:重算任务持久化在 `jobs` 表,服务启动时把中断的 running 任务
  重新入队续跑;申诉期限是绝对时间,天然随恢复继续有效。

## 开发命令

- 安装依赖:`python3 -m pip install -r requirements.txt`
- 运行测试:`python3 -m pytest -q`
- 编译或构建检查:`python3 -m compileall -q app.py domain tests`
- 启动服务:`python3 -m uvicorn app:app --host 0.0.0.0 --port 8000`

测试和构建只使用仓库内数据,不需要连接外部业务服务。

## 配置(环境变量)

- `EVIDENCE_DB_PATH`:SQLite 路径,默认 `:memory:`(镜像中默认 `/data/evidence.db`)
- `EVIDENCE_QUORUM`:成绩锁定所需独立仲裁确认人数,默认 `2`
- `EVIDENCE_APPEAL_WINDOW_S`:发布后申诉窗口秒数,默认 `259200`(72h)
- `EVIDENCE_DEFER_JOBS=1`:关闭请求内自动执行任务(重算只入队,便于演示恢复)

## 主要接口

鉴权:变更类接口需要 `X-User-Id` / `X-User-Role` 请求头,角色为
`player` / `uploader` / `referee` / `arbitrator` / `admin`。

- `POST /api/v1/events/{eid}/gates|no-fly-zones|calibrations` — 赛道与校准配置(referee/admin)
- `POST /api/v1/evidence/packets` — 证据包接收,归并/隔离(uploader)
- `GET  /api/v1/flights` / `flights/{fid}` / `flights/{fid}/points` — 证据、分段、成绩版本与航段理由
- `GET  /api/v1/quarantine`,`POST /api/v1/quarantine/{qid}/resolve` — 隔离裁决(referee/admin)
- `POST /api/v1/flights/{fid}/scores` — 提交候选成绩并冻结航段裁决(referee)
- `POST /api/v1/scores/{sid}/confirmations` — 独立仲裁确认所见证据版本(arbitrator)
- `POST /api/v1/events/{eid}/publish` — 发布排名并生成通知事实(referee/admin)
- `GET  /api/v1/events/{eid}/ranking|publications|assessments` — 排名(含航段理由、
  成绩版本、名次变化)、发布与通知事实、影响评估
- `POST /api/v1/assessments/{aid}/reopen|reject` — 获准重开 / 驳回(admin)
- `GET  /api/v1/jobs`,`POST /api/v1/jobs/drain` — 任务队列查看与手动执行(admin)
