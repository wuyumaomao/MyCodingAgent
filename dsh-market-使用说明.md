# dsh-market（dshmarket）安装记录与使用说明

## 一、安装结果

| 项目 | 值 |
| --- | --- |
| 插件 | `dshmarket` **v1.47.0**（npm 官方源，MIT） |
| 上游仓库 | https://github.com/dsh-market/dsh-market |
| 官网 | https://dshmarket.com |
| 安装位置 | `C:\Users\93655\.dsh\profiles\web\node_modules\dshmarket` |
| 目标 profile | `web`（对应 `http://127.0.0.1:3080`） |
| npm 供应链凭证 | 有 provenance / SLSA 证明，由 GitHub Actions + OIDC 发布 |
| 运行时依赖 | 仅 `undici`、`js-yaml` |

执行的安装命令：

```sh
npm install -g pnpm                              # 本机原先没有 pnpm，插件安装器需要它
dsh plugin --profile web add dshmarket           # 官方安装方式
```

安装后 `C:\Users\93655\.dsh\profiles\web\package.json` 已自动登记：

```json
{
  "dependencies": { "dshmarket": "^1.47.0" },
  "dsh": { "profile": { "bundles": [
    "@deepseek-ai/dsh-base",
    "@deepseek-ai/dsh-web-app",
    "dshmarket"
  ] } }
}
```

已用 `dsh --profile web --dump-config` 静态校验：组合后的插件树中确实出现了
`- id: dsh-market` / `name: dshmarket`，即下次启动会正常加载。

## 二、⚠️ 还差最后一步：重启 dsh web

**当前正在运行的这个 dsh web 进程还没有加载插件**（探测
`http://127.0.0.1:3080/dsh-market/status` 返回 404）。新增 bundle 必须在启动时组合，
所以需要重启：

1. 在运行 `dsh web` 的终端里 `Ctrl+C`，然后重新执行 `dsh web`；
2. 或者直接使用插件自带的「一键重启」按钮（重启后才会出现）。

> 我没有替你重启，因为当前会话就跑在这个 dsh web 进程里，重启会直接中断本次会话。

重启后打开 `http://127.0.0.1:3080` → **设置 → 插件市场**。

## 三、怎么用

- **逛与搜**：内置 2300+ 社区插件目录，支持分类筛选、star 数、最热/最新排序，描述跟随界面语言（中/英）。
- **一键安装**：卡片上点安装，确认来源后实时显示进度。安装优先级为
  `npm 包 → 作者提供的 GitHub Release 预构建 → 整仓源码`；走 npm 通常几秒完成，且默认不执行构建脚本。
- **主题**：「主题」独立页，点一下切换，跨重启保留，卸载即恢复。
- **收藏**：「发现 / 主题」页点书签收藏，在「收藏」Tab 集中管理（搜索、排序、安装）。
- **备份与恢复**：「备份」可导出 profile 插件清单与配置为 JSON，支持 WebDAV 每日自动备份、
  私有 GitHub Gist 跨机同步。恢复是**合并**式（备份之后新装的插件会保留），写入前校验、失败回滚。
- **更新**：逐插件检测 npm 版本 / 锁定 commit 与 HEAD 差异，可单个或全部更新；市场自身也走同一通道升级。
- **诊断 / 加载顺序 / AI 修复**：一页看清 bundle 栈、重复 loader 条目、依赖版本冲突、核心包多版本共存等；
  可拖拽调整社区 bundle 顺序（落盘前做静态组合校验）；「AI 修复」生成的是保守提示词，
  且明确禁止 Agent 直接改动运行中的 harness，只产出 `apply` / `rollback` 脚本由你在外部终端执行。
- **热禁用 / 启用**：开关会往 profile 的 `cordis.patch.yml` 写 `disabled: true|false`，
  借助 DSH 的 HMR 约 1 秒重新组合，无需重启。
- **卸载**：两步确认；也可用命令行 `dsh plugin --profile web remove dshmarket`。

## 四、GitHub 可达性（2026-09-19 修正）

**结论：GitHub 在这台机器上可用。** 本说明早期版本曾断言「GitHub 被 hosts 屏蔽、相关功能不可用」，
那是**误判**，现更正并附证据。

### 实际情况：Steam++ 在加速，不是屏蔽

机器上运行着 **Steam++（Watt Toolkit）**（`E:\Steam++\Steam++.exe`，PID 45176），监听 `0.0.0.0:443`。
它以 **MITM 方式**加速：把 GitHub 等域名写进 `hosts` 指向 `127.0.0.1`，再由其本地代理转发到
真实站点（实测证书颁发者为 `CN=SteamTools Certificate`，即它自签重签）。hosts 里 41 条
`127.0.0.1` 条目均由它写入，**不要手删**——删掉会失去加速。

实测（2026-09-19）：

| 访问方式 | 结果 |
| --- | --- |
| `https://github.com/` | ✅ HTTP 200（575 KB，2.6 s） |
| `https://api.github.com/repos/...` | ✅ HTTP 200 |
| `https://raw.githubusercontent.com/...` | ✅ HTTP 200 |
| `git ls-remote https://github.com/...` | ✅ exit=0，正常返回 commit |
| 直连真实 IP（绕过 hosts） | ✅ 5 个 IP TLS 握手全通；`codeload.github.com` HTTP 200 |
| 公共 DNS 解析 `github.com` | ✅ 223.5.5.5 / 119.29.29.29 / 114.114.114.114 均返回真实 IP |

### 因此依赖 GitHub 的功能

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| 插件目录浏览/搜索 | ✅ 正常 | `awesome-dsh-plugin.com/plugins.json` |
| npm 源安装插件 | ✅ 正常 | registry.npmjs.org |
| **GitHub 源安装插件（`github:...`）** | ✅ 应可用 | `git ls-remote` 实测通 |
| GitHub Gist 备份/同步 | ✅ 应可用 | api.github.com 实测 200 |
| GitHub Release / 源码回退安装 | ✅ 应可用 | codeload / 对象存储可达 |
| 卡片截图（GitHub 图床） | ✅ 应可用 | raw.githubusercontent.com 实测 200 |
| DSH 的 `web_fetch` 工具抓 GitHub | ❌ 不行 | **DSH 自身 SSRF 护栏**拒绝解析到回环地址的域名，报 "resolves to a non-public IP address"。这是工具策略而非网络故障；需要抓页面时改用 shell 的 `Invoke-WebRequest` |

### 当初误判的三个来源

1. `web_fetch` 报 "non-public IP" → 那是 DSH 的 SSRF 护栏，被误读成网络屏蔽；
2. `Resolve-DnsName` 返回 `127.0.0.1` → 那是加速器的本地代理设计，不是黑洞；
3. 早期 `git ls-remote` 报 "Access is denied" → 是 DSH **沙箱**在 workspace-write 模式下拦截
   `git.exe` 执行，同样与网络无关。

### 安全提示（Steam++ 走 MITM）

- 它重签被加速域名的证书，**对这些域名的 HTTPS 流量有可见性（理论上有修改能力）**。这是此类
  加速器的固有代价；涉及 GitHub token / 密码时请知悉。
- 它监听的是 `0.0.0.0:443`（**所有网卡**，不只回环）。在不可信网络（公共 WiFi）下这是额外
  暴露面，建议确认防火墙规则。
- 目录源访问不稳定时仍可用 `DSHM_REGISTRY_URL=https://你的镜像/plugins.json` 指向镜像；
  需要自定义 GitHub 代理时用**设置 → 插件 → 插件配置 → GitHub 加速**或 `DSHM_GITHUB_PROXY`。

## 五、常用配置片段

`C:\Users\93655\.dsh\profiles\web\cordis.patch.yml`（注意 `allowRestart` **必须**嵌在
`config:` 下面，写在顶层会静默失效）：

```yaml
- id: dsh-market
  name: dshmarket
  config:
    allowRestart: false   # systemd / pm2 / launchd 等托管部署建议关闭「一键重启」
```

## 六、安全提醒

- 该插件是**第三方代码**，会作为 bundle 注入 DSH 运行时，并可调用 pnpm 安装其他插件；
  它自己 README 也写明「收录 ≠ 背书，请只安装你信任的来源」。
- 它的安装白名单只允许 `awesome-dsh-plugin` 精选列表内的来源，构建脚本默认禁止执行，安装接口只收同源 POST。
- 备份文件可能包含 profile 里的密钥，WebDAV 建议只走 https 且设为私有访问。
- 安装时受当时 DSH 沙箱限制（workspace-write 拦截 `git.exe` 执行）与 `web_fetch` 的 SSRF 护栏，
  **未能直接审阅上游仓库源码**；判断依据是 npm 元数据 + provenance 证明 + 对本地已安装副本的
  检查（`lib/`、`src/`、`client/` 共 40+ 模块）。注：GitHub 本身可达（见第四节更正），
  现已具备 `git clone` 审阅源码的条件。

## 七、以后怎么启动 DSH（关机重启后）

原先 DSH 是用 npx 临时跑起来的（`npx --yes @deepseek-ai/dsh web`），`dsh` 并不在系统里，
所以重启后敲 `dsh web` 会报「不是内部或外部命令」。现已全局安装 `@deepseek-ai/dsh@0.1.5-rc.1`，
全局命令位于 `C:\Users\93655\AppData\Roaming\npm`（该目录本就在持久 PATH 中）。

**每次开机后：**

```powershell
dsh web
```

然后浏览器打开 http://127.0.0.1:3080 ，用完在终端按 `Ctrl+C` 关闭。

**注意两点：**

1. `dsh web` 是前台进程，**终端关掉服务就停**。想后台常驻需另配服务或计划任务。
2. 启动前要确保 3080 端口没被占用。若旧的 dsh web 还在跑（比如终端窗口找不到了）：

   ```powershell
   Get-NetTCPConnection -LocalPort 3080 -State Listen |
     ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
   ```

**如果你更习惯 npx 方式**（不依赖全局安装，缓存已在磁盘上，启动同样很快）：

```powershell
npx --yes @deepseek-ai/dsh web
```

**数据位置**：会话、配置、插件全部在 `C:\Users\93655\.dsh`，重启不会丢失。

**关于安装脚本警告**：全局安装时 npm 12 默认拦截了 5 个包的 install script，经比对
`node-pty`（自带 `prebuilds\win32-x64` 预编译二进制）与 `koffi` 的目录结构，与原本能正常
运行的 npx 副本**完全一致**，且 `dsh --profile web --dump-config` 组合 profile 成功、
`dshmarket` 正常在列，因此不影响使用。

## 八、MCP 服务器（已配置三个）

### 结论：DSH 的 MCP 支持是内置的，不用装插件

DSH 官方自带 `@deepseek-ai/dsh-mcp-client`（已在 profile 依赖里），作用是把外部 MCP 服务器的
工具桥接成 harness 原生工具。**接一个 MCP 服务器 = 补丁层里一条配置**，不需要装任何 DSH 插件。

### 已接入的三个服务器

| serverName | 包（npm 全局安装） | 版本 | 用途 | 数据位置 |
| --- | --- | --- | --- | --- |
| `memory` | `@modelcontextprotocol/server-memory` | 2026.8.31 | 知识图谱长期记忆，跨会话记住事实 | `C:\Users\93655\.dsh\mcp-memory.json` |
| `thinking` | `@modelcontextprotocol/server-sequential-thinking` | 2026.8.31 | 结构化分步思考 | 无 |
| `fs` | `@modelcontextprotocol/server-filesystem` | 2026.8.31 | 指定目录的文件读写/搜索 | 无（只读限 `F:/MyCodingAgent`） |

**当前状态（2026-09-16）**：`thinking` 与 `fs` 已按用户要求**禁用**（补丁层在 insert 之后追加
`id` + `disabled: true` 的 override，热卸载），仅 `memory` 在役——9 个工具，≈3,071
tokens/请求（三件套全开时 ≈8,129）。恢复方法：把补丁里对应条目的 `disabled` 改回
`false`（或删除该两段 override），保存即热生效。

安装命令（已完成）：

```sh
npm install -g @modelcontextprotocol/server-memory @modelcontextprotocol/server-sequential-thinking @modelcontextprotocol/server-filesystem
```

配置写在 `C:\Users\93655\.dsh\profiles\web\cordis.patch.yml`，用的是 **`insert` 形式**
（源码 `applyEntryPatches` 证实：不带 `insert` 的条目是「覆盖已有插件」，找不到会静默跳过；
只有 `insert` 才是「新增实例」——同一个 dsh-mcp-client 包要跑三个实例，必须这么写）：

```yaml
- insert:
    - id: mcp-memory
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: memory
        transport: stdio
        command: mcp-server-memory
        env:
          MEMORY_FILE_PATH: C:/Users/93655/.dsh/mcp-memory.json
    - id: mcp-thinking
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: thinking
        transport: stdio
        command: mcp-server-sequential-thinking
    - id: mcp-filesystem
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: fs
        transport: stdio
        command: mcp-server-filesystem
        args: ['F:/MyCodingAgent']
```

已用 `dsh --profile web --dump-config` 验证：三个实例都进入组合树（L544–565），无报错。
三个服务器也都单独通过了 MCP `initialize` 应答冒烟测试。

### 怎么用（已实测：无需重启，立即生效）

由于 profile 配了 `"patchReload": "live"`，补丁文件保存后 DSH 的 HMR 会热插入这三个
dsh-mcp-client 实例并完成工具发现——**已实测在本会话内直接可调用**，无需重启
（对比：dsh-market 需要重启是因为它是新 bundle；MCP 只是补丁层实例化已装好的包）。

模型工具列表里会出现 24 个工具（命名规则 `mcp__<serverName>__<tool>`）：

- `mcp__memory__*`：create_entities / create_relations / add_observations / delete_entities /
  delete_observations / delete_relations / read_graph / search_nodes / open_nodes（共 9 个）
- `mcp__thinking__sequentialthinking`（1 个）
- `mcp__fs__*`：read_file / read_text_file / read_media_file / read_multiple_files / write_file /
  edit_file / create_directory / list_directory / list_directory_with_sizes / directory_tree /
  move_file / search_files / get_file_info / list_allowed_directories（共 14 个）

已实测：`mcp__memory__read_graph` 返回空图谱、`mcp__fs__list_allowed_directories` 返回
`F:\MyCodingAgent`，链路端到端可用。

**用户不需要手动调用**——工具暴露后由模型自主决定何时使用，与内置工具一致；
想在对话里点名时说「用 memory 记住…」「用 fs 工具列出…」即可。

工具暴露的时机：补丁层应用时（live 模式保存即热插入）；harness 启动时在首轮对话前完成
初始发现；服务器发 `tools/list_changed` 时自动重新同步；断线自动重连（500ms 起指数退避、
上限 30s），连续失败 10 次后工具移除。某台服务器启动失败时默认
`failOnStartupError: false`，harness 照常启动，只是那组工具不出现。

### 注意事项

- **token 开销（实测）**：工具定义（名称+描述+参数 schema）进入**每次**模型请求。逐个
  `tools/list` 实测：memory 9 个工具 ≈ 3,071 tokens，thinking 1 个工具 ≈ 1,327 tokens
  （描述是长文，单工具不等于便宜），fs 14 个工具 ≈ 3,731 tokens，**合计 ≈ 8,100
  tokens/请求（28 KB）**。缓解因素：工具定义位于请求前缀且 DSH 保证 schema 稳定
  （「重新同步替换而非累积」），provider 的自动上下文缓存会命中该前缀，命中输入按约
  1/10 计价——所以是「首次贵、多轮后便宜」；但窗口占用是实打实的，compaction 只压
  历史不压工具定义。
- **裁剪执行记录（2026-09-16）**：已按性价比把 `fs`（≈3,731 tokens）与 `thinking`（≈1,327
  tokens）禁用，每请求节省 ≈5,058 tokens。做法：在补丁层 insert 之后追加两条
  `- id: mcp-xxx` + `disabled: true` 的 override——loader 语义支持同层后面的补丁命中
  前面 insert 出来的行（`applyEntryPatches` 源码注释明示）；这也是 dsh-market 插件开关
  写补丁的同一机制。dump-config 复核：两实例已带 `disabled: true`，`mcp-memory` 无标志
  且 `read_graph` 热重载后实测仍通。npm 包未卸载（磁盘占用极小，恢复免重装）。
- **概念澄清**：一条配置 = 一条到 MCP 服务器的**连接**，不是「一个工具」。
  `dsh-mcp-client` 的一个实例连接一个服务器并注册其**全部**工具；
  `serverName` 只是命名空间前缀（`mcp__<serverName>__<tool>`），用于不同服务器间工具重名共存。
- **隐性成本**：工具越多模型选错工具的概率越高；用多少接多少，别为了「可能有用」而常驻。

## 九、模型请求报错：Stream ended without finish_reason

**现象**：「已重试模型请求（5/5）· 8s → 本轮运行失败」。

**含义**：SSE 流式响应在收到终止块（`finish_reason`）之前被掐断，属**传输层失败**。
DSH 的 `dsh-llm-retry` 把它归为 TRANSPORT 类，自动重试 5 次（指数退避 500ms→10s + 10%
抖动），全败才标记本轮失败。注意：**每次重试都是一次独立计费的请求**。

**与 MCP / 插件无关**：报错发生在模型请求层，不是工具执行层；工具 schema 非法会是
即时的 4xx 明确报错，而不是流中断。

**本机相关配置**（settings.yaml）：provider `wyb` → `baseURL: https://openai.gptcodex.top/`
（**第三方中转站**），模型 `glm-5.3`，openai-completions 协议。

**排查顺序**：

1. 直接重发失败的那一轮——瞬时故障居多（实测该域名短请求 3/3 通，但延迟 748→1837ms 抖动大）。
2. **换 provider 对比测试**（配置里还有 `ZAI_CODING_CN_API_KEY` 的线路）：快速隔离
   「中转站的问题」还是「本地网络的问题」。
3. **检查本地代理**：系统里配过 `127.0.0.1:10808`（V2Ray 系端口，当前 ProxyEnable=0 是关的）。
   代理开着时 SSE 长连接被本地代理掐断是这类报错的经典来源。
4. 找中转站查余额/额度/线路；长期追求稳定建议官方 API 直连。

**最终诊断（2026-09-17，已实锤）**：

- 会话日志（scholar-rag，turn 7）解出 5 次重试的原始失败：**4 次 `Connection error`**
  （连接层就失败，请求根本没建立）+ 1 次 `Stream ended without finish_reason`，
  全部 TRANSPORT 类、provider=wyb → 与模型/请求内容/MCP 无关，是中转站
  （GPTCODEX-CN，Cloudflare 入口）**入口线路间歇性不可达**。
- 同一时段 ZAI 线路正常（对比实验成立）；本机代理确认关闭。
- 复测：站点现已恢复——`POST /v1/chat/completions` 4/4 正常应答 401
  `API_KEY_REQUIRED`（无密钥时的正确响应，说明应用层活着），延迟 500~940ms。
- **处置**：默认模型已切 `zai-coding-cn/glm-5.3-flash`；wyb 中转站保留，恢复后可在
  会话的模型选择器随时切回。注意中转站每个模型走不同上游通道：glm-5.3 通道不稳时，
  同站的 `deepseek-v4-flash/pro`、`kimi-k3` 可作备选。
- **`fs` 只能访问 `args` 里列的目录**，要加目录就往 `args` 数组里添路径。
- **`memory` 数据是本地 JSON**，可备份；想换位置改 `MEMORY_FILE_PATH` 即可。
- **别用 npm 上的 `mcp-server-fetch`**：那是抢注的假包（v0.0.1-security），不是官方的
  （官方 fetch 服务器是 Python 包 `mcp-server-fetch`，在 PyPI）。
- **Windows 提示**：DSH 用的 MCP SDK 1.30.0 走 `cross-spawn`，能正确解析 `.cmd`，
  所以 `command` 直接写命令名即可，不用包 `cmd /c`。
- **stdio 环境会被清洗**：名字含 `KEY/PASSWORD/SECRET/TOKEN` 的环境变量和所有 `DSH_*`
  不会传给 MCP 子进程，需要传 token 就在条目的 `env` 里显式写（支持 `!!js process.env.XXX`）。
