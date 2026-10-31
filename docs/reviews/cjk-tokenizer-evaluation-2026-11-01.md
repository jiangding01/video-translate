# CJK 分词器评估：能不能一个依赖解掉两个卡点（Q7b 对照路线）

- 日期：2026-11-01；基线 `main` @ `5324549`，工作树干净
- 方法：3 个 subagent 并行（代码上下文精读 / 上游库联网核实 / 临时 venv 真实语料实测），统筹人对关键事实逐条复核
- 标注约定：**[实测-本人]** 我亲自跑了命令或读了页面；**[实测-sub]** subagent 实跑（原始输出随当时会话的临时目录失效，两个核心现象的复现命令见 §3 末）；**[代码]** subagent 精读源码并给出行号，关键行号我抽查过；**[推断]** 没有直接证据的判断
- 本轮零代码改动、零提交；项目 `.venv` / `uv.lock` / `output/` 未碰
- 入库复核（2026-11-01，Opus 5 主会话）：budoux / jieba 的 PyPI 元数据与发布节奏（0.10.0/0.10.1/0.10.2 = 10-19/10-26/10-29、`py3-none-any`、运行时零依赖、336KB vs 41MB）、budoux 中文错断、jieba `posseg` 标签乱象、「零日文目标语言 Job」、`segment.target_wrap_binding()` 的存在与 `burnin.py` 复用判定位置，均由主会话独立重跑或查证坐实（其中 budoux 中文错断实测比原文更差：`从容应对` → `从|容应|对`）。未亲核项见 §7

---

## 0. 结论摘要

**分词器不能同时解掉两个卡点，它只能解一个半里的"一"：换行点。** 换行只需要词边界，121KB 的纯 Python 库 budoux 就够；专名候选需要命名实体信号，词边界给不了——中文唯一轻量选项 jieba 的词性标签在真实语料上实测信噪比约一半、词典外专名直接切碎、且 2020 年起停止维护；日文能给可靠专名标签的 Sudachi/fugashi 要背 44–250MB 词典。

**而且两个卡点今天都没有真实语料压力**：仓库零份日文译文 artifact（segment.py:240-241 注释自证），`output/` 下全部历史 Job 都是英文源→中文（实测），CJK 源的专名抽取降级路径从未被真实任务触达。

**推荐：现在不引入任何分词器依赖，两个卡点都挂上明确的触发条件。** 触发后的最小落地也已想清楚（§6）：换行腿用 budoux 只做日文、optional extra、缺依赖退回现状；专名腿届时优先 Q7b 模型推断而不是分词器，因为 n=2 实测已经证明缺口是"精度与语义"，分词器补的是另一个方向。

---

## 1. 两个卡点对"分词器"的要求不一样（问题 1）

### 1.1 译文换行（卡点 2）：只要"这里可以断"的位置集合

**[代码]** `segment.wrap_target_lines`（segment.py:278-311）的机制：token 化 → 伪 Word 喂宽度 DP（`_partition_words`，:173-203）→ 每个边界复核 `forbidden_break`（:128-142，全仓唯一禁则谓词）→ 违禁或无解返回 `None` 维持 review。token 是不可再拆的原子，**DP 和禁则都不关心 token 内部**。所以分词器的插入形态极其干净：把分词结果当作 `_CJK_WRAP_TOKEN.findall`（:243-247）的替代 tokenizer，与已落地的 `_KATAKANA_RUN`（片假名连续段整体成 token）是同形态扩展。换行是纯展示层：`wrap_target_lines` 输入输出都是 str，不接触 Cue 对象，grep 证实 qa/render/segment 三处无任何 `target_text` 赋值——**约束 1（时间轴）天然不被触碰**。

这个用途对分词器的要求：**只要边界，不要标签**。甚至不要求"词"是语言学意义上的词——日文排版的惯例单位是文节（助词粘前词），比形态素粗。

### 1.2 CJK 专名候选（卡点 1）：要的是命名实体性质，不是边界

**[代码]** `translation_context.py` 的主判据（规则 D，:372-397）本质是**用大小写形态当免费的 NER 信号**："只出现大写形态且至少一次非句首"筛出来的就是专名。CJK 没有这个信号，所以走 `no_case_signal` 降级（:494-520），降级后形态类规则（`MX-5` 这类含数字/连字符的 ASCII 串）照跑、kind 退为 `shaped_term`（:638-663）——**CJK 源今天也不是零产出，ASCII 型号名仍能抓到**。

给 CJK 补这条腿，分词器需要给出的不是"词在哪断"，而是"**这个词是不是专名**"。词边界本身对专名抽取只有间接价值（把候选切出来），判定还得靠词性/NER 标签。两个卡点共享的只有"CJK 没有空格"这个表象，**底下的需求一个是边界集合、一个是实体标签，是两种能力**。

### 1.3 实测验证：标签能力才是分水岭

**[实测-本人]**（复核 subagent 结果，亲自重跑）：

- jieba `posseg` 在真实中文译文上：`别以为` 标成 nr（人名）、`舒适性` 标成 nr、`马自达` 标成 `nrfg`（不在常用 nr/ns/nt/nz 过滤集里，按标签过滤直接漏）、词典外的 `米亚塔` 被切成 `米亚/ns | 塔/j`。**[实测-sub]** 全语料（139 行）按 nr/ns/nt/nz/eng 过滤得 10 个词，真专名/术语约 7 个但含 3 个切碎残片（`MX`、`What`、`Car`），噪声 2 个。信噪比约一半，且**类别标签不可信**（品牌标成人名、地名标成人名）。
- budoux：**只输出断块，没有任何词性/专名信号**（上游 README 自述，[实测-sub] 验证 API 输出）。对专名卡点零贡献。
- 日文侧 **[实测-sub]**：fugashi（unidic-lite）能给 `名詞/固有名詞`，Sudachi 标签更细（`固有名詞/地名` vs `人名` 可区分，比 jieba 可靠得多），但 `MX-5` 被拆成 `MX|-|5`、拿不到固有名詞标签——型号类还得靠既有 ASCII 形态规则兜。

**结论（问题 1 的直接回答）**：分词器**能解换行卡点（且只需要最轻的那种）**；**解不好专名卡点**——中文侧唯一轻量选项的标签质量实测不过关，日文侧过关的选项要 44–250MB 词典，而且两侧都只覆盖"词典内专名"，对真正缺的"语义性术语"（见 §5.1）零贡献。"一个依赖解两个卡点"不成立：换行要 budoux（无标签），专名要 jieba/Sudachi（标签），**是两个不同的依赖，且后者还按语言再分叉**。

---

## 2. 候选库逐个代价（问题 2）

| 库 | 最新版/日期 | 形态 | macOS arm64 + Linux x86_64 wheel | 安装体积（含词典） | 许可证 | 额外下载 | 维护 | 能产出什么 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **budoux** | 0.10.2 / 2026-10-29 | 纯 Python，零依赖，`requires-python >=3.11` | `py3-none-any`（121KB），平台无关 | 336KB（模型 JSON 内置：ja 36KB、zh-hans 59KB、zh-hant 59KB、th 31KB） | Apache-2.0 | 无 | 活跃（10 月三连发） | **只有断行块**，无词性无专名 |
| **jieba** | 0.42.1 / **2020-01-20** | 纯 Python | **只有 sdist**（19.2MB，纯 Python 现场构建、无需编译器，[实测-sub] 装通） | 41MB | MIT | 无（词典内置；首次运行建缓存 ~0.2s/进程） | **停止维护**（最后提交 2020-02，648 open issues） | 分词 + posseg 词性（nr/ns/nt/nz），标签质量见 §1.3 |
| **mecab-python3** + unidic-lite | 1.0.12 / 2025-11 + 1.0.8 / 2021 | C 扩展（wheel 内含 MeCab） | 库有 cp311–cp314 双平台 wheel；**unidic-lite 只有 sdist**（47.4MB，纯 Python 构建） | 库 <1MB + 词典装后 **248MB**（[实测-sub]） | BSD（UniDic 2.1.2 为 2013 年数据） | 无 | 库活跃，词典 2021 年后不更新 | 形态素 + UniDic 四层词性（含固有名詞/人名/地名） |
| **fugashi** + unidic-lite | 1.5.2 / 2025-10 | Cython 扩展（内含 MeCab） | cp311–cp314 双平台 wheel（arm64 实测装通，manylinux 覆盖为 [实测-sub] 引 PyPI JSON，本人未逐一核） | 同上，合计约 250MB | MIT+BSD | 无 | 活跃 | 同上 |
| **SudachiPy** + sudachidict | 0.7.0 / 2026-09 | Rust 扩展 | **cp310-abi3** wheel 全覆盖（macosx_11_0_arm64 / manylinux_2_28_x86_64 / aarch64 / win，**[实测-本人]** 查 0.7.0 JSON 逐文件核实），覆盖 cp311–cp313 | 库 4.2MB + core 词典 193MB（或 small 42.5MB） | 库与词典均 Apache-2.0 | 无（词典是 pip 包） | 活跃 | A/B/C 三档切分 + 细分词性（地名/人名可区分，[实测-sub] 质量最好） |
| **pkuseg**（原版） | 0.0.25 / 2020 | 编译扩展 | **最高 cp38**，3.11+ 不可用 | — | MIT | — | 死 | —（出局） |
| **spacy-pkuseg** | 1.0.2 / 2026-10-26 | 编译扩展 | cp311–cp315 双平台 wheel（[实测-sub] 引 PyPI JSON；1.0.2 文件列表我 fetch 时被截断未亲核，旧版确有同款覆盖） | wheel 2.5–4.3MB + **拉入 numpy**；注意 PyPI 上 `pip install pkuseg` 0.0.12 其实是纯 Python 转发版、会装 numpy、init 1.59s 且往 stdout 打日志（[实测-sub]） | MIT | **词性模型运行时从 GitHub release 下载**（与本项目"本地优先、零运行时下载"冲突） | 活跃（explosion 维护） | 分词 + 词性（需下载 postag 模型） |
| LTP / HanLP / spaCy 管线 | — | — | — | 数百 MB 起 + PyTorch | — | 运行时下载模型 | — | 出局：spaCy 的 zh/ja 底层就是 spacy-pkuseg 和 SudachiPy，不多给东西（[实测-sub] 联网调研，常识级结论） |
| **ICU cjdict**（Python 侧 PyICU / Node `Intl.Segmenter` / Rust `icu_segmenter`，同一份 CLDR 词典） | 随系统 ICU | PyICU 需编译并链接系统 libicu（非纯 Python） | 取决于系统 ICU | 库小，词典随系统 | ICU 许可（MIT 系） | 无 | 活跃（ICU 本体） | 词边界，**无词性/专名标签** |

**纯算法方案**：
- `re` / `regex` 的 `\b`：**[实测-sub ×2 独立验证]** CJK 字符全算 `\w`，连续 CJK 串内部**零边界**；`regex` 的 `(?w)`（UAX#29）在 CJK 字符间**每个位置**都给边界（等于逐字，无词概念）。唯一有点用的是 `re.ASCII` 下 `\b` 能切出嵌在 CJK 里的 ASCII 段——这个能力现有 `_CJK_WRAP_TOKEN` 的 Latin 段规则已经覆盖了。**正则不是选项。**
- 自带词典做最大匹配：词典本身就是上面那些库的主要体积（几十 MB 起），自己背词典等于自己当上游，还要自己处理 OOV。**[推断]** 比引库更贵，出局。

**"日语和中文大概率需要不同的库"被实测坐实**：换行腿 budoux 一个包覆盖 ja+zh（这是唯一例外）；专名腿则 zh 只有 jieba（停维护）/spacy-pkuseg（numpy+运行时下载），ja 只有 MeCab 系/Sudachi 系，没有共用方案，朝鲜语两头都缺（ko 换行已按空格分词，无需求）。

---

## 3. 真实语料实测要点（复现命令见本节末）

语料：`output/mazda-mx5-portrait-20261023/chinese-only/chinese.srt`（48 行 475 字符）+ 同内容竖屏折行版（91 行）。**语料很小，以下是现象不是统计结论。**

1. **jieba 中文词边界基本合理**（马自达/性价比/减速带都是整词），但 ASCII 段切碎：`MX|-|5`、`What| |Car|?`、`1.5|升`。[实测-sub]，样例本人复核
2. **budoux 中文模型错断显著**：`Wh|at`（英文单词从中间断开）、`性价|比`、`从|容驶过`、`2026年|度汽车`、`第七|年`。**[实测-本人复现]**。budoux 的 zh 模型质量明显差于 ja 模型
3. **budoux 日文是文节粒度，直接可用作换行点**：`グラフィックスの|性能が|大幅に|向上しました`，专名不切碎；fugashi/Sudachi 是形态素粒度（`向上|し|まし|た`），**对换行太碎**，要自己把助词助动词往前合并——即"能给专名标签的库，拿来做换行还要再加一层文节合并逻辑"。[实测-sub]
4. **性能**：budoux import+加载 <5ms、全语料 0.78ms；jieba 每个新进程 ~0.2s 初始化（缓存不省时间）+ stderr 日志；pkuseg init 1.59s + stdout 日志。CLI 场景 budoux 几乎零成本。[实测-sub]
5. **本机安装全部成功**（macOS arm64，含 sdist 现场构建的 jieba / unidic-lite，均纯 Python 无编译器需求）。[实测-sub]

**复现两个核心现象**（临时 venv，**不要装进项目环境**）：

```bash
python3 -m venv /tmp/cjk-eval && /tmp/cjk-eval/bin/pip install -q budoux jieba
# budoux 中文模型错断（含把英文单词从中间断开）
/tmp/cjk-eval/bin/python -c "import budoux; p=budoux.load_default_simplified_chinese_parser(); print([' | '.join(p.parse(s)) for s in ['Which Claude model should you use','这款车的性价比很高','从容应对各种路况']])"
# jieba posseg 把常用词标成人名、把词典外专名切碎
/tmp/cjk-eval/bin/python -c "import jieba.posseg as pseg, jieba; jieba.setLogLevel(60); print([f'{w}/{t}' for w,t in pseg.cut('别以为马自达是靠偷工减料做到的，舒适性和米亚塔')])"
```

---

## 4. 对照路线：Q7b 模型推断腿的现状

**[实测-本人]** HANDOFF 待办第 4 条与 ROADMAP 第 4 条（2026-11-01 改定）：n=2 真实英语素材上规则 D **召回两份都满**（6/6、6/6），精度 6/8 且唯一误收是 ASR 噪声；漏的全是**语义性**项——`Fable 5.1`（版本号不以字母开头，token 正则够不着）与 `medium`/`low` 档位名（普通词构成的产品术语，确定性判据必然排除）。**"该补的是精度与语义而不是召回"已被实测钉住。** Q7b 模型腿接入成本：`run_provider_batches` + fingerprint checkpoint + 新等待步骤登记 `AWAIT_STEPS` / `cli._STEP_ROUTES`（AST 守卫强制），不进 prompt 不进指纹（协议零变更，Q7c 才变）。

这对本评估的含义：**分词器与模型腿不是同一道题的两个解**。分词器给的是"CJK 的词边界覆盖"（召回侧，而召回恰恰不是实测缺口）；模型腿给的是"语义性术语的识别"（恰是实测缺口），且天然同时覆盖 CJK（模型不需要大小写信号）。如果有一天 CJK 专名需求被真实语料证实，**模型腿比分词器更对口**——jieba 的标签质量（§1.3）做不出"高置信候选"，产出还是要人工审核，和模型腿一样，但模型腿不引入按语言分叉的依赖矩阵。

---

## 5. "不做"的代价（问题 3——这是正经选项）

### 5.1 卡点 1（CJK 专名抽取）不做的后果

- CJK 源的 `build-translation-context` 继续降级：无 `proper_name`/`capitalized_run` 候选，但 `shaped_term`（`MX-5`、ASCII 缩写、代码标识符）照常产出，`extractor_degraded` 如实记录，退出码 0。**降级是诚实且部分有产出的，不是全损。** [代码]
- 影响面：**今天为零**。`output/` 下全部历史 Job 均为英文源（[实测-sub] 遍历 SRT 核实），CJK 源降级路径从未被真实任务走到。HANDOFF 的 n=2 实测也全是英文素材。
- 严重度：**潜在而非现实**。真实后果出现的前提是"用户开始跑日文/中文源视频且需要 glossary 候选"——届时再决策，损失的只是这段时间里一个没人用的功能。

### 5.2 卡点 2（译文换行词法边界）不做的后果

- 日文译文超宽时，平假名↔汉字边界仍按字切，可能产出 `科学|的`、`ど|んなに` 这类坏切点（REVIEW_FINDINGS P7 ② 的同类问题），或片假名硬形态触发 review。
- 影响面：**今天同样为零**——仓库**零份日文译文 artifact**（segment.py:240-241 注释，[代码]）。P7 的 12 个坏切点实测数据来自**源文 ASR 分段**（P7 ⑤ 明确标注），不是译文换行，且源文侧已拍板 0/12 不做。
- 中文译文：按字换行在中文排版惯例下可接受（标点禁则已由 `forbidden_break` 挡住；字间任意断行是中文行业常规，**[推断]**），而且 budoux zh 模型实测错断比按字切更危险（§3.2）——**中文换行引分词器反而可能是负收益**：它把"处处可断"收窄成"模型认为可断"，模型错了就是无解→review 变多。**但这个负收益是「硬禁则」形态特有的，不是引分词器的必然**——补记见 §8。
- 严重度：**纯理论**。真实后果出现的前提是"出现日文目标语言的真实 Job 且换行 review 频发"。

### 5.3 不做的隐性收益

`translation_context.py` 和 `textwidth.py` 保持零依赖叶子（docstring 与 REVIEW_FINDINGS ⑥(a) 都把这当约束，[代码]）；运行时依赖维持 `pysubs2` 一个；CI 四矩阵零新风险；不违反修改流程第 6 条（不为未确认需求预留抽象层）——**两个卡点的需求恰好都还处于"未确认"状态**。

---

## 6. 推荐路线（问题 4）与最小落地范围（问题 5）

### 6.1 推荐：**现在不做，两个卡点分别挂触发条件；触发后按下述最小方案落地**

这不是"陪衬选项赢了"，是三组实测共同指向的结论：
1. 分词器解不了专名卡点（标签质量/体积/语言分叉三重实测证据，§1.3/§2）；
2. 换行卡点有完美的轻量解（budoux ja），但需求本身还不存在（零日文译文 artifact）；
3. Q7b 实测已把专名缺口定性为"精度与语义"，那是模型腿的主场，不是分词器的。

### 6.2 触发条件（什么证据会改变结论）

- **换行腿立项**：出现真实日文目标语言 Job，且 `wrap_target_lines` 返回 None / 坏切点进 review 的频次可观（有具体 Job 可指）。→ 落地 §6.3 的 budoux 腿。
- **专名腿立项**：出现真实 CJK 源 Job 且用户需要 glossary 候选。→ **先走 Q7b 模型腿**（不引分词器）；只有当模型腿因成本/离线被否时，才回头评估"Sudachi 只做日文"（中文侧 jieba 实测不过关，没有可推荐的确定性方案）。
- **推翻"换行用 budoux"的证据**：budoux ja 模型在真实日文译文上实测错断率不可接受（目前只有 5 句构造样本），或其发布节奏中断。
- **推翻"专名不用 jieba"的证据**：出现一个维护活跃、词性标签实测可靠、纯 Python 的中文分词器（今天没有）；或在 ≥10 份真实中文源语料上实测 jieba posseg 候选信噪比显著好于本次 960 字小样本。
- **推翻"现在不做"的证据**：上面任一触发条件成真——本评估反对的是"为还不存在的需求先背依赖"，不反对能力本身。

### 6.3 换行腿最小落地（届时照此做）

- **依赖形态**：`budoux>=0.10` 进 optional extra（如 `[project.optional-dependencies] cjk-wrap`），代码内 try-import。121KB 纯 Python、Apache-2.0、零传递依赖、`requires-python >=3.11` 与项目一致（全部 [实测-本人] 核过 PyPI）。CI 四矩阵零编译风险。
- **范围**：**只做日文**（`language in {"ja"}`）。中文维持按字切（§5.2 的负收益理由写进注释）。
- **插入点**：`segment.wrap_target_lines` 的 token 化一步（:298 附近），budoux 断块作为 token 来源，替代日文分支里 `\S` 单字 fallback；既有 Latin 段与 `_KATAKANA_RUN` 保护保留在外层（budoux 的 `Wh|at` 类错误被这层天然挡住）。DP、`forbidden_break` 复核、`None` 语义全部不动——分词器只是换了"原子"的定义，**切不开仍然 None 维持 review，QA 失败照旧停住（约束 4）**。
- **降级口径**：照 `layout_measurement_unavailable` 的精神——budoux 缺失（ImportError）或解析异常时，退回现行按字切 + 既有"无解退按字切"退路（segment.py:241-242 注释已预留），**不是放行而是回到保守行为**；不新增事件。
- **复用绑定（关键细节）**：`TARGET_WRAP_VERSION` 2→3，且 `target_wrap_binding()` 必须带上"是否启用分词器 + budoux 版本"——否则烧录腿 `_reusable_rendered_layout` 会把两种切分产出的布局 QA 混同复用（装没装 budoux 的机器产出不同换行，绑定不区分就违反"QA 量的就是烧出去的"同源原则）。
- **测试**：真实日文句子的切点断言（有/无 budoux 两种环境各一组，缺依赖用例走 try-import 打桩）；"不该红时不红"——zh/ko/Latin 行为零变化的回归断言；绑定变更的复用拒绝用例。
- **文档**：README 可选依赖说明、ARCHITECTURE 的 segment 条目、CHANGELOG（wrap.version 升版 = 在途 Job 布局 QA 不复用，属预期，照 2026-10-24 片假名条目的口径写）。
- **不做的**：不碰 `translation_context.py`（它保持零依赖叶子）；不给中文接 budoux；不预留"未来换 Sudachi"的抽象层（修改流程第 6 条）。

### 6.4 专名腿届时的形态（只记方向，不是本轮方案）

Q7b 模型腿按 ROADMAP 既定路径：`run_provider_batches` + checkpoint + `AWAIT_STEPS` 登记，产出仍是候选、仍要人工审核后经 `--glossary` 回流（Q7a 的"只产出候选，不产出事实"边界不变）。模型腿同时顺手覆盖英文侧实测漏掉的语义性术语（`Fable 5.1`、档位名），这是分词器永远给不了的。

---

## 8. 外部对照补记（2026-11-01，BaoCut 第二轮调研 + 主会话实测）

来源：[BaoCut 第二轮增量调研](baocut-rescan-2026-11-01.md) §4。BaoCut 本区间未引入分词器、断行判定零改动，所以**本评估的结论不变**；但它**既有**的 ICU 路线（`0898dbba`，上一轮调研基线之前）给出两点修正，都不推翻「现在不做」，只改「将来真做时该怎么做」。

### 8.1 负收益是硬禁则形态特有的，软惩罚可以避免

**[实测-本人]** BaoCut 把「不要断在中文词里」做成换行 DP 的**软成本**而非硬禁则：`crates/subtitle-render/src/raster.rs:753` 是 `let word_penalty = (40.0 * font_size * font_size).min(5e5);`，`:781` 处 `cost += word_penalty`；`:715-722` 的注释写明它「排在禁则与孤字之后：宁可断在词里，也不把句读推到行首」，且「词典不认识的词照样可能被拆开」。词边界来自 `speech_doc::word_breaks::han_word_interior`（ICU 中日文词典），只看两侧都是汉字的缝。

这恰好绕开 §5.2 担心的那条路径：**软成本永不产生无解**，词典错了只是让这一处断点变贵，DP 仍会在别处或原地断开，不会退化成 `None` → review。封顶 `.min(5e5)` 是这个保证的一部分。

**对本评估的含义**：§5.2 用「硬禁则 → 无解 → review 变多」论证过「中文引分词器可能是负收益」，该论证**只对硬禁则形态成立**。若将来中文换行真要词边界，正确形态是把它作为成本项加进 `_partition_words` 的 DP，**不是**扩 `forbidden_break`（那是全仓唯一禁则谓词，禁则是硬的）。§6.3 的 budoux 日文方案同理：budoux 断块当 token 是「换原子定义」，与软成本是两种接法，届时要明确选哪一种。

### 8.2 候选表漏了 ICU，但补测后它比 budoux 更差

§2 原先把中文轻量选项写成「只有 jieba」，漏了 ICU cjdict（已补进表格）。**[实测-本人]** 用 Node 24 的 `Intl.Segmenter`（ICU 78.3，与 PyICU 共用同一份 CLDR 词典）跑本文 §3 的同一组句子：

```
这 | 款 | 车 | 的 | 性 | 价 | 比 | 很高        ← `性价比` 碎成三字（budoux 为 `性价|比`）
舒适 | 性 | 和 | 操 | 控 | 性 | 都 | 不错      ← `舒适性` / `操控性` 均碎
从容 | 应对 | 各种 | 路 | 况                  ← 前两词对，`路况` 碎
马自达 | MX | - | 5 |  | 很 | 灵活            ← 品牌对，型号 `MX-5` 拆成三段
米 | 亚 | 塔 | 是 | 它的 | 另 | 一个 | 名字    ← 词典外专名碎（与 jieba 同）
别 | 以为是 | 靠 | 偷工 | 减 | 料 | 做到 | 的  ← `偷工减料` 碎，`以为是` 为错误合并
Which | Claude | model | should | you | use    ← 唯一强项：英文词不从中间断开
```

结论：**ICU 中文切分接近逐字，整体比 budoux zh 更差**（budoux 至少保住了 `性价比` 的前两字与 `MX-5`），唯一优于 budoux 的是不切坏英文单词。所以「中文侧没有质量过关的确定性方案」这一判断**被加强而不是削弱**，§1.3 的分水岭结论不变。

两点注意，避免过度解读：
- **[推断]** 8.1 与 8.2 很可能是配套的：词典质量这么差，硬禁则必然灾难，所以 BaoCut 只能把它做成软成本。这是对其设计动机的推测，没有它的文档或提交信息支持（本评估不采信那类材料）。
- **[未测]** BaoCut Rust 侧用 `icu_segmenter` 的 auto 模式，可能走 LSTM 中文模型而非上面这条词典路径，质量未必与 `Intl.Segmenter` 相同。触发条件成真时要按实际要用的绑定（PyICU 走哪条路径）重测，不能拿本节的数直接套。

## 7. 复核记录（区分实测与推断的落实）

本人亲自验证过（不依赖 subagent 转述）：
1. budoux 中文错断（`Wh|at`、`性价|比`、`从|容`）——在 subagent 建的 venv 里重跑复现
2. jieba posseg 标签乱象（`别以为/nr`、`舒适性/nr`、`马自达/nrfg`、`米亚|塔` 切碎）——同上重跑复现
3. budoux PyPI 元数据（0.10.2、py3-none-any 121KB、Apache-2.0、`>=3.11`）——WebFetch PyPI JSON
4. jieba PyPI（0.42.1、仅 sdist 19.2MB、MIT、2020-01-20 上传）——WebFetch PyPI JSON
5. SudachiPy 0.7.0 **逐文件**核 wheel 列表（cp310-abi3 × arm64/manylinux_2_28 双平台齐全）——WebFetch 版本专属 JSON
6. 项目依赖现状（pyproject：运行时仅 `pysubs2`）、HANDOFF 待办 4 / ROADMAP 4 原文、CI 矩阵（HANDOFF 快照表）

采信 subagent 但未逐一亲核的：fugashi/spacy-pkuseg 的 manylinux wheel 覆盖（引自 PyPI JSON，subagent B 给了 URL）；各库安装后的磁盘体积与性能计时（subagent C 实跑，原始输出已随临时目录失效）；源码行号与机制描述（subagent A，其中 wrap 同源、降级口径、插入点三处关键结论与 AGENTS.md/HANDOFF 的既有记述互洽，视为可靠）。

明确标注为推断的：中文按字断行的排版可接受性；Sudachi 日文专名召回的全面质量（只测了 5 句构造样本）；"自带词典做最大匹配比引库更贵"。
