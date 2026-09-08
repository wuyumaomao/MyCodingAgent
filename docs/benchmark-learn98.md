# Benchmark 评测框架学习笔记

## 一、核心思想

不要用“我觉得 Agent 能工作”作为结论，而要把一次 Agent 运行变成一条可验证、可复盘、可聚合的证据链：

```text
固定 Benchmark 任务
    -> BenchmarkEvaluator 执行
    -> Verifier 判定单任务
    -> 保存运行 Artifact
    -> Metrics 聚合和对照实验
```

核心方法可以概括为：

1. 先固定任务和环境。
2. 再定义客观判定条件。
3. 先用固定输出验证 Harness 本身。
4. 再用真实模型评估真实效果。
5. 每种机制使用适合自己的指标。

## 二、任务不是一句话，而是一份合同

Benchmark 任务不能只写“帮我修改 README”，还需要定义完整约束：

```json
{
  "id": "readme_intro_locked",
  "prompt": "...",
  "fixture_repo": "tests/fixtures/readme_repo",
  "allowed_tools": ["read_file", "patch_file"],
  "step_budget": 4,
  "expected_artifact": "...",
  "verifier": "...",
  "category": "documentation"
}
```

这份合同规定了：

- Agent 要做什么；
- 在哪个固定仓库副本中做；
- 允许使用哪些工具；
- 最多执行多少步；
- 最终应该产生什么结果；
- 用什么规则验证结果；
- 这是什么类型的任务。

## 三、Fixture 隔离与可复现

每个任务运行前，都应该把固定 Fixture 仓库复制到新的临时目录：

```text
固定 Fixture
    -> 本次任务副本
    -> Agent 在副本中执行
```

这样上一轮任务修改过的文件不会影响下一轮。

可以为 Fixture 记录快照标识（例如内容哈希），从而知道一次结果对应的是：

- 哪个任务定义；
- 哪份 Fixture；
- 哪一版 Agent 代码。

评测运行不应修改原始 Fixture 或主仓库。

## 四、先用 FakeModel 验证 Harness

默认先使用 `FakeModelClient` 或 Fake LLM，返回固定的模型输出序列。

这样可以把两个问题拆开：

```text
Harness 是否正确？
模型本身是否稳定？
```

如果一开始就使用真实模型，失败时很难判断原因究竟是：

- Runtime 有 Bug；
- 工具执行有 Bug；
- Verifier 写错；
- 任务设计不稳定；
- 模型随机输出失败。

固定模型输出可以先稳定验证：

```text
AgentLoop
    -> 工具调用
    -> 文件修改
    -> Verifier
    -> Artifact 写入
```

之后再接入真实模型，评估真实效果。真实模型结果不能替代确定性回归测试。

## 五、单条任务的通过条件

不能只看 Agent 是否回答了“Done”。任务应由多个条件共同判定：

```python
within_budget = tool_steps <= step_budget
verifier_passed = verifier.returncode == 0
non_failure_stop_reason = stop_reason == "final_answer_returned"

passed = (
    within_budget
    and verifier_passed
    and expected_artifact_exists
    and non_failure_stop_reason
)
```

也就是说，任务必须同时满足：

- 没有超过工具步数；
- 验证程序通过；
- 期望产物存在且正确；
- Agent 正常结束。

例如 Agent 回复“Done”，但文件内容没有修改正确，Verifier 失败，最终仍然算失败。

## 六、失败必须分类

不能只记录一个笼统的 `fail`，而要保存具体失败原因，例如：

```text
missing_artifact
budget_exceeded
verifier_failed
failure_stop_reason
```

这样才能回答：

> 这个版本失败主要来自工具调用超预算，还是 Verifier 错误，还是目标文件没有生成？

失败分类对调试、回归和面试都很重要。

## 七、Artifact 要保存运行上下文

一次 Benchmark 结果不应只有：

```json
{
  "pass_rate": 0.8
}
```

还应该记录：

- Agent 代码的 commit 和 branch；
- Benchmark 来源；
- Fixture snapshot ID；
- 模型名称和版本；
- decoding 参数；
- timezone 和 locale；
- 每条任务的完整结果；
- 失败类别；
- 工具步数和 attempts；
- 运行过程产物（trace、report 等）。

否则不同环境下得到的同一个 `pass_rate` 无法直接比较。

## 八、机制要使用匹配的指标

不要所有模块都只看 `pass_rate`：

| 被评估机制 | 更合适的指标 |
|---|---|
| 上下文治理 | prompt 长度、压缩率、预算裁剪情况 |
| 记忆机制 | 重复读取次数、后续工具步数、任务正确率 |
| 安全治理 | 拦截次数、错误码分布、路径逃逸阻断率 |
| Provider 对照 | pass rate、平均 attempts、平均 tool steps |
| 执行效率 | 工具步数、运行时长、重试次数 |
| 恢复机制 | resume 成功率、错误恢复率、误接受率 |

例如要证明上下文压缩有效，不能只说“开启压缩后 pass rate 是 85%”，还应该比较：

```text
平均 prompt：7000 字符 -> 5400 字符
任务正确率：压缩前后是否下降
```

## 九、在本项目中的最小实践

当前 Coding Agent 可以先设计一个最小只读 Benchmark：

```json
{
  "id": "read_readme_intro",
  "prompt": "读取 README.md，回答项目用途",
  "fixture_repo": "tests/fixtures/readme_repo",
  "allowed_tools": ["readfile"],
  "step_budget": 2,
  "expected_artifact": "final answer contains project purpose",
  "verifier": "检查最终答案是否包含固定关键词",
  "category": "read_only_qa"
}
```

实现顺序：

```text
1. 准备固定 README Fixture
2. 写一条任务合同
3. 写 Verifier
4. 用 FakeModel 固定调用 readfile
5. 验证 AgentLoop、工具链和报告
6. 再换真实模型
7. 比较 Fake baseline 与真实模型结果
```

一条合格的最小 Benchmark 应满足：

```text
可重复运行
可自动判定
失败可解释
结果可复盘
```
