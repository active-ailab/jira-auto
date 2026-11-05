# Jira Auto · Windows WSL2 部署与迁移指南

版本：2026-11-05；适用：Windows 11 / WSL2 / Ubuntu 24.04 / x86_64。交付方式：个人独立实例，联网安装。本文和部署包配套，先读第 1～3 节再操作。

## 1. 交付物与能力边界

下载本飞书文档对应的 `Jira-Auto-WSL2-20261105.zip`，里面包含：

- **jira-auto-service.tar.gz**：当前工作目录完整应用源码、网页、MCP、分析技能、测试、uv.lock、安装与自检脚本
- **jira-auto-toolkit.tar.gz**：Linux x86_64 Jira CLI 1.7.0、uv 0.11.17、repo launcher、elf-fetch 0.1.0 源码及 VS Code 授权 helper 源码、第三方许可和来源
- **WSL部署指南.md**：本指南的本地副本
- **SHA256SUMS.txt**：两个压缩包和指南的校验值


工具包固定版本来自当前可用环境；elf-fetch 对应提交 e74c975ef8b5fd477b33bcf3ecf38a53c01d01e5。`PROVENANCE.json` 记录来源及工具哈希。工具包不包含 Codex 可执行文件、Ubuntu apt 包或 Python 依赖缓存，安装过程需要访问相应官方源。不要在 Windows 原生终端直接运行这些 Linux 可执行文件。

可迁移能力：Jira CLI 同步、手动加入工单、质量平台后台采集、原包归档、附件逐文件检查、版本 XML/ELF/MAP 获取、日志/截图/支持格式 ramdump 分析、审批后评论、飞书长连接卡片、每日最多 3 条优先处理建议、网页过程和证据查看。

边界：当前为个人单实例服务，不是多人租户系统；源码只读比较目标 manifest 对应提交，**自动 repo sync 尚未接入**；未知或不支持的 dump 格式不会被当作完整解析；模型调用失败会单独报错，不等于“证据不足”。MCP 提供查询/准备/分析入口，不提供绕过审批的发布能力。通用 ChatGPT 云端不能直接访问你局域网中的 stdio MCP；Windows 本机支持 MCP 的客户端需按第 12 节配置。

默认创建干净实例：不携带本机 `.netrc`、Jira 配置、飞书 Secret、Codex 登录、质量平台会话、SSH 私钥、历史数据库、真实附件、固件源码和 `.venv`。以上在新电脑自行配置。原机器的历史分析暂保留原机，本文不包含跨路径数据库迁移工具。

## 2. 资源、账号与部署路径

建议至少 4 核、16 GB 内存；服务本身占用较小，日志/符号/固件源码决定磁盘需求。给服务材料和缓存预留 50 GB 起，完整固件 repo 另按项目体量预留空间。Windows 睡眠、关机、VPN 中断时后台任务和提醒都无法正常运行。

准备这些账号权限：Jira 登录账号/密码与工单读取、附件上传、评论权限；质量平台原有 SSO；Zepp CI 的 elf-fetch 授权；Gerrit SSH 读取和目标项目代码权限；新机 Codex/ChatGPT 登录；专用飞书企业自建机器人应用的管理权限和可用范围。凭据只在目标机输入。

推荐目录：

```text
/home/<WSL用户名>/apps/jira-auto-service     应用与 data
/home/<WSL用户名>/apps/jira-auto-toolkit     工具与 elf-fetch 源码
/home/<WSL用户名>/firmware                  固件 repo 工作区
/home/<WSL用户名>/.config/jira-auto/service.env
```

将源码放在 WSL Linux 文件系统，不要部署到 `/mnt/c/...`，以避免权限和大量小文件访问问题。此建议及 WSL2 要求见 [OpenAI WSL 文档](https://learn.chatgpt.com/docs/windows/wsl)。

## 3. 安装 WSL2 并启用 systemd

以下命令在 **Windows 管理员 PowerShell** 中执行：

```powershell
wsl --install -d Ubuntu-24.04
wsl --update
wsl --version
wsl --list --verbose
```

按提示重启 Windows，进入 Ubuntu，创建自己的 Linux 用户。确认 Ubuntu-24.04 的 VERSION 为 2；不是则执行：

```powershell
wsl --set-version Ubuntu-24.04 2
```

以下在 **Ubuntu WSL 终端** 中执行：

```bash
uname -m
python3 --version
ps -p 1 -o comm=
```

预期 x86_64、Python 3.12 或更新、PID 1 为 systemd。本工具包不适用于 ARM64 Windows。

如果 PID 1 不是 systemd，用 `sudoedit /etc/wsl.conf` 合并以下内容，保留原有其他配置：

```ini
[boot]
systemd=true
```

随后在 Windows PowerShell 执行 `wsl --shutdown`，再打开 Ubuntu。WSL 的 systemd 支持要求 WSL 0.67.6 或更新，参见 [Microsoft systemd 说明](https://learn.microsoft.com/en-us/windows/wsl/systemd)。

## 4. 解压和安装应用、工具

先把 ZIP 下载到 Windows Downloads。以下在 **WSL** 中执行，将路径里的 Windows 用户名换成自己的：

```bash
mkdir -p "$HOME/apps/jira-auto-release"
cd "$HOME/apps/jira-auto-release"
python3 -m zipfile -e /mnt/c/Users/你的Windows用户名/Downloads/Jira-Auto-WSL2-20261105.zip .
sha256sum --check SHA256SUMS.txt
cd "$HOME/apps"
tar -xzf jira-auto-release/jira-auto-service.tar.gz
tar -xzf jira-auto-release/jira-auto-toolkit.tar.gz
cd jira-auto-service
bash deploy/install-wsl.sh "$HOME/apps/jira-auto-toolkit"
export PATH="$HOME/.local/bin:$PATH"
```

首次安装不要选择已经有旧版应用的目标目录。若目录已存在，先改名留存，不能直接用解压覆盖正在运行的实例。

安装器先验证工具包 SHA256，再安装 apt 依赖，按 `uv.lock` 创建新 `.venv`，安装 elf-fetch。不会复制任何密码；如果 `~/.local/bin` 已有不同版本的工具，会保留并提示你检查，不会擅自覆盖。

将 `export PATH="$HOME/.local/bin:$PATH"` 加到 `~/.profile`。服务本身的 systemd PATH 已包含该目录。

## 5. Jira CLI 认证与附件凭据

在 WSL 中配置，不能只配置 Windows 的 Jira CLI。初始化选用公司 Jira Server / https://jira.huami.com、自己的登录账号和所需默认项目。Jira 读取、工单评论始终通过 CLI。

本环境验证可用的是 **basic + 登录密码**。先用一次性隐藏输入验证，不把密码写进命令历史：

```bash
(
  read -rsp 'Jira 登录密码: ' JIRA_API_TOKEN
  echo
  export JIRA_API_TOKEN JIRA_AUTH_TYPE=basic
  jira init --installation local --server https://jira.huami.com --auth-type basic
  jira me
  jira issue list --plain --paginate 1
)
```

成功后用本机编辑器配置 `~/.netrc`（已有文件时只增补/检查对应 machine，不覆盖其他主机）：

```text
machine jira.huami.com
  login 你的Jira登录账号
  password 你的Jira登录密码
```

```bash
chmod 600 "$HOME/.netrc"
JIRA_AUTH_TYPE=basic jira me
```

netrc 文件不是 shell 脚本，密码包含空格或特殊符号时应使用 netrc 支持的引号/转义语法。不要把密码发给机器人、写进部署文档或打包。

**验收**：退出刚才的子 shell 后，`JIRA_AUTH_TYPE=basic jira me` 仍成功。若只有临时输入密码时成功而 netrc 返回 401，重点核对 machine、login 和保存的 password 是否完全一致。不要只用“网页登录成功”证明 CLI 凭据正确。附件下载/上传因为当前 CLI 不支持文件通道，使用固定 Jira origin 的 curl 并复用这一份 netrc，不另建密码库。

## 6. elf-fetch、符号与固件源码

```bash
elf-fetch --help
elf_fetch init
elf-fetch auth status
```

`init` 使用自己的 CI 授权，具体支持用户名/密码或 token；以工具提示为准，不要把 CI 密码当成 Jira 密码。WSL 的浏览器授权可用工具包 `elf-fetch/vscode-auth-helper` 源码构建 VS Code helper，构建步骤见该目录 README 及 [elf-fetch 官方仓库](https://github.com/active-ailab/elf_fetch)。helper 的 Node/npm/VSIX 构建不是基础服务安装的必需步骤；已有可用 CI 凭据可直接走 init。

用自己有权限的真实设备/版本验证下载；把下方占位符替换掉：

```bash
elf-fetch --device 设备标识 --version 固件版本 --files xml,elf,map --dir "$HOME/firmware-artifacts"
```

验收应找到 XML manifest、有效 ELF、MAP，不是 HTML 登录页或空文件。安装器提供 ARM/RISC-V addr2line 和 readelf；是否完整还原现场取决于符号版本、dump 格式和原始证据。

固件源码请按团队已有 Gerrit/repo 流程，在 `~/firmware` 获取自己的授权工作区。需让 manifest 中目标项目的提交对象可被 Git 读取；只 clone 当前浅层分支可能缺失历史节点。SSH key 自己生成/注册，禁止打包私钥。本包包含 repo launcher，但不包含项目 manifest URL、私有代码或 SSH 授权。

尚未准备源码时可以 `mkdir -p "$HOME/firmware"`，先联调网页/Jira；源码对照自检会提示未完成。不要把这时的分析结果当成已经结合目标版本代码验证。不要对日常开发目录执行自动清理或强制同步。

## 7. 安装和登录 Codex，检查接口兼容

在 **WSL** 按 [OpenAI 官方 WSL 安装说明](https://learn.chatgpt.com/docs/windows/wsl) 安装 Codex：

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
codex --version
codex login
codex login status
```

浏览器回调受限时，按 [官方认证文档](https://learn.chatgpt.com/docs/auth?surface=cli) 使用 `codex login --device-auth`；该方式取决于账号/组织是否允许。Windows 客户端已有登录，不代表 WSL 内 CLI 已登录。不要在部署包里复制原机 auth 缓存。

当前分析适配器依赖 Codex exec 的 `--ignore-user-config`、结构化 JSON schema、临时运行和工具禁用参数。原机验证版本为桌面应用内置 `codex-cli 0.166.0-alpha.5.1`，**不把该内置二进制直接复制给其他电脑**。目标机官方 CLI 的具体支持情况必须运行第 9 节自检，并实际执行一次分析确认。自检失败时先检查/更新官方 CLI；仍不兼容则需要适配该版本，不能删除隔离参数凑合启动。可查 [官方 CLI 参数参考](https://learn.chatgpt.com/docs/cli/reference)。

AI 使用登录账号的额度与模型配置；没有成功输出时应显示“分析失败”，不能生成“证据不足”的假结论。看板部署成功并不自动证明 AI 已可用。

代理可选：若代理在 Windows 上，默认 NAT 模式中的 WSL `127.0.0.1` 通常指向 WSL 自己；通过 `ip route show default` 查 Windows 网关，并确保 Windows 代理允许该连接。镜像模式的 localhost 行为不同。不要照抄旧机器 `127.0.0.1:7890`。参见 [Microsoft WSL 网络说明](https://learn.microsoft.com/en-us/windows/wsl/networking)。

## 8. 创建配置和启动服务

先确定 **Windows 的局域网 IPv4**，例如 192.168.1.20。下列 URL 必须换成你自己的，端口保持 8791：

```bash
cd "$HOME/apps/jira-auto-service"
mkdir -p "$HOME/firmware"
python3 deploy/configure.py \
  --source-dir "$HOME/firmware" \
  --dashboard-url http://192.168.1.20:8791 \
  --codex-bin "$HOME/.local/bin/codex"
```

若 `command -v codex` 返回其他路径，使用实际绝对路径。需要代理时加 `--ai-proxy http://可从WSL访问的代理地址:端口`。

配置脚本生成权限 0600 的 `~/.config/jira-auto/service.env`，拒绝覆盖已有配置，并生成 Windows MCP 示例。关键项：

- **JA_HOST=0.0.0.0**：监听 WSL 全部接口
- **JA_ALLOWED_HOSTS**：localhost、127.0.0.1 和 Windows LAN IP/域名，逗号分隔，不带协议和端口
- **JA_SOURCE_DIR**：固件工作区的 Linux 绝对路径
- **JA_DATA_DIR**：当前应用的 data 目录
- **JA_LAN_AUTH=0**：按当前需求不启用用户名密码登录
- **JA_ALLOW_JIRA_WRITE=0**：先只读联调，完成审批联调后再明确开启
- **JA_CODEX_BIN / JA_AI_PROXY**：模型 CLI 路径及可选代理
- **JIRA_AUTH_TYPE=basic**：后台 CLI 使用 basic 认证


```bash
.venv/bin/python scripts/install_service.py
sudo loginctl enable-linger "$USER"
systemctl --user status jira-auto.service --no-pager
curl --fail http://127.0.0.1:8791/api/status
```

安装器打印的 localhost 地址是本机验收入口，局域网入口由 Windows 网络配置提供。修改 service.env 后执行 `systemctl --user restart jira-auto.service`。环境文件由 systemd 读取，不要直接 `source` 成 shell 脚本。

若系统报 `Failed to connect to bus`，先确认当前 shell 是普通 WSL 用户、PID 1 为 systemd，重新进入 WSL。不要改成 `sudo systemctl --user`；那会针对另一个用户。

## 9. 自检与网页验收

```bash
cd "$HOME/apps/jira-auto-service"
export PATH="$HOME/.local/bin:$PATH"
.venv/bin/python deploy/doctor.py --online
journalctl --user -u jira-auto.service -n 80 --no-pager
```

doctor 只检查，不上传附件、不发布评论，也不打印认证响应。它检查 Python、工具、Codex 登录与接口、固件 .repo、systemd、Jira 认证及网页状态。它不会消耗模型额度做真实 AI 请求；所以所有检查通过之后仍需做下面的实际分析验收。

浏览器先在 Windows 打开 `http://localhost:8791`；然后按第 10 节开放 LAN，从另一台电脑访问 `http://Windows局域网IP:8791`。

验收顺序：

1. 看板 Jira 接入正常，名下工单可同步；手动输入一个有权限的 Jira key 能加入分析列表。
2. “接入与配置”点击“连接质量平台账号”，完成原平台 SSO，回到同一看板 origin 显示已连接。
3. 选择一个未发布的新测试工单，执行分析：查看过程显示附件下载/质量采集、解包、逐文件检查、版本准备、AI 阶段；材料页面可追溯来源。
4. 有质量反馈且 Jira 缺日志时，确认原包归档到 Jira 后继续分析；已有附件优先复用，不为了验收重复上传。
5. 崩溃例检查核心现场日志和符号化调用栈、版本来源、解析限制；普通问题检查现象→日志/截图→代码→结论。
6. 产出“待审稿”；AI 调用错误必须显示失败；现有证据不足可形成有边界的待审结论。
7. 飞书绑定后能收到卡片并通过“验证回调（不评论）”联调；验收不点击真实审批发布。
8. 今日列表最多 3 项，超过 6 个自然月的截止日期不参与推荐；北京时间 09:00 后完整同步触发当天提醒。服务离线不会补发过去日期。

质量登录回调必须能从当前浏览器访问看板地址，且 Host/Origin 在白名单；换 IP 后应更新 service.env 和飞书配置中的看板 URL，再重新连接质量平台。不要修改票据校验或复制浏览器 Cookie 来绕过。

## 10. 局域网访问：NAT / 镜像二选一

**0.0.0.0 只是 WSL 内监听，不能单独保证其他电脑能访问。** 下面两种方案选一种。当前服务不登录，仅供你已授权的内网使用；脚本只开 TCP 8791 的局域网范围，不关闭整个 Windows 防火墙。

### 10.1 默认 NAT 模式

在 Windows 管理员 PowerShell 中进入本包 `jira-auto-service/deploy` 所在的 Windows 可访问目录，例如从 WSL 复制脚本到 Downloads，然后：

```powershell
powershell -File .\windows-lan.ps1 -Distro Ubuntu-24.04 -ListenAddress 192.168.1.20 -Mode Nat -WhatIf
powershell -File .\windows-lan.ps1 -Distro Ubuntu-24.04 -ListenAddress 192.168.1.20 -Mode Nat
netsh interface portproxy show v4tov4
```

脚本把 Windows LAN IP:8791 转发到当前 WSL IPv4:8791，创建 Private/Domain、指定 LAN 网卡子网范围的防火墙规则。需要管理员权限；若域策略禁止设置，联系管理员，不要禁用防护。Windows 的网络类型应为合适的 Private/Domain；Public 网络不在脚本规则范围。

WSL 重启后 IP 可能改变，要重跑 NAT 脚本刷新转发。Windows LAN IP 改变时，旧 listenaddress 转发也要检查清理，并更新服务白名单和看板 URL。检查 `Get-Service iphlpsvc`；Windows IP Helper 不运行会影响 portproxy。

### 10.2 Windows 11 镜像网络

适用于支持该功能的 Windows 11 22H2 及更新系统。在 Windows 用户 `%UserProfile%\.wslconfig` 的现有内容中合并：

```ini
[wsl2]
networkingMode=mirrored
```

先结束正在执行的任务，再 `wsl --shutdown` 并重进 Ubuntu。不要在分析/上传过程中强制重启 WSL。管理员 PowerShell 运行：

```powershell
powershell -File .\windows-lan.ps1 -ListenAddress 192.168.1.20 -Mode Mirrored -WhatIf
powershell -File .\windows-lan.ps1 -ListenAddress 192.168.1.20 -Mode Mirrored
```

镜像模式不设置 NAT portproxy，而是添加该端口的 Windows/WSL Hyper-V 防火墙规则；需要系统支持相应 cmdlet。企业 VPN 可能影响兼容性。规则完成后必须从另一台 LAN 电脑实际验证，不能用 Windows localhost 成功替代。

两种模式的原理与官方配置参见 [Microsoft WSL networking](https://learn.microsoft.com/en-us/windows/wsl/networking)。

## 11. 后台持续运行和 Windows 登录后自动启动

`systemctl --user enable` 和 linger 只管理 Linux 用户服务，**不保证 Windows 开机时 WSL 已启动，也不能让关机/睡眠中的电脑工作**。

先用 Windows 普通 PowerShell 手动验证常驻入口（替换 Linux 用户名）：

```powershell
wsl.exe -d Ubuntu-24.04 -- bash /home/你的WSL用户名/apps/jira-auto-service/deploy/keep-alive.sh
```

该命令启动用户服务后保持前台进程，窗口关闭会结束保活入口。实际长期开启可在 Windows“任务计划程序”创建任务：

- 名称 Jira Auto WSL；使用拥有该 WSL 发行版的 Windows 用户。
- 触发器：该用户登录时，可延迟 30 秒。
- 操作：程序 `C:\Windows\System32\wsl.exe`；参数 `-d Ubuntu-24.04 -- bash /home/你的WSL用户名/apps/jira-auto-service/deploy/keep-alive.sh`。
- 设置：任务已经运行时“不启动新实例”；取消“运行超过 3 天停止”；按需要关闭“切换到电池停止”等条件。
- 启动一次任务，检查任务处于运行、WSL 服务 active、LAN 看板可访问，再重启 Windows 做登录后验收。

这提供“登录后运行”。若需要无人登录也运行，须另行配置任务身份、Windows 账户和组织策略，并验证该账户能访问同一 WSL 发行版；不在此包中保存 Windows 密码。NAT 模式还需在 WSL 网络就绪后以管理员权限刷新转发；不要认为普通用户保活任务同时能更新 netsh。

## 12. 飞书专用机器人、审批和 MCP

### 飞书应用

推广给同事建议每人一个独立机器人应用/独立实例和自己的 Jira 身份。若迁移你当前的机器人，按第 13 节切换，不能两台实例同时连接同一应用等待同一绑定消息。

在飞书开放平台创建企业自建应用并开启机器人能力。按平台申请并发布所需权限：发送单聊消息、接收用户与机器人单聊消息；将使用者加入应用可用范围。事件订阅长连接添加 `im.message.receive_v1`；卡片回调长连接添加 `card.action.trigger`。事件和回调可能在不同配置页，各自都要设置/发布；机器人客户端里能搜索到不代表权限/回调配置已生效。

WSL 本机运行：

```bash
cd "$HOME/apps/jira-auto-service"
.venv/bin/python scripts/configure_feishu.py
```

依次输入新应用 App ID、隐藏的 App Secret、LAN 看板 URL。脚本会短暂停止并重启用户服务，秘密仅保存在当前实例 data/feishu.json。若 service.env 自定义了 JA_DATA_DIR，该命令也应在相同环境下运行；本部署向导默认就是应用目录 data。

将终端输出的整行 `绑定 jira-auto ...` 私聊发给此机器人，不要发群里或本指南。收到“绑定成功”后才算完成接收人绑定；需要重新显示当前口令：

```bash
.venv/bin/python scripts/configure_feishu.py --show-pairing
```

网页接入状态和服务日志确认绑定。长连接无需公网回调 URL；看板链接仍要求手机/电脑可到达该 LAN。验证卡片按钮回调成功后再启用评论：编辑 service.env，把 `JA_ALLOW_JIRA_WRITE="0"` 改为 `"1"`，重启服务。这样仍只允许你审批的那一版稿件发布，不会自动流转 Jira。

### Windows 本机 MCP 客户端

`deploy/configure.py` 生成 `deploy/mcp-windows.generated.json`。其中使用 Windows `wsl.exe` 启动 Linux `.venv/bin/jira-auto-mcp`，形如：

```json
{
  "mcpServers": {
    "jira-auto": {
      "command": "wsl.exe",
      "args": ["-d", "Ubuntu-24.04", "--", "/home/你的WSL用户名/apps/jira-auto-service/.venv/bin/jira-auto-mcp"]
    }
  }
}
```

按客户端支持的 MCP 配置格式导入/调整；部分客户端顶层结构不同。WSL 内客户端可直接运行 Linux 入口。服务需已启动，MCP 默认连接 WSL 本机 http://127.0.0.1:8791。不要把 plugin/mcp.json 中 Linux `uv` 命令直接复制给 Windows 原生客户端；不要为了连接把只允许 loopback 的桥接校验取消。当前提供的是可安装桥接/技能源码，不是已发布到 ChatGPT 应用目录的公网插件。

## 13. 从原电脑迁移的切换顺序

1. 新机按本指南安装干净实例，Jira 写入保持关闭，飞书先不启用；完成 Jira、CI、Codex、质量平台和一次实际分析验收。
2. 原机有任务/上传正在执行时等待结束；记录需要保留的历史分析链接。不要拷贝正在写入中的 SQLite 文件。
3. 使用同一飞书应用时，原机执行 `systemctl --user stop jira-auto.service`，确认旧长连接断开，再在新机运行 configure_feishu.py 并重新绑定。旧卡片不能代表新机报告审批，不点击旧卡片。
4. 新机设置正确看板 URL、LAN 白名单和 Windows 网络规则；验证回调、分析草稿和推荐列表。
5. 新机确认后按需开启 Jira 评论写入。旧机保持停止，避免重复分析/推荐/消息。新实例没有旧提醒收据，切换当天可能重新生成当天卡片；若不希望额外提醒，可在当天提醒发送后次日再切换机器人。
6. 需要回退时先停新机，再启旧机；不要两边同时运行同一机器人。新机已发布的评论不能靠删除新机目录撤销。

历史数据库/附件迁移须单独设计：路径、证据哈希、报告版本、审批状态和上传/发送收据都需要校验，不能把本次“无凭据分发包”当成全量备份。

## 14. 排错、维护和更新

- **Jira 401**：netrc machine/login/password、basic 类型、是否只在临时环境变量里验证成功
- **localhost 可访问，其他电脑失败**：JA_HOST、JA_ALLOWED_HOSTS、Windows LAN IP、NAT 转发/WSL IP、Windows/Hyper-V 防火墙、VPN
- **Invalid host / Origin / 403**：使用的地址是否在 JA_ALLOWED_HOSTS；改配置并重启，不能关闭校验
- **点击质量登录后无法回来**：当前看板 origin 可达、认证重定向可达、10 分钟一次性登录是否过期
- **elf-fetch 失败或产物不匹配**：CI 权限、设备映射、版本号、授权状态；XML/ELF 必须与现场对应
- **Codex 找不到/登录失败**：WSL 实际安装路径、服务 PATH、WSL 登录、代理、CLI 参数兼容
- **飞书发消息但不绑定**：是否私聊正确应用、当前口令、单聊消息权限、事件发布状态、是否有另一台实例争抢
- **卡片无响应**：card.action.trigger 回调长连接、版本发布、服务在线、旧卡片是否失效
- **WSL 重启后 LAN 失联**：NAT IP 变化需重跑 windows-lan.ps1；用户服务、保活任务是否运行
- **每日提醒没发**：上海 09:00 后服务在线、完整新鲜同步、机器人绑定、是否已发送/结果未知；不要手工删收据强行重发


常用命令：

```bash
systemctl --user restart jira-auto.service
systemctl --user stop jira-auto.service
journalctl --user -u jira-auto.service -f
.venv/bin/python deploy/doctor.py --online
uv sync --frozen --extra test
.venv/bin/pytest -q
```

更新前停止服务并在你自己的私有存储备份整个 data、service.env、Jira/CI/Codex 配置和当前代码。备份含秘密，不上传分发文档。新版本先在独立目录验证，不直接解压覆盖当前数据目录；当前没有通用的数据库降级工具，升级后不要无检查回退旧程序读新数据库。

## 15. 本交付验证范围

交付前在当前 Linux 环境检查压缩包成员、工具 SHA256、脚本语法、配置生成、源码依赖安装和应用回归测试。最终验证结果见飞书文档末尾“交付验证记录”。没有连接目标 Windows/WSL 机器，不能声称已验证其防火墙、企业 VPN、任务计划或真实账号权限；目标机按第 9 节逐项验收。

推广方式建议：先给一位同事按此包搭建独立实例，记录 Jira/CI/Codex/飞书各自接入耗时；完成实际分析和卡片闭环后，再做版本化发布和升级机制。多人共用中央服务还需要账号隔离、作业隔离和独立审批路由，当前包不宣称已具备这些能力。

## 16. 交付验证记录

- 原工作目录回归：193 passed；分发源码在新目录按 uv.lock 重新安装后回归：193 passed（3 个上游弃用警告）。
- app.js/review.js 语法、git diff 空白检查、Bash 安装脚本语法检查通过。
- 隔离 HOME 验证配置生成、0600 权限、LAN 监听、默认禁用 Jira 评论、拒绝覆盖现有配置通过。
- ZIP CRC、压缩成员白名单、实际工具版本检查通过；与本机已保存的密码/Secret/质量会话逐字比对，未发现凭据进入包内（不输出秘密）。
- 当前没有目标 Windows/WSL 测试环境；PowerShell、Windows 防火墙、任务计划、目标 CLI 模型兼容及真实账号闭环待目标机验收。
- 当前服务保持运行，本次未审批或发布任何真实 Jira 评论，未修改现有飞书绑定。
