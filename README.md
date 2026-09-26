# 影视字幕本地化质检

一个仅使用 Python 标准库实现的字幕翻译、时间轴审核和交付服务。SQLite 保存项目、字幕版本、人员分配、时间点评论、术语表、复核意见、评论返修记录和交付快照。

## 运行

```bash
python app.py --init
python app.py --port 8009
```

打开 <http://127.0.0.1:8009>。`--init` 会创建示例纪录片项目、`zh-CN` 草稿版本和一条术语规则。数据库默认是 `subtitle_qc.db`，可用 `--db` 或 `SUBTITLE_DB` 修改；旧库启动时自动补充返修流程所需的表和列。

## 代码结构

- `core.py`：共享内核（时间工具、`DomainError`）。
- `records.py`：**评论记录层**，意见主体、处理说明回复（`comment_replies`）、状态事件（`comment_events`）的存取。
- `flow.py`：**状态流转层**，返修状态机、权限校验、提交复核闸门。
- `app.py`：项目/版本/字幕/复核/交付业务与 HTTP 路由。
- `static/index.html`：**字幕工作台**（录入字幕、版本状态流转）。
- `static/rework.html`：**评论返修页**（提出意见、登记责任人、回复、确认、退回）。
- `static/flow.html`：**状态流转页**（按意见查看事件流水），共享 `app.css`、`common.js`。

## 流程

1. 负责人创建项目、字幕版本和术语规则。
2. 为版本分配 `translator`、`timeline`、`reviewer`。
3. 翻译或时间轴成员保存字幕；每项包含 `expected_revision`，旧页面提交会返回 409。
4. 成员可对具体字幕或毫秒时间点添加评论。
5. **评论接成返修流程**（见下），全部意见关闭后才能提交复核。
6. 翻译/时间轴成员提交复核，分配的非创建人复核人批准或退回。
7. 负责人锁定已批准版本，再执行交付。
8. 交付时生成确定性的 SHA-256 快照；同语言的新交付会把旧版本标记为 `superseded`，但旧快照不会删除或覆盖。

字幕保存会验证时长范围、起点小于终点、字幕重叠、序号冲突和术语表。术语表中配置的禁用译法会直接阻止保存；指定译法可用。

### 评论返修状态机

```
open ──复核人登记责任人/期限──▶ open ──翻译·时间轴回复处理说明──▶ pending_confirmation
  ▲                                                                    │
  │◀──── 复核人退回 ────────────────────────────────────────────────────┤
  │◀──── 关联字幕再次修改（closed / pending_confirmation 自动重开）─────┤
  └────────────────────────── 复核人确认 ──────────────────────────────▶ closed
```

- **登记**：复核人（reviewer 或负责人）登记责任人（必须是该版本的翻译/时间轴成员）和 `YYYY-MM-DD` 处理期限。
- **回复**：获得翻译或时间轴权限的成员写明处理说明，意见转为 `pending_confirmation`；未登记责任人或非编辑成员不能回复。
- **确认/退回**：只有复核人可以确认关闭；不认可时填写原因退回 `open` 继续返修。
- **自动重开**：挂在某条字幕上的意见关闭（或待确认）后，该字幕再次保存修改时自动重新打开；只挂时间点、未挂字幕的意见不重开。
- **提交闸门**：版本存在任何未关闭或待确认意见时，`submit` 返回 409。
- 处理说明保存在 `comment_replies`，状态流水保存在 `comment_events`（登记/回复/确认/退回/重开均留痕），与意见主表分开。

## API

所有身份通过 `X-User`、`X-Role` 请求头模拟，角色包括 `owner`、`admin`、`translator`、`reviewer`、`timeline`。

- `POST /api/projects`：创建项目和成片校验信息。
- `POST /api/projects/{id}/versions`：创建目标语言版本，可指定同语言父版本。
- `POST /api/projects/{id}/glossary`：设置指定译法和禁用词。
- `POST /api/versions/{id}/assignments`：分配角色。
- `POST /api/versions/{id}/cues`：新增或修改字幕，要求 `expected_revision`；响应含 `reopened_comments`。
- `POST /api/versions/{id}/comments`：按具体时间毫秒或字幕 ID 评论。
- `GET  /api/versions/{id}/comments`：评论记录（含责任人、期限、处理说明回复）。
- `POST /api/versions/{id}/comments/{cid}/triage`：复核人登记 `assignee`、`due_date`。
- `POST /api/versions/{id}/comments/{cid}/reply`：翻译/时间轴成员提交 `note` 处理说明。
- `POST /api/versions/{id}/comments/{cid}/confirm`：复核人确认关闭。
- `POST /api/versions/{id}/comments/{cid}/return`：复核人填写 `reason` 退回。
- `GET  /api/versions/{id}/comment-events`：状态流转事件流水。
- `POST /api/versions/{id}/submit|review|lock|deliver`：完成审核交付状态机；`submit` 受意见闸门保护。
- `GET /api/versions/{id}/cues`、`GET /api/deliveries`：查看结果。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖完整返修闭环（登记→回复→确认关闭）、退回与字幕修改自动重开、提交闸门、时间点意见不重开、状态事件独立记录，以及锁定覆盖保护、旧修订冲突、时间轴重叠、术语禁用和人员权限。
