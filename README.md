# Jira Auto

个人 Jira Bug 预处理、证据分析与审批工作台。通过 Jira CLI 跟踪工单，收集日志和版本产物，结合现场、截图与目标版本代码生成可审阅结论；由使用者在网页或飞书批准后评论，Jira 状态流转仍由使用者完成。

当前版本 **0.1.0 / 开发版**，面向 Linux 和 Windows WSL2 的个人独立实例。已按 Zepp/Huami 的 Jira、质量平台、CI 与固件格式集成；接入其他组织需要适配固定服务地址、字段和证据解析规则。

## 功能

- **工单跟踪**：每 5 分钟分页同步本人未解决工单；支持手动输入 Jira key 或链接加入分析列表，已关闭或转出工单继续核对状态。
- **质量平台采集**：首次在网页连接 SSO，后台精确查询反馈并下载原始文件；校验、解包、归档 Jira 原包后继续分析。也支持手动导入 ZIP。
- **逐文件检查**：识别固件/手机日志、截图、二进制和 ramdump，记录用途、实际解析状态和边界；限制嵌套深度、体积、路径与 CRC。
- **版本和现场分析**：通过 [elf-fetch](https://github.com/active-ailab/elf_fetch) 获取 XML/ELF/MAP，核对架构、调试信息和目标 manifest；支持已知格式的 RV32 coredump、调用栈符号化、复位原因分类和只读源码对照。
- **AI 根因分析**：调用使用者已登录的 Codex CLI，将精选证据、行号、截图和源码交给模型；区分已确认机制、待验证诱因、证据不足与模型执行失败。检索近期相似工单作为线索。
- **过程看板**：持久化队列、阶段检查点、工具输出、事件时间线、SSE、材料和证据引用，支持查看失败原因和重试准备/分析阶段。
- **飞书审批**：专用机器人长连接、私聊绑定、版本化审批卡；批准具体版本后由服务评论，材料/工单变化使原审批失效。未知发送/发布结果不会自动重复提交。
- **日期提醒**：北京时间每天 09:00 后，完整同步时生成最多 3 个本人未完成工单建议，按截止日期→优先级→开始日期排序；截止日期超过 6 个自然月的工单不推荐。
- **本地 MCP/技能**：提供查询、准备、分析和草稿入口。MCP 不暴露审批、发布、流转、任意命令或任意文件访问。

## 当前边界

- 自动切换 manifest 和 `repo sync` **尚未接入**；源码对照只读，不清理开发工作区。
- 不承诺解析所有 ramdump、线程、堆和私有二进制格式；报告明确标记实际覆盖范围。
- 质量平台独立异步在线解密尚未集成；下载成功不代表所有文件已解码。
- 当前不是多用户租户服务，也不是已发布到公共插件目录的云服务。
- 默认仅监听本机，Jira 评论写入默认关闭；局域网模式当前不要求用户名密码，需显式配置 Host/Origin 白名单，只在可信内网使用。
- AI 依赖目标机 Codex 的登录、额度及 CLI 参数兼容；工具禁用参数不可为了启动而移除。

## 快速开始（Linux / WSL2）

需要 Python **3.12+**、uv、git、curl、已初始化的 [Jira CLI](https://github.com/ankitpokhrel/jira-cli)、elf-fetch、Codex CLI。崩溃分析还需 readelf 和对应 ARM/RISC-V addr2line；源码分析需具备相应权限的固件 repo 工作区。

```bash
git clone https://github.com/active-ailab/jira-auto.git
cd jira-auto
uv sync --frozen --extra test
```

### 1. 初始化账号（在本机操作）

Jira CLI 使用自己的 Jira 账号初始化；Zepp/Huami 环境使用 basic 认证。密码通过隐藏输入或现有 netrc 加载，不写入本仓库：

```bash
(
  read -rsp 'Jira 登录密码: ' JIRA_API_TOKEN
  echo
  export JIRA_API_TOKEN JIRA_AUTH_TYPE=basic
  jira init --installation local --server https://jira.huami.com --auth-type basic
  jira me
)
```

随后在本机 `~/.netrc` 配置对应 machine，设置权限 `chmod 600 ~/.netrc`，并验证离开上述子 shell 后 `JIRA_AUTH_TYPE=basic jira me` 仍成功。服务和附件通道复用这一份凭据；不要把 netrc 上传到仓库。

```bash
elf_fetch init
elf-fetch auth status
codex login
codex login status
```

Codex 应安装在运行服务的 Linux/WSL 环境中；Windows 客户端的登录不代表 WSL CLI 已登录。Codex 适配与代理说明见 [WSL 指南](docs/WSL部署指南.md)。

### 2. 先以本机只读方式运行

```bash
export JIRA_AUTH_TYPE=basic
export JA_SOURCE_DIR="$HOME/firmware"   # 自己的固件 repo 工作区
uv run --frozen jira-auto
```

打开 <http://127.0.0.1:8791>。首次在“接入与配置”连接质量平台账号；也可先联调已有 Jira 附件。默认数据在当前项目 `data/`，不属于源码或分发内容。

### 3. 配置长期运行 / 局域网访问

配置项见 [.env.example](.env.example)。systemd 使用私有 `~/.config/jira-auto/service.env`，模板见 [deploy/service.env.example](deploy/service.env.example)。服务不会自动读取项目 `.env`。

可用向导生成当前实例配置（替换 LAN 地址及 Codex 路径，源码目录需已存在）：

```bash
python3 deploy/configure.py \
  --source-dir "$HOME/firmware" \
  --dashboard-url http://192.168.1.20:8791 \
  --codex-bin "$HOME/.local/bin/codex"
.venv/bin/python scripts/install_service.py
systemctl --user status jira-auto.service
```

向导会设置 `JA_HOST=0.0.0.0`、实际 Host 白名单、`JA_LAN_AUTH=0` 和 `JA_ALLOW_JIRA_WRITE=0`，已有配置拒绝覆盖。修改配置后重启服务。systemd 环境文件不等于 shell 脚本，不要直接 source。

**WSL 用户**：按 [详细 WSL2 部署指南](docs/WSL部署指南.md) 配置 systemd、Windows NAT/镜像网络、防火墙和登录后保活；仅设置 0.0.0.0 不足以提供 LAN 访问。指南包含工具合集部署步骤；直接 clone 本仓库时按本节安装各工具，无需先执行依赖工具包路径的 install-wsl.sh。

### 4. 配置飞书专用机器人

在飞书企业自建应用中开启机器人、发送与接收单聊消息权限，配置长连接消息事件 `im.message.receive_v1` 和卡片回调 `card.action.trigger`，发布应用并添加使用者可用范围。

```bash
.venv/bin/python scripts/configure_feishu.py
```

在本机输入 App ID、隐藏的 App Secret、实际看板 URL。将脚本输出的整行绑定口令私聊发送给对应机器人，收到“绑定成功”后再验证卡片回调。长连接无需公网回调 URL。

新实例使用自己的机器人应用；迁移同一个应用时先停止旧实例，避免两台同时消费事件。秘密和绑定身份保存在私有 data 下，不提交。

### 5. 分析、审批和评论

1. 同步工单或指定任意有权限访问的 Jira 加入分析列表。
2. 执行 AI 分析，在“查看过程”检查采集、归档、版本、现场和模型阶段。
3. 审阅问题过程、核心日志/调用栈、原因、证据与简短评论草稿；模型错误会独立标记失败，不能审批为有效结论。
4. 完成只读联调后，在自己的服务环境中显式设置 `JA_ALLOW_JIRA_WRITE=1` 并重启。此后用户审批的具体版本才可以评论；不自动流转 Jira。

Jira 读取与评论走 CLI；只有 CLI 缺失的附件上传/下载使用固定 Jira origin 的 curl 通道，复用 netrc、校验附件和持久化收据，结果未知不自动重传。

## MCP 与分析技能

- [plugin/](plugin/)：开发版插件描述、MCP 桥接及 Zepp 分析技能。
- MCP 入口 `.venv/bin/jira-auto-mcp`，默认仅访问服务的 loopback 地址。
- Windows MCP 客户端使用向导生成的 `deploy/mcp-windows.generated.json`，由 `wsl.exe` 启动 Linux 入口；该生成文件包含个人路径，不提交。
- 通用 ChatGPT 云端不能直接访问你内网的 stdio MCP；本项目不把本地桥接描述为已经上线的公网插件。

## 项目结构

```text
jira_service/      API、队列、采集、附件检查、现场/版本/AI 分析、审批及网页
plugin/            本地 MCP 插件与可复用分析技能
scripts/           服务安装、飞书配置、插件/WSL 打包
deploy/           实例配置、自检、WSL 安装和 Windows LAN 辅助脚本
docs/             详细部署指南与架构/流程说明
tests/            使用隔离目录和假客户端的回归测试
.env.example      无凭据的配置说明
uv.lock           Python 依赖锁文件
```

## 自检与开发验证

```bash
.venv/bin/python deploy/doctor.py --online
.venv/bin/pytest -q
node --check jira_service/static/app.js
node --check jira_service/static/review.js
git diff --check
python3 scripts/check_public_files.py
```

doctor 不发布评论或上传附件，也不进行真实模型调用。发布前回归基线为 193 项测试通过；目标 Windows/WSL 的防火墙、企业 VPN、任务计划和实际账号闭环需要在目标机验收。

[架构与流程](docs/架构与流程.md) 说明证据边界、审批版本、发布结果核对、队列恢复和每日推荐策略；[AGENTS.md](AGENTS.md) 包含维护约束。

## 仓库内容与个人数据

仓库仅提交源码、合成测试样例、依赖锁、插件描述、技能、部署脚本和配置模板。以下内容必须留在各使用者本机：

- Jira netrc/配置、飞书 App Secret/open_id/绑定口令、质量平台会话、Codex auth、SSH 密钥。
- `data/` 数据库、日志、截图、ramdump、下载的 XML/ELF/MAP、真实用户附件。
- `.venv`、个人 service.env、生成的 MCP 路径配置、打包 ZIP 和 outputs。

`.gitignore` 会排除这些常见路径及产物。模板中的地址、账号和测试凭据均为示例；请通过配置脚本在本机接入，勿把实际凭据填进模板后提交。推广给同事使用独立身份和独立 data 目录。

## 打包

```bash
python3 scripts/package_plugin.py
python3 scripts/package_wsl.py --help
```

WSL 打包器需在本机准备已验证的 Jira CLI、uv、repo 及 elf-fetch 源码；它生成部署合集，**不是完全离线安装器**。第三方工具仍各自遵循上游许可，生成的工具二进制及凭据不纳入 Git。本项目尚未选择开源许可证；公开可见不自动代表授予开源许可。
