# anima-blackbox · 智能体黑匣子（双账言行对照参考实现）

**一句话**：把「主体声称要做什么」和「主体实际做了什么」物理分成两本不可改写的账，用一个可外部重放的对照器暴露二者差额——不是更好的日志，是**让「言行不一」变成机器可判定的事**。

纯 Python 标准库，零依赖，零生产改动，sandbox 参考实现。MIT 许可。

[English](README_EN.md)

> 状态：**参考实现（reference implementation）**。不是生产级系统，不接真实执行链路，不含系统账号隔离。边界见文末。

---

## 三十秒跑完（不需要 pip / 联网）

```bash
git clone https://github.com/deanhan2026-lang/anima-blackbox.git && cd anima-blackbox
python3 -X utf8 selftest_b0.py     # 24/24 PASS
python3 -X utf8 canon_vectors.py   # canonical 对齐向量 ALL PASS
python3 -X utf8 chain.py           # 混合口径分段校验 5/5 PASS
```

**你应该看到**：`黑匣子自测：24/24 PASS` / `canonical vectors self-test: ALL PASS` / `chain segmented self-test: 5/5 PASS`。

再校验发布包完整性：`sha256sum -c MANIFEST.sha256`（macOS: `shasum -a 256 -c`）。

---

## 为什么是两本账，不是一本

单本日志再怎么防篡改，都只能证明「记录没被改」，证明不了「主体说到做到」——因为声称和行为写在同一处，攻破一处就能重新对齐。

本实现把两者分开：

| 账 | 谁写 | 记什么 | 独立性来源 |
|---|---|---|---|
| **A 账 · 声明账** (`claim_log.jsonl`) | 被审计主体 | plan / intent / self_report | 弱：靠「写完不可改」（append-only + 链 + 外部锚定），不保证「写下时为真」 |
| **B 账 · 执行账** (`execution_log.jsonl`) | 独立伴生进程（sidecar） | action / consequence | 强：主体无写权限，只能发消息 |

**言行差额**才是真正的信号：说了没做、做了没说、参数越界、后果不符——这些在单账系统里看不见。

---

## 架构

```
主体 ──写──> A账(声明)  ─┐
                          ├─> 对照器(reconcile) ──> 判定单元 ──> 外部可独立重放
sidecar ─写─> B账(执行)  ─┘        ↑只读，无 append 权限
Watchdog ── 检测 sidecar 掉线 / 日志段缺失
```

关键设计取舍：

- **对照器是下游消费者，不是黑匣子的组件**。黑匣子只产两账 + 事件流，不产裁决；对照器消费事件流输出判定单元。好处：任何人都能用同一规则自己写对照器做交叉验证，而不是「重放我们写的对照器」。
- **对照器输出可复算判定单元，不是自然语言报告**（与 DVEF-001 判定格式同构）：`inputs` / `params` / `output` 三段各带摘要，`params` 封闭枚举无时间戳随机数。外部拿单元即可重放摘要与三态合法性。
- **三态 verdict**：`MATCH`（言行一致）/ `MISMATCH`（检出差额，score=差额条数）/ `INCONCLUSIVE`（证据不足：缺账 / 链破损 / scope 不可核）——**空账不许伪装成「无差额」**。
- **差额四级标注**：`MISMATCH_SET` / `MISMATCH_SCOPE` / `MISMATCH_CONSEQUENCE_STRUCTURED` 强可复算进判定位；`MISMATCH_CONSEQUENCE_NARRATIVE`（纯自然语言后果）弱可复算，只能进 `INCONCLUSIVE`。`evidence_strength` 进 `inputs_digest`，防止「强弱证据达成同一结论」被误认同等可信。
- **混合口径分段校验**：历史账用 JSON 插入序（JS `JSON.stringify` 等价），新账用字典序，`legacy_till` 由调用方显式给、不猜测。附阴性对照：用错口径校验旧账必然失败——「只会通过」的校验器不算校验。

---

## 文件

| 文件 | 职责 |
|---|---|
| `chain.py` | append-only 哈希链基元（seq / prev_hash / entry_hash）+ 混合口径分段校验 |
| `ledgers.py` | 两账写入，writer_token 权限隔离，字段封闭枚举 |
| `reconciler.py` | 言行差额对照 → 可复算判定单元（seal / replay） |
| `watchdog.py` | sidecar 掉线检测，标记日志段缺失 |
| `canon_vectors.py` / `.json` | 跨实现 canonical 口径对齐向量（键序 / 非ASCII / 转义 / 数字四类坑） |
| `selftest_b0.py` | 全套自测，逐条验收下方判据 |
| `MANIFEST.sha256` | 发布包完整性清单 |

---

## 验收判据（自测逐条覆盖）

| 判据 | 自测覆盖 | 结果 |
|---|---|---|
| ① 三类言行差额检出 | SAID_NOT_DONE / DONE_NOT_SAID / PARAM_OUT_OF_SCOPE + MATCH + 空账 INCONCLUSIVE | PASS |
| ② sidecar kill → Watchdog 报警标缺失 | 心跳声明 seq=3 账本只到1 → `E-SEGMENT-MISSING, missing_from=2`；主体不能伪造心跳 | PASS |
| ③ 事件流机械转 inputs/params | params 无时间戳 / 随机数 | PASS |
| ④ 进程级权限分离 | 主体可写 A 账、不可写 B 账；对照器两账无写权限；跨账 5/5 被拒 | PASS |
| ⑤ 差额即判定单元、反向样本检出 | seal 三态 + 摘要；replay 一致；伪造差额=0 → `E-OUTPUT-DIGEST` | PASS |
| 弱证据不进强判定 | 仅 narrative 后果差异 → `INCONCLUSIVE`；`evidence_strength` 进 inputs_digest；narrative 不进落盘摘要 | PASS |
| 链完整性 / 篡改 / scope 只引用 / 字段枚举 / 锚定摘要 | 篡改 entry_hash 重算不符；伪造 scope 被拒；链头摘要可锚 | PASS |

**自测抓出并修掉的两个真问题**（诚实留痕）：①测试脚本把 `scope` 传错函数（测试代码错，非产品）；②**真接口不一致**——`authority_level` 藏在 content 里导致越界漏报，已提升为结构化顶层字段并纳入链摘要。教训：授权级别必须是可判别的顶层字段，不能只藏在内容里。

---

## 边界（不许省）

1. **参考实现，不是生产系统**：不接真实 Agent 执行链路，事件由自测构造。
2. **进程级隔离是模拟**：用 `writer_token` 模拟权限分离；真实系统账号 / 文件权限隔离不在本实现范围。
3. **A 账独立性天然弱于 B 账**：A 账由主体自述，「写完不可改」不等于「写下时为真」。本实现测得出「自己骗自己」，但前提是声明最终落到不可改的账上。
4. **有限深度**：`replay()` 标注 `CL1-digest-consistency`——摘要级一致性可外部验证；完整语义重放需外部持有同版 `scope_registry`。不假称解决「谁来验验证器」的递归。
5. **锚定是接口不是实现**：链头摘要可供外部锚定，但本实现不含真实外部锚定发件。
6. **不可篡改 ≠ 真相**：哈希链只保证内部一致性，防不住「两端合谋重写 + 整链重建」。外部锚定与独立复算节点才是外部有效性的来源，本实现不提供这两者。

---

## 许可

MIT © 2026 ANIMASTELLAR TECHNOLOGY (SHENZHEN) CO., LTD. 见 [LICENSE](LICENSE)。

关联可复算格式：[DVEF-001](https://github.com/deanhan2026-lang/dvef-001)（判定依据格式，同体系）。
