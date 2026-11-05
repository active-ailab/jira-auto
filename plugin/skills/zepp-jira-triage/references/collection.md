# 质量平台收集与 Jira 归档

## 优先使用后台采集

反馈工单加入分析列表或运行 AI 分析时，服务先读取 Jira 附件；缺少日志则进入“质量日志采集与归档”阶段，直接查询反馈、下载原始文件、打包检查、上传 Jira，CLI 核对附件后继续分析。看板材料页 `quality_collection` 和任务时间线保留真实状态。

首次或会话失效时，在“接入与配置 → 连接质量平台账号”完成原有 SSO 登录，不抓取 Cookie。服务保存本机会话；断开浏览器后仍可采集。`auth_required` 等重连；`pending/network_error/archive_pending` 间隔至少 5 分钟、最多自动续跑两次；`archive_failed` 或上传结果未知先查收据和错误，不能反复上传。重连只续接用户已选择且登录受阻的任务。

接口适配经验：反馈清单为 `/api/feedback/list` 的 `detail`，下载 `/api/feedback/download` 必须带本条反馈的 `id/index/user_id/sync_time`；只带前两项会误报账号注销。返回的 `url/APP_OOM_DATA/app/fb` 可能是 URL 或文件数组。下载用独立请求，不将 SSO Authorization 转发至存储；支持主机以代码验证清单为准，新区域显式受阻后可浏览器兜底。不要将签名 URL、回调票据或会话写入技能、报告和事件。

后台探测下载与正式采集状态分离；已发布工单可以只验证文件哈希，但不能因此排队上传或新稿。后台采集原始文件不意味着平台异步解密或每种二进制解码均已实现。

## 浏览器兜底路径

1. Jira CLI 读取工单原始详情，使用服务解析出的 `issue.feedback.url` 或描述中的 `https://devops.zepp.com/zeus/#/feedback?app_name=...&id=...`。不要复用其他工单的反馈 ID。
2. 优先使用已登录的 Chrome 会话，以当前工具提供的浏览器 API 操作。打开反馈，核对问题描述、设备、版本和反馈时间。已有 Jira 附件优先校验复用；需要补充材料时再进入平台。
3. 操作栏 **日志 → 原始日志 → 下载Zip**。下载按钮可能异步打包；点击不等于完成。确认最终文件存在、下载结束、ZIP 可读，不能把 HTML 登录页或 `.crdownload` 交给后台。
4. “解密日志”是另一条平台异步解析流程，不等于原始 ZIP 下载。若固件文本已经可读，可先分析；仅在相关材料确实需要专用解码时使用当前可用的解析入口，说明等待状态，不随意重复触发任务。
5. 登录回跳导致旧 tab 句柄超时时，用浏览器支持的标签列表重新定位该页。遵守浏览器的登录、权限与确认规则，不导出 cookie/登录票据到代码、日志或技能文件。

后台接口受阻时，可由有浏览器能力的交互式 agent 主动下载，再交给现有 ZIP 导入通道。没有有效会话时如实标记登录受阻；不要反复绕过平台授权。

## 可靠交给后台

在服务源码目录运行，或通过 `--service-dir` 显式指定服务位置。本 skill 附带的本地 helper 使用该仓库虚拟环境，只校验本地输入并排队；上传、CLI 刷新、去重和分析衔接由已运行的服务执行，避免临时脚本重新实现认证。

```bash
.venv/bin/python \
  plugin/skills/zepp-jira-triage/scripts/queue_quality_archive.py \
  --issue PROJECT-123 --archive /absolute/path/downloaded-feedback.zip
```

默认仅检查，不上传、不排队。确认这是本次目标反馈且用户请求包含正常处理流程后，同一命令加 `--submit`。安装在插件目录时使用该 skill 自身的脚本位置；其他机器用 `--service-dir`、必要时 `--data-dir` 指向其服务。

脚本把原始包保存到 `data/cases/KEY/quality/SHA256/原文件名`，排队 `import_quality_logs`。它要求工单已被服务导入且有质量反馈链接；未知工单先通过现有添加工单入口导入，再收集。若已有相同/其他分析正在执行，先跟进它，不并发启动不同输入的同类任务。

跟进 `GET /api/runs/{run_id}`，不要输出 `/api/session` 的 token：

- `completed`：看结果中的 `jira_attachment.attachment_id`，再跟进 `next_run_id`，直到分析完成。
- `failed/interrupted`：查看事件和具体失败阶段。只续跑允许的读取、下载、解析、分析阶段。
- 附件上传 `unknown`：服务有 `quality_uploads` 收据，禁止自动重发；先通过 CLI 核对现状，不能手改收据伪造成功。
- `needs_selection`：查看现场与 dump 组。唯一时间关联可自动选择；真正有多组歧义时保留边界，不能随便选最新 dump。

若通过看板上传：**材料与版本 → 导入原始日志 ZIP**。质量反馈工单走 `import_quality_logs`，普通本地材料走 `import_logs`；后台行为不同，不能混淆。

## 工具与认证边界

- 工单详情、评论、相似工单检索均使用安装的 Jira CLI。`.netrc` / 现有服务环境只在本机使用，不打印或复制凭据。
- 当前 CLI 缺少附件上传/下载命令；仅附件传输允许服务现有的固定 Jira origin + curl + netrc 适配器，禁止任意目标、重定向和新的凭据库。
- 上传原包需要当前任务范围允许、服务回写开关启用。查询状态或 review 已发布评论不自动触发上传。
- 只有后台完成上传且 CLI 重新读到附件，才称“已附到 Jira”。本地 `log_archive` 记录不等于上传成功。
- 不直接改 SQLite 中的审批/发布状态。脚本排队任务不能审批、发布评论或流转工单。
