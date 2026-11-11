# 经营周报流水线

> **界面边界（重要）**：产品界面只做在助手对话里。`/chat.html` 是唯一实际使用界面，S1–S6 的详情、审核、交付都以助手内的弹窗（`runDetailDialog`、`s2DetailDialog` … `s6DetailDialog`）呈现；`/s2.html`–`/s6.html` 是开发自测页，只用于调试流水线、逐阶段查看中间产物，**不是产品界面**（S1 没有独立页面，S1 详情在助手的运行详情弹窗里）。做演示、截图、UI 改动时以 `/chat.html` 的弹窗为准，不要把测试页当成最终交付界面。

把「一份销售数据 + 一份会议纪要」变成一份可审计的经营周报。当前完成 **S1–S6**：S1–S4 准入 / 拆卡 / 对账 / 修纪要，S5 扩写与审核，S6 交付归档（历史质检与送审已并入 S5，交付从 S5 拆为独立 S6）。S5 扩写可在对话里跑（`/chat.html` 的 S1–S6 弹窗），也可用 `/s5.html` 自测页调试：交给 pandasai 做数据发现（模型生成并执行 pandas 代码发现/取数/算数/选题），图由 Altair 固定模板按已入台账的分组/数值两列渲染，模型只把新发现写成「数据发现」补充句挂在同主题骨架句之后，纪要骨架（`corrected.json` 的 `items[].fixed`）逐句原样保留；去掉字数、章节、禁用词、固定图数等格式规制。S5 的业务数（摘要/区域/品类/前5/门店）由 pandasai 固定问句算出，代码用源数据复核后才采用；收尾把 `[[f:key]]` 替换成台账 `display`，跑自动质检，再让 GLM 复核正文与数据质量（有疑问可调 pandasai 再查），结论写进 `qc_result.json`，通过后写入 `review_record.json` 并自动进入待审核；审核台就是助手里的 S5 弹窗（自测页 `/s5.html` 也可查看），通过或退回由审核人决定，通过时审核人签名确认；审核签名记录后，在助手的 S6 弹窗生成交付包（自测页 `/s6.html` 也可操作；S6 不再单独要求签名）。数据发现的问题清单可在助手的 S5 弹窗里增删改（自测页 `/s5.html` 也可），也可让主 agent 调 `s5_questions` 工具改；保存后重新扩写生效。已扩写过时默认全量重跑，不复用上一稿结果；只有明确要“只重算改动的问题”时才走增量（`full=false`），未改的复用当前 findings。弹窗里的「重新扩写」就是全量；「只重算改动的问题」才是增量。

## 快速开始

```bash
pip install -r tools/requirements.txt

双击 启动界面.bat            # 或 python web/server.py --port 8000
```

打开 http://127.0.0.1:8000/chat.html ，点顶栏「模型」配置一个供应商（Base URL / API Key / 模型），保存后即可使用；上传数据文件与纪要，按左侧流水线依次完成 S1–S6。每个阶段的详情、审核与交付都在助手弹窗里完成，不需要另开页面。`/s2.html`–`/s6.html` 只是开发自测页，不是使用入口。

## 模型配置

页面顶栏的「模型」按钮打开配置弹窗，支持保存多套供应商并切换当前生效：

- 每个供应商填名称、Base URL、API Key、模型；模型可以从接口的 `GET /models` 拉取后点选，也可以手填。
- 「测试连接」会用当前表单或已保存的配置发一条短消息验证，失败原因直接显示，密钥不会回显。
- 保存后写入 `config/models.json`（已忽略，不入库），全流水线立即使用同一份配置。
- 没有配置文件时回退到 `.env` 的 `GLM_API_KEY` / `GLM_BASE_URL` / `GLM_MODEL`；页面上保存过供应商后，以列表里当前启用的那份为准。
- 只支持 OpenAI 兼容的 `/chat/completions` 接口；`pip install -r tools/requirements.txt` 不受影响。

## 结构

| 路径 | 作用 |
|---|---|
| `pipeline/s1_intake.py` | S1 准入检查：清点、判角色、抽周期与需求、对账、判定、落盘 |
| `pipeline/domain_map.py` | 用模型把纪要里的指标/实体映射到数据列，按输入哈希落盘冻结 |
| `pipeline/s2_cards.py` | S2 纪要拆卡：切句、拆卡、抽动作与未来事项，落盘 `cards.json` |
| `pipeline/s3_reconcile.py` | S3 数据对账：取数、判容差、出问题清单与台账，落盘 `issues.json` / `ledger.json` |
| `pipeline/s4_draft.py` | S4 修纪要底稿：按问题和用户裁决逐句改稿，代码校验原句、台账依据与数字，落盘 `corrected.json` |
| `pipeline/s5_discover.py` | S5 数据发现：模型提单步可答问句、pandasai 生成并执行 pandas 代码取数/算数，产出 findings（含 spec / code_hash / 稳定 key）；有 `s5_questions.json` 时只跑清单里的问题，不重新出题 |
| `pipeline/s5_questions.py` | S5 问题清单：规范化、增删改、落盘 `s5_questions.json`、计算「改动是否已用于当前稿」的脏状态 |
| `pipeline/s5_write.py` | S5 扩写：纪要骨架逐句保留 + 发现转台账 + 按发现形态出图 + 装配「数据发现」补充句，落盘 `draft.md` / `figures_manifest.json` / `profile.json` / `s5_facts.json`，自动送审并进入待审核；每次重跑前把上一版归档到 `history/<时间>/` |
| `pipeline/s5_review.py` | S5 审核：整理送审材料写 `review_record.json`，审核人签名确认 → 已通过、退回 → 退回修改并按类别归到 S3/S5；送审已并入本模块 |
| `pipeline/s6_deliver.py` | S6 交付：按 S5 记录的审核签名生成 Word + Markdown + 图表 + 台账 + 图表清单 + 质检说明 + 审核记录 + 清单，按内容指纹 + 版式版本出版本，原子写入 `deliver/vN/`，状态改为已交付 |
| `pipeline/s6_layout.py` | S6 AI 排版层（交付时调用）：模型输出 JSON 计划（选 2–3 张图、图宽、小节标题、小标题、分页），代码校验后渲染 `layout.md`；封面、摘要、核心指标表和三线表由代码生成，正文经润色与未入选图引用清理后逐字落稿 |
| `pipeline/s5_chart.py` | pandasai 出图备选：两列数据 → pandasai `save_charts` PNG，校验 300dpi / 代码引用 df；生成代码留档 `figures/src/figNN.py` |
| `pipeline/s5_altair.py` | S5 默认出图：已入台账的分组/数值两列 → Altair 固定模板（横向柱/竖向柱/折线/占比条）→ SVG + 300dpi PNG，Vega-Lite spec 留档 `figures/src/figNN.vl.json` |
| `pipeline/typeset_docx.py` | Pandoc 排版工具：把 `layout.md` + `tools/templates/report-reference.docx` 转成 Word，校验产物可打开且含图 |
| `tools/make_reference_docx.py` | 生成 Word 样式模板（A4、宋体小四 1.5 倍行距、黑体标题、Image Caption 段后 18pt、页眉页脚页码）；改版式改这里再重跑 |
| `pipeline/report_docx.py` | python-docx 兜底排版：Pandoc 不可用或排版稿校验失败时使用，逻辑与 Pandoc 版一致 |
| `pipeline/pandasai_glm.py` | 让 pandasai 的 OpenAI 适配器指向当前模型（补模型白名单 + api_base） |
| `pipeline/agent.py` | 主 agent：对话 + 工具调用，不计算、不改判定 |
| `pipeline/model_config.py` | 模型供应商配置：`config/models.json` 的读写、校验、当前供应商切换 |
| `pipeline/llm.py` | OpenAI 兼容客户端（仅标准库）；优先读当前供应商，无配置时回退 `.env` |
| `web/server.py` | 本地 HTTP 服务（仅标准库） |
| `web/static/chat.html` | **唯一产品界面**：助手对话 + S1–S6 弹窗（运行详情、拆卡、对账、修稿、扩写审核、交付） |
| `web/static/s2.html`–`s6.html` | 开发自测页，只用于逐阶段排障，**不是产品界面**（S1 无独立页面） |
| `s1.txt` … `s6.txt` | 各阶段规格说明（`s5.txt` 扩写与审核，`s6.txt` 交付；历史质检与送审已并入 S5） |
| `规范.md` | 共用判定标准：字数、图数、容差、禁用词、目录结构、docx 版式 |
| `fixtures/w44/` | 固定夹具：两周销售明细 + 纪要，含退货、缺数、重复记录 |
| `runs/` | 运行产物（已忽略，不入库） |

## 设计边界

- **AI 做映射，代码做判定**。模型只把自然语言翻译成结构化字段；通过与否由代码按阈值判，同一份材料永远同一结论。
- **状态只写在 `status.json`**。每步只读文件，不读对话。
- **数字只引用，不转述**。主 agent 不能自己算，也不能改工具的结论。

## 进度

- [x] S1 准入检查（代码判定 + AI 指标映射）
- [x] S2 纪要拆卡（模型读句拆卡 + 代码校验原话与枚举）
- [x] S3 数据对账（pandasai 生成代码取数 + 容差判定 + 台账）
- [x] S4 修纪要底稿（逐条裁决 + 模型改句 + 代码校验，落盘 corrected.json）
- [x] S5 扩写出图（pandasai 发现驱动 + 纪要骨架逐句保留 + 去掉格式规制 + 保留台账/占位符，收尾替换占位符并自动送审）
- [x] S5 审核台（扩写后自动 待审核 → 审核人签名确认 → 已通过 / 退回修改）
- [x] S6 交付归档（审核签名记录后生成论文式 docx + md + 图 + 台账 + 清单；同内容幂等，内容或版式变化才递增版本）
- [x] S6 AI 排版（交付时模型出摘要、正文润色、小节标题、分页计划 → `layout.md` → Pandoc + Word 模板出稿；图片位置由代码按正文引用锁定，图注间距由模板统一）
- [x] 交付包留痕：`layout.md` / `layout.json` / `polish_map.json` / `draft_original.md` 随包归档，清单记录 `layout_engine` 与 `layout_source`
