# Coding Agent

一个用于探索和构建 Coding Agent 的项目。

## 项目状态

项目处于受控操作 MVP 阶段，当前支持通过 CLI 读取、解释、在用户批准后修改本地 Git 仓库，以及运行受限的测试和脚本命令。

## 项目目标

逐步构建一个能够理解开发任务、构造上下文、调用工具并交付结果的 Coding Agent。

计划关注以下能力：

- 任务理解与澄清
- 计划生成与执行
- 代码库浏览和修改
- 测试与结果验证
- 可追踪的执行过程

## 快速开始

环境要求：Python 3.11+。

安装开发依赖：

```bash
uv sync --extra dev
```

配置模型服务：

复制 `.env.example` 为 `.env`，然后填写真实配置：

```bash
copy .env.example .env
```

`.env` 会被自动读取且不会提交到 Git。也可以直接设置同名环境变量。

运行查询：

```bash
coding-agent "解释这个仓库的启动和测试脚本"
coding-agent "package.json 里有哪些可用命令" --repo .
```

工具调用的限制按**重复**判定，而不是按次数：只读工具（`listfiles` / `readfile` / `search` / `find_files`）的**完全相同**调用再次出现时会被拒绝——成功过的调用说明结果已在上下文中，失败两次的调用再试也不会变。这两类返回 `repeated_tool_call` 结构化错误，模型可据此换参数或换工具。写工具和 `shell` 不参与重复判定（同一命令跑两次可能是合理的），且它们一旦成功就清空重复检测记忆；上下文压缩发生后同样清空，因为被压掉的旧结果允许重读。

`--max-tool-calls`（默认 30）退化为**安全阀**而非工作预算，`tool_call_limit` 只用于兜住异常行为。之所以改成这样：按工具计数会拦掉合法用法——读一个 574 行的文件需要 5~8 次区间读取——却拦不住真正有害的重复调用。

每个 run 默认最多执行 20 个 AgentLoop 轮次。该轮次预算在代码中固定管理；一次响应返回多个 tool calls 时，会在同一轮全部执行，不会额外消耗轮次。

模型请求在同一个 AgentLoop 轮次内默认最多重试 1 次（最多 2 次 API attempt）。重试使用完全相同的 `messages` 和工具定义，不会重置上下文、轮次或工具调用计数；工具执行错误仍作为 `role: tool` 结果回传模型，不属于 API retry。OpenAI SDK 内部重试已关闭（`max_retries=0`），因此实际重试由项目统一记录。

当前提供六个文件/搜索工具和一个受控命令工具：

- `listfiles`：列出仓库中的文件和目录。
- `readfile`：读取 UTF-8 文本文件，可用 `start`/`end` 指定行范围。
- `search`：搜索文件内容中的文本或正则表达式，返回匹配文件、行号和文本。
- `find_files`：按文件名或 glob 模式查找文件，例如 `*.py`、`*shell*`。
- `write_file`：创建文件或整体覆盖已有文件。
- `patch_file`：精确替换已有文件中唯一匹配的一段文本。
- `shell`：在批准后运行白名单中的仓库命令。

查找代码时，可以根据目标信息选择工具：

- 只知道代码功能或关键词，例如“找出排序脚本”或“哪里使用了 `ShellPolicy`”，使用 `search` 搜索文件内容，再用 `readfile` 阅读候选文件。
- 知道文件名的一部分或文件模式，例如“找所有 shell 相关的 Python 文件”，使用 `find_files` 搜索文件名或路径。

`search` 和 `find_files` 都是只读工具，不需要用户审批；它们内部可以使用 `rg` 或 Python 遍历，但不会调用对外暴露的高风险 `shell` 工具。

`write_file` 和 `patch_file` 都是高风险工具。模型提出调用后，CLI 会逐次显示仓库相对路径和受限预览；仅输入 `y` 或 `yes` 才会写入。覆盖已有文件会显示明确警告。无交互输入时，写工具返回 `approval_required`，不会修改文件。

写入只允许发生在仓库工作区内。解析后仍在工作区内的绝对路径或 `..` 路径可以使用；逃逸工作区、经过符号链接或超过 64 KiB 的写入会被拒绝。**父目录不存在时由工具自己建出来**，并把要建的目录列进审批预览（`Will also create directories: kb, kb/deep`）——这是"批准写文件"必须让人看见的副作用。早先的实现在这种情况下回一句 `Parent directory does not exist`，代价是模型为了建一个空目录花了 6 个整轮（两次 `python -c os.makedirs` 被拒 → 写临时脚本 → 跑它 → 才写成文件）。写入通过同目录临时文件和原子替换完成，替换失败时保留原文件。

`shell` 是受策略控制的命令工具。可以用自然语言要求 Agent 运行脚本或测试，例如“运行 `tools/repo_stats.py`”或“执行 `pytest tests/test_cli.py -q`”；也可以在请求中写出完整命令。只读 Git 查询（`status`、`diff`、`log`、`show`、`branch`、`rev-parse`）验证通过后自动执行，不打断用户；Python、pytest、npm 和 uv 命令仍会显示实际命令、仓库相对工作目录和超时，只有输入 `y` 或 `yes` 才会启动进程。无交互输入时返回 `approval_required`。

第一版只允许 `python`、`pytest`、`git`、`npm` 和受控的 `uv`：Python 可以运行工作区内的 `.py` 脚本，也可以走 `python -c <代码>`（需要审批，见下）；Git 只允许 `status`、`diff`、`log`、`show`、`branch`、`rev-parse`，这些只读查询自动放行；npm 只允许 `npm test` 与 `npm run <script>`；uv 只允许 `uv sync --dev`、`uv run pytest [参数]`、`uv run python <工作区内的 .py 脚本> [参数]`、`uv run python -c <代码>` 或兼容的 `uv run <工作区内的 .py 脚本> [参数]`，后几类仍需审批。目标仓库存在 `.venv` 时，Python/pytest 会优先使用目标解释器；环境缺失或依赖不完整时，Agent 可以请求批准后运行 `uv sync --dev`，完成后自动使用新环境。不支持任意 CMD/PowerShell 字符串、管道、重定向、命令连接、后台进程、其他 uv 命令、提交或推送。模型 API 默认超时为 600 秒（10 分钟）；shell 默认超时为 60 秒，可用 `--shell-timeout` 调整到最多 300 秒；stdout 与 stderr 各最多保留 64 KiB，超时会终止 Windows 进程树。

**`python -c` 走审批门，不走黑名单。** 这是踩过之后改的：硬拒它并不会让这个能力消失，只会让模型改走更难审计的路——真实 run 里它为了跑一行 `os.makedirs` 往仓库里写了 `_make_kb_dir.py`，跑完再写 `_cleanup_tmp.py` 删掉，来回 6 轮。审批门本来就是给"能力大但需要人看一眼"的命令准备的。安全性上有一点要说明白：**代码参数那一格要跳过 shell 元字符检查**——`;`、`|`、`>` 在 Python 源码里很常见，而 runner 用 `shell=False` + argv 列表启动进程，代码里的这些字符由 Python 解释，不构成命令分隔。其余 token（`cwd`、额外参数）照旧严查，所以 `python -c "..." && whoami` 仍然被拒。`python -m` 和裸 flag 仍然拒绝，消息里会指向可用写法。

工具调用的校验顺序是：`ResponseParser -> 工具查找 -> JSON Schema -> 工具安全检查 -> 执行`。模型返回的 arguments 先按注册时提供的 JSON Schema 在客户端校验；缺少必填字段、类型错误、数值越界或包含不允许的额外字段时，不会调用工具，而是把 `invalid_tool_arguments` 作为结构化 `role: tool` 结果回传给模型。Schema 只负责参数结构，工作区边界、文件存在性、权限和编码等运行时安全检查仍由具体工具负责。Provider 支持的 `strict` schema 只是额外约束，不能替代客户端校验。

库调用可以通过 `CodingAgent.from_settings()` 创建一个可复用的 Agent，再使用 `ask()` 执行独立任务：

```python
agent = CodingAgent.from_settings(repo, settings)
answer = agent.ask("解释项目结构")
```

`from_settings()` 负责装配 `Workspace`、模型客户端、工具注册表和循环依赖；每次 `ask()` 都会创建新的对话上下文、工具调用计数和事件接收器。若需要保存本次运行的 trace/report，应为每次 `ask()` 创建新的 `RunRecorder` 并显式传入。CLI 也通过同一 facade 启动 Agent。

每次运行都会组装三条静态 system 消息，顺序固定：

1. **agent 行为规范**：只能使用提供的工具、留在工作区内、写文件要直接调用工具而不是用文字请求批准。这一段跨仓库不变。
2. **仓库导航图**：`important_files`（README / pyproject / AGENTS.md 等元文件）、**`source_files`（源码文件 + 大小）**、`candidate_dirs`、git 状态和运行时环境。

`source_files` 这一节是补上的，因为原实现只列固定的元文件清单加硬编码的 `main.py`/`app.py`/`run.py`/`cli.py`——**入口叫别的名字的仓库一个源码文件都不在图上**。真实 run 里踩到：httpstat 仓库的入口是 `httpstat.py`，模型第一步只能发 `find_files httpstat.py` 去问"入口在哪"，而导航图存在的全部意义就是省掉这一步。现在图上直接有：

```
source_files:
- httpstat.py (19 KB)
- httpstat_test.sh (4 KB)
- tests/test_httpstat.py (11 KB)
```

带上大小是因为模型要据此判断"整读还是先 search 定位"——没有大小，一个 19 KB 的文件和一个 200 B 的文件看起来一样。清单按目录深度排序（浅的优先）、上限 20 条 / 1200 字符，因为它进的是**静态前缀**、每轮都要重发（实测整个导航图才 1016 字符）。

系统提示里也加了对应的引导：用导航图选目标而不是探索目录树；只关心文件的一部分时先 `search` 定位、读命中附近的范围；**一两段中等范围好过很多细小范围**；只有小文件或整份文件才是证据时才整读。最后那句是踩出来的——第一版写的是"read a narrow range around the hit"和"many questions need no readfile at all"，被模型**字面执行**成"每个命中点做一次微型读取"，同一个任务轮数从 3 涨到 6、总 token 翻倍。改成"prefer one or two medium ranges over many small ones"之后：同样 3 轮、同样 2 次 `readfile`，但读入字符 8140 → 4277、总 token 少 20%。
3. **仓库约定**：读取目标仓库根目录的 `AGENTS.md`（上限 32 KiB，超限截断并标记；符号链接和编码错误一律忽略）。

第 3 条属于**目标仓库**而不是本程序，所以 `--repo` 指向另一个仓库时会自动换成那个仓库自己的约定，不需要改代码；目标仓库没有该文件时静默跳过，只保留前两条。文件在每次运行开始时读取一次并进入静态前缀，因此运行中途修改 `AGENTS.md` 不会影响当前 run。

上下文由 `ConversationContext` 管理：session history 按顺序保存用户请求、assistant tool call 和 tool result；每轮发给模型的是经过预算裁剪的 transcript。旧的 `readfile` 结果会替换为文件摘要，旧的 `search`/`shell` 结果会保留受限元数据；需要细节时模型可使用 `readfile` 的 `start`/`end` 参数重新读取。

压缩分两级，原则是 **LLM 只用于提炼知识，不用于缩减字节**。

第一级按**单个结果**，**只在视图真的越线时才做**：结果进入历史时**原样保存**，工具自身的返回上限（`readfile` 64 KiB）已经约束了单结果大小；当组装 prompt 时发现视图超过触发线，才从**最大的结果开始**按需修剪，只修到装得下为止。**一轮不够就把目标减半再来一轮**（默认 6000 → 3000 → 1500 → 600），而不是立刻交给 LLM 写摘要——因为一轮修剪的下限是 `结果数量 × 目标`，结果一多（默认阈值 96000 ÷ 粒度 6000 ≥ 16 个）第一档无论如何都压不到阈值以下，于是整档本地压缩被跳过。实测：20 个"全部略大于粒度"的结果，收紧前会调用一次 LLM 摘要，收紧后视图 136360 → 94270、**0 次模型调用**。修剪规则：`readfile` 保留路径、行范围、file summary、line_index **以及首尾两段代码本身**（按整行切，并写出被省略的是第几行到第几行，再给一条可直接照抄的续读区间）；`shell` 保留 exit_code 与头尾两段预览（关键信息常在末尾）；`search` 保留命中行本身（路径 + 行号 + 行内容）；`listfiles`/`find_files` 保留真实条目数 + 路径列表（目录带尾斜杠）。分配顺序是**内容优先**：先算去掉内容字段后还剩多少配额，再把余量花在内容上——字符串按首尾截断，列表按条数截断，都是"尽量多留"而不是砍到一个写死的条数；file summary 和 line_index 这些元数据是 LLM 提炼出来的、丢了要重新提炼，所以只在余量实在挤不出来时才丢。每一档都从**原文**重算 stub，绝不在已压过的 stub 上再压（stub 的 content 是"文件头 + 文件尾"的拼接，不是连续行，再算 `omitted_lines` 会算错）。这一级**不调用模型**，理由是确定性：本地规则是纯函数，同一份历史、同一个预算跑一次就固定下来；而 prompt cache 按精确前缀匹配，LLM 概括每次措辞都不同，会让第一条 stub 之后的全部内容失效，也让同一份 session 无法复现出同一个 prompt。

**为什么把修剪从"摄入时"挪到"压力时"**（这是被参考实现纠正的一个设计错误）：原来在结果产生时就按 6000 字符截断，于是一个 574 行 / 22.7 KB 的文件在 **96 KB 的视图里明明放得下**，却被切成 4 段、模型要读 5 次。而 DSH 的 `read` 工具（`readLimit` 2000 行 / `readMaxBytes` 50 KiB）下这是**一次**读完——它的 pruner 明确写着"修剪只在压缩触发条件满足后运行，低于压力时不会修剪任何内容"。挪过去之后，实测同一个"读两个文件并逐条对比"的任务，`readfile` 从 8 次降到 **2 次**、轮数从 8 降到 **3**、时长 62s → 56s。原因是"上下文放不放得下"是**组装 prompt 时**才知道的事，在结果产生时无从判断。

这里踩过三个坑，都值得单独记：

1. **stub 里只留元数据、不留内容**。当时的写法是超预算的 `readfile` 结果压成"路径 + 行范围 + file summary"，一个字符的代码都不剩。结果是模型看不到任何内容，只能换个更小的区间把同一个文件再读一遍，而且越读越窄——真实 run 里的读法是 130 行 → 60 行 → 12 行，35 次 `readfile` 把 30 次调用的安全阀耗尽，任务在 `round_limit` 上失败。
2. **压缩把 `error_type` 也丢掉了**：模型只看到 `ok: false`，不知道是审批被拒还是路径不存在，于是原样重发同一条命令。现在失败原因由 `_preserve_error` 统一附加，优先级高于任何预览。
3. **"没有字符串内容可截"被当成了"内容装不下"**：`search` / `listfiles` 这类结果只有列表、没有长字符串，截断函数直接返回失败，调用方于是走了"丢弃全部内容"的兜底分支——stub 只剩一个 `match_count`。修法是把列表也当成内容型字段按条数裁剪，并且在没有内容可截时按当前长度决定成败。同一个 bug 还暴露了 `listfiles` 的键名写错（结果键是 `entries`，代码读的是 `files`），stub 里写着 `file_count: 0`——那不是丢信息，是给模型**错误**信息。

**代价要说清楚**：`session.history` 现在保存的是工具返回的原文（不再预先压成 stub），所以 session 文件会变大——换来的是同一个 session 继续跑时模型仍能拿到完整内容，而且第一级修剪不再做无用功。完整原文同时也写在每个 run 的 `report.json` 里（`tool_result` 事件）。

**预算由模型窗口推导，不是一个拍出来的数。** 这里踩过一个大坑：`transcript_budget_chars` 原来写死 120,000 字符（≈27K token），而那个数是从"读两个文件需要多少空间"倒推的——**全程没看模型能装多少**。实际用的模型 `deepseek-flash`（DeepSeek-V41-Flash）窗口是 **1,000,000 token**，于是只用掉了窗口的 2.7%，"读几个文件就触发压缩"。DSH 自己的触发线是窗口的 80%。

现在：

```
窗口 1,000,000 token × 3.5 字符/token × 0.20（prompt 占比）− 16,000（system+工具定义+记忆快照）
→ transcript 预算 684,000 字符（原来的 5.7 倍）
→ 触发线 547,200，压缩目标 171,000，地板 175,200，余量 372,000
```

占比取 0.20 是**保守**值（DSH 用 0.8）：留出输出空间，也避免长上下文里"中段信息被忽略"的退化。生产链会把 `Settings.context_window_tokens` 传入 `ConversationContext`；可以用环境变量 `CODING_AGENT_CONTEXT_WINDOW_TOKENS` 或 CLI 参数 `--context-window-tokens` 覆盖默认窗口。`--transcript-budget` 仍然可以手动覆盖（验证压缩行为时要用）。

`next_action_hint` 里的窗口也是踩出来的：最早写死"从缺口第一行起读 100 行"（`first + 99`）。在平均行长 33 字符的文件上刚好，换个行长 165 字符的文件就会立刻引发第二次截断；而只按 2/3 配额算窗口又会白扔 1/3 预算。现在窗口按**这份内容自己的平均行长**推：

```
窗口行数 = (本次内容分到的配额 - 省略标记) / 平均行长
```

同一份 575 行、平均 33 字符/行的文件，补齐 429 行的缺口：写死 100 行要 5 次续读（连整读 6 次），按行长算出 143 行只要 3 次（连整读 4 次），而且窗口范围内不会二次截断。**注意这个保证是"对着提示给的那一段"成立的**：把 143 行套用到另一段（平均行长 40.4）会超出 55 字符，那一次读取会被截断并产生一条新的提示——这是可接受的，因为新提示同样按它自己那一段算。还有一个粗糙处：无损读取不产生 stub，也就没有新提示，模型得自己选下一段区间。

这个阈值也不能设小。它决定的是**模型能不能在一次调用里看完一个源文件**：设成 2000 字符时，一个 574 行的文件要切成十几段才读得完，而每次 `readfile` 都占一轮、都要重发一遍不断变长的 transcript——既烧轮数又烧 token。6000 字符够装下一个完整函数或一段主流程，剩下的靠省略行号指路。两个阈值还必须联动：`compaction_threshold_chars` 是 `transcript_budget_chars` 的 0.8 倍，压缩目标和 handoff 上限必须共同低于触发线；测试用的极小预算如果无法满足这个不变量，应直接拒绝，而不是进入压缩抖动。

**代价要明确**：`session.history` 里保存的是压缩后的 stub，完整工具输出保存在当次 run 的 `report.json` 里（`tool_result` 事件在压缩之前就写出了完整结果）。

第二级按**段落**（这一级用 LLM）：transcript 超过阈值时把最旧的一批消息换成一个交接摘要，摘要必须比它替代的内容更小才会被采纳；失败会发 `span_failed` 事件，并在视图再增长 50% 后重试——一次调用抖动不该关闭整个 run 的压缩。两级压缩都会发出 `context_compressed` 事件（含 `kind`、`before_chars`、`after_chars`），因为压缩是 agent 里最贵的操作，必须可观测。被裁剪丢弃的是最旧的 group；当前用户请求不属于历史组，一定会出现在 prompt 末尾。

`readfile` 有**两条**返回上限，谁先到算谁：

- **2000 行**（`max_lines`，对齐 DSH 的 `readLimit`）——让"一次调用能拿多少"变得可预测，footer 给出的续读起点才稳定
- **64 KiB**（`max_bytes`，作用于**返回内容**而不是源文件）——防止超长行把窗口吃光

读取 200 KB 的文件并指定 `start`/`end` 可以正常工作。每个结果都带一条 **footer**，由工具自己说清下一步读哪里——三种形态对应三种状态：

```
(Showing lines 100-160 of 574. Use start=161 to read the next range.)     还有后续
(End of file - total 574 lines.)                                          已到文件尾
(Output capped: showing lines 1-2000 of 3000. Use start=2001 to continue.) 被配额截断
```

**为什么 footer 必须由工具返回**：续读指令原本只在压缩层产生（`next_action_hint`），而压缩层只在**视图越线**时才修剪——一个 64 KiB 的截断结果远小于触发线（547,200），压根不会被修剪，那时模型只拿到一个裸的 `end`，得自己推 `start = end + 1`。工具的职责不该外包给压缩层。另外 footer 还补上了**文件尾信号**（否则模型会再发一次空区间确认到底）和**文件总行数**。

单行超过 2000 字符时**截断这一行**并标记（`... (line truncated to 2000 chars, 98009 omitted)`），而不是把整个读取判失败：压缩过的 JS 和单行 JSON 就是一行几万字符，拒绝读取等于模型完全看不到这个文件。

**源文件总量上限（`max_file_bytes`，64 MiB）只挡"整读"**：带了 `start`/`end` 就放行，因为 `_read_lines` 是流式的、范围外的行不保留，内存上没有风险。不分情况一律拒绝的话，一个 100 MB 的日志文件连第 1-5 行都读不出来。整读超限时的消息会指出可用写法（`File is 3 MiB, too large to read whole; pass start/end to read a specific range, or use search to locate text`）。

不能为了做摘要而整读的文件不会被误记为"已完整读过"。

每次 CLI 调用默认创建新 session。需要跨 run 延续任务时，使用输出中的 session ID：

```powershell
coding-agent "先检查 README" --repo F:\AgentLabs\httpstat
coding-agent "继续检查测试" --repo F:\AgentLabs\httpstat --session <session-id>
```

session 状态保存在目标仓库的 `.coding-agent/sessions/<session-id>.json`，其中包含完整 history、working memory、file summaries 和 episodic notes。快照由三段组成：`[Memory]` 是每轮无条件发送的状态板（任务、约束、最近读过的文件、最近改动的文件、最新工具错误，各字段的更新时机见下）；`[Relevant Memory]` 按关键词最多召回 3 条历史 note；`[Relevant File Summaries]` 按相关度最多召回 5 条文件摘要（用 query 关键词对 `路径 + summary + symbols` 打分，改过的文件 +3、刚读过的 +1）。三段都在 prompt 尾部且很少变化，所以缓存前缀保持稳定。完整工具结果仍保留在每个 run 的 `report.json` 中。

session 还保存一个有界的 `checkpoints` 集合。工具批次开始时写入 `active`，每个工具在执行前标为 `running`，结果落入 history 后标为 `completed` 或 `completed_error`；批次结束后只保留少量 `recent` 元数据。若进程在工具执行窗口崩溃，下一次使用同一 `--session` 会比较工作区快照和 `runtime_identity`，将可确认的写入标为已恢复，无法确认的调用以 `execution_interrupted` 结果交给模型判断，绝不会自动重跑 `write_file`、`patch_file` 或 `shell`。`resume_state` 保存这次检查结论；trace/report 仍是完整审计来源。

`[Memory]` 里两个容易混淆的字段：`recent_read_files` 只在 `readfile` 成功时更新（`listfiles`/`find_files` 带回来的目录不算，否则这一栏会变成 `a.py, kb, tests, .`），`recent_modified_files` 只在 `write_file`/`patch_file` 成功后更新。两个都要渲染：历史里的 `readfile` 结果会被段落压缩吃掉，`[Memory]` 不会，所以它是压缩之后模型唯一还能知道"这个文件我已经读过"的地方。

每次 CLI 提问都会创建一个独立的 run。运行结束后，CLI 会在标准错误中显示 run ID 和 trace 路径：

```text
.coding-agent/runs/<run-id>/trace.json
```

trace 会记录模型请求、工具调用、审批结果、工具结果、错误和最终回答，不会保存 API Key。

每次运行都会在同一个 run 目录生成两个文件：`trace.json` 保存简洁摘要，`report.json` 保存每轮完整 LLM 消息、工具定义、规范化模型响应和工具结果。report 仍会进行字段脱敏；如果读取的文件包含密钥等敏感文本，这些内容可能出现在报告中，请谨慎保存。

`llm_retry` 事件记录 API 重试的 `attempt`、稳定错误类型和耗时。`llm_response` 记录 `finish_reason`、`finish_reason_source`（`provider` 或兼容推断的 `fallback`）以及 `api_attempts`。没有工具调用却缺少 `finish_reason` 会被判为 `invalid_response`，进入有限重试；`length` 和 `content_filter` 不重试。

可以使用链路检查脚本查看一次 run 的关键过程：

```powershell
uv run python scripts/inspect_run.py
uv run python scripts/inspect_run.py .coding-agent/runs/<run-id>
uv run python scripts/inspect_run.py --prompts
```

脚本默认选择最新的完整 run。不带参数时只输出事件顺序、工具、审批、成功/失败和耗时，不展开完整 prompt；加 `--prompts` 则逐轮展开每轮完整的 `messages`、模型响应和工具调用，用于审计工具选择与 prompt 结构。每个 `llm_request` 还带 `prompt_chars`、`stable_prefix_chars`、`history_covered`、`dropped_groups` 四个指标，可用来观察上下文长度和压缩行为随轮次的变化。字段含义、指标读法和审计清单见 `docs/run-report-reader.md`。

## 开发约定

- 先明确目标和边界，再开始实现
- 为重要行为补充自动化验证
- 保持提交范围小而清晰
- 在完成前运行与变更相匹配的检查

## 后续计划

1. 增加本地 HTTP 服务和 IDE 客户端
