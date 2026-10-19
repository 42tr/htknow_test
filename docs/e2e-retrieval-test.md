# 端到端检索测试（e2e_retrieval_test.py）

对一次真实部署做「建库 → 上传 → 解析 → 索引 → 检索 → 指标评估 → 报告」的全链路自动化验收。
脚本零第三方依赖（Python 3.8+ 标准库即可运行），报告为自包含 HTML（内联 CSS + SVG，无 CDN，可离线/内网传阅）。

```
scripts/e2e_retrieval_test.py     测试主程序
scripts/e2e_corpus.json           语料清单与黄金问题集（ground truth）
scripts/.corpus/                  语料下载缓存（自动生成，已 gitignore）
scripts/reports/                  HTML / JSON 报告输出目录（自动生成，已 gitignore）
```

## 快速开始

```shell
# 最常用：指定 HOST:PORT
python3 scripts/e2e_retrieval_test.py --target 192.168.0.46:11000

# 只测纯文本与合成文档（不依赖 MinerU / Office 转换服务）
python3 scripts/e2e_retrieval_test.py --target 127.0.0.1:3000 \
  --docs art_of_war,sanguo_yanyi,needle_synthetic

# 排除需要 Office 转换服务的 DOCX
python3 scripts/e2e_retrieval_test.py --target 127.0.0.1:3000 --exclude calibre_docx_demo

# 只准备并校验语料，不连服务（排查网络/清单问题）
python3 scripts/e2e_retrieval_test.py --dry-run

# 放宽门禁（例如已知部署缺少 Office 转换服务）
python3 scripts/e2e_retrieval_test.py --target 127.0.0.1:3000 --min-parse-success 60
```

退出码：`0` 全部关键门禁达标；`1` 存在未达标项；`2` 脚本或环境错误。可直接接入 CI。

## 测试流程

1. **预检**：`GET /api/v1/knowledge/knowledge_base/` 验证连通性与认证头；顺带读取 `/api-docs/openapi.json` 记录版本。
2. **备料**：按 `scripts/e2e_corpus.json` 从公网镜像下载语料，做 SHA-256 内容寻址校验 + 魔数嗅探
   （防止把 HTML 错误页当 PDF 上传）；同时本地生成一份合成「大海捞针」文档。
   文本类语料会追加本次运行唯一标记，改变文件哈希，避免服务端
   `HTKNOW_REUSE_DUPLICATE_FILES` 复用历史解析结果导致解析耗时失真。
3. **建库**：`POST /knowledge_base/` 创建一次性知识库（`kb_type=analysis`，默认 `parse_priority=90`）。
4. **上传**：`POST /files/` 逐文件上传（`immediate_parse=true`），记录上传耗时。
   `--upload-mode batch` 可改为先全部上传再统一等待，用于测排队吞吐。
5. **等解析**：轮询 `GET /files/{id}` 至终态（1 完成 / 3 跳过 / -1 失败），
   记录客户端墙钟耗时与服务端 `updated_at - created_at` 两个口径；再读 `GET /files/{id}/slices` 统计切片数与字符数。
6. **检索**：`GET /search/?query=...&kb_id=...` 执行黄金问题集，每条重复 `--repeats` 次采样延迟并做结果稳定性检验；
   随后执行负样本对照。
7. **评估与报告**：计算信息检索指标与门禁，输出 HTML + JSON 报告；按 `--cleanup` 策略处置测试知识库
   （默认 `delete-on-pass`：通过即删除，失败保留现场便于排查）。

## 语料与黄金问题集

`scripts/e2e_corpus.json` 中每个文档声明：来源镜像（含 SHA-256）、格式、语言、许可证，
以及一组**黄金问题**。每条黄金问题绑定其证据所在文档，并声明：

- `keywords_all`：Top-K 中每个词都必须出现在**某一条切片正文内**的关键词（答案在上下文判定）
- `keywords_any`：至少出现其一即可的候选词
- `expect_rank_max`：可选，要求证据文档的最高排名不超过该值
- `grade`：相关性等级（默认 2，用于 nDCG 的增益）

所有关键词均已逐条对照原文核实。匹配时对 PDF 解析常见的空格丢失做了兼容（原文匹配失败后再去空格匹配一次）。
关键词命中一律**逐切片判定**：把 Top-K 正文拼成一整块再匹配，会因为去空格而让跨切片边界的词「假命中」，
把答案其实不在上下文的情况误判为通过。

内置语料（均可公开下载，许可证见清单）：

| id | 内容 | 考察点 |
| --- | --- | --- |
| `art_of_war` | 《孙子兵法》Giles 英译全本 | 纯文本解析、英文长文档切片与召回 |
| `sanguo_yanyi` | 《三國演義》繁体中文全本 | jieba 中文分词、中文长文本召回 |
| `rag_paper` | RAG 原始论文 PDF（arXiv 2005.11401） | MinerU PDF 解析、学术事实召回 |
| `somatosensory` | 多栏学术小 PDF | 小文件解析速度、复杂版面召回稳定性 |
| `calibre_docx_demo` | Word DOCX 样例（`optional`） | Office → PDF 转换链路；缺服务时自动降级为告警 |
| `needle_synthetic` | 本地生成的「大海捞针」文档 | 隔离验证 上传→切片→Tantivy→检索 链路本身，并提供负样本 |

`needle_synthetic` 在 64 段填充文本中埋入全局唯一 token，查询即该 token；
负样本使用同命名空间但从未写入文档的 token，断言 Top-K 正文中不出现，用于检验假阳性。
它不依赖任何外部解析质量，是判断「检索链路本身是否正确」的基线。

扩展自己的语料：在清单中追加一个文档对象即可；也可以直接 `--extra-file path` 上传本地文件
（只统计解析，不参与黄金问题评估）。

## 指标口径

以**文档**为相关性单元（每条黄金问题绑定证据文档），在 Top-K 上计算：

| 指标 | 含义 |
| --- | --- |
| `Hit@K` | 至少一条命中证据文档的问题占比 |
| `MRR@K` | 首个命中排名的倒数均值 |
| `nDCG@K` | 文档级 qrels：命中按 `file_id` 折叠（取该文档最好的排名与最高增益），IDCG 按相关**文档**数（上限 K）构造，因此证据文档排在第 1 位时 nDCG = 1；不会再因同一文档被切成多条切片而虚高或超过 1 |
| `Precision@K` / `Recall@K` | 标准定义；单证据文档设计下 Recall@K 与 Hit@K 数值一致 |
| 答案在上下文率 | `keywords_all` 逐切片全部命中且 `keywords_any` 至少命中一个的问题占比（等价 RAG 的 Answer-in-Context） |
| 关键词覆盖率 | 期望关键词的平均命中比例 |
| 结果稳定性 | 相同问题重复检索、Top-K 结果序列完全一致的比例 |
| 负样本对照通过率 | 未注入内容未被召回的比例（假阳性检验） |
| 延迟分位数 | 全部检索请求的 P50/P90/P95/P99/max/均值/标准差 |

解析侧指标：上传耗时、解析耗时（客户端墙钟 + 服务端口径）、切片数/字符数、吞吐 KB/s、解析成功率。
**解析成功率门禁只统计必测文档**；清单中标记 `optional: true` 的文档失败会降级为告警。

Top-K 说明：检索接口没有 top_k 参数，返回条数受服务端 `HTKNOW_SEARCH_LIMIT`（默认 10）限制。
脚本默认 `--top-k 10`；若服务端返回更少，指标会按实际生效的 K 计算并在报告中告警。

## 门禁与退出码

默认门禁（均可用命令行覆盖）：

| 门禁 | 默认阈值 |
| --- | --- |
| 解析成功率（必测文档） | ≥ 100% |
| Hit@K | ≥ 80% |
| MRR@K | ≥ 0.60 |
| nDCG@K | ≥ 0.40 |
| 答案在上下文率 | ≥ 70% |
| 负样本对照通过率 | = 100% |
| 检索 P95 延迟 | ≤ 5000 ms |
| 关键词覆盖率（非关键） | ≥ 60% |
| 单文件解析 P95（默认不启用） | `--max-parse-ms` 指定后生效 |

未设置阈值的门禁显示为「未启用」，不参与判定。任一**关键**门禁未达标则退出码为 1。

## 报告

- `scripts/reports/e2e-report-<run>.html`：自包含单文件，含结果概览、门禁表、环境配置、
  解析明细与耗时条形图、检索质量指标条与分文档对比、延迟分布折线图（叠加 P50/P95/P99）、
  逐条黄金问题的 Top-K 命中展开、负样本对照、失败诊断与排查建议。适合打印或归档。
- 同名 `.json`：机器可读的完整结果，便于接入监控或做跨版本趋势对比。

## 排查建议

报告「结论与排查建议」会根据失败特征自动给出线索，常见情况：

- **解析失败且日志含 embedding**：检查 `HTKNOW_EMBEDDING_URL` / `HTKNOW_EMBEDDING_KEY`；
  长文档可调大 `HTKNOW_EMBEDDING_BATCH_TIMEOUT_SECS`、`HTKNOW_EMBEDDING_BATCH_MAX_CHARS`。
- **PDF 解析失败**：检查 `HTKNOW_MINERU_URL` 是否可达；页数过大可用 `HTKNOW_MINERU_MAX_PAGES` 限制。
- **DOCX 解析失败**：检查 `HTKNOW_OFFICE_CONVERT_URL`；该文档默认 `optional`，可 `--exclude calibre_docx_demo`。
- **解析一直停在待处理**：确认服务端 `HTKNOW_PARSE_ENABLED=true`，或用 `--parse-timeout` 放宽等待。
- **命中但关键词覆盖不足**：尝试更换 `--slice-type`（`smart` / `fixed` / `paragraph`）对比切片粒度。
- **负样本泄漏**：检查 `HTKNOW_RERANK_URL` 是否配置、`HTKNOW_RERANK_THRESHOLD` 是否过松。

## 生成答案评估（chat / both）

`--mode retrieval` 保持原有检索流程；`--mode chat` 使用本地合成事实评估对话；
`--mode both` 在同一个主知识库上完成检索与对话，最后按整体结果清理。
对话模式默认不下载原有公开语料；显式指定 `--docs` 时仍会准备对应公开文档。

评审模型通过 `E2E_JUDGE_URL`、`E2E_JUDGE_MODEL`、`E2E_JUDGE_API_KEY` 配置，或用
`--judge-url` / `--judge-model` 覆盖非敏感配置。密钥没有命令行参数，不写入报告和元数据。
评审请求使用两个独立提示：正确性评审看到参考事实，忠实度评审只看到本轮实际来源。
返回的事实 ID、布尔字段、答案引句、来源引句和引用编号均进行结构校验；校验失败记为评审错误。
`htknow-answer-v5` 为本轮答案建立句子/行片段索引，为可见来源建立逐行片段索引；
忠实度评审选择 `answer_span_ids` / `passage_ids`，由程序从不可变原文生成引句。
选中不相邻片段时保留所有中间文字，不拼接引句；片段编号去重排序，无效编号、未知来源仍拒绝。
实际引用编号由答案片段中的 `[n]` 决定。评审额外填入的来源不会成为答案引用，
这类对齐记录保存在 `citation_alignment`；缺失的实际引用必须校正，不能从其他支持来源推定为有效。
若陈述已被判为 `unsupported` 且没有支持证据，则缺失引用只能推导为无支持（记录 `derived_from`），不能推导为有效。
正常的“当前检索未找到信息”拒答不要求事实证据；拒答中附带的事实、明确的否定事实仍逐条核验。
信息不足说明单独保存在 `information_limits`，与有证据可核验的事实分开。
`htknow-answer-v5.1` 对重复事实遗漏引用的情况反馈具体答案片段编号、原文和缺失引用编号，
要求分别检查无引用概述与带引用的后续表述，不用概述的评审替代后续引用核验。
`htknow-answer-v5.2` 将 `A[1]，B[2]。` 这类分别标注引用的分句拆开索引，
避免把 B 的引用错误地归到 A 的事实陈述。
`htknow-answer-v6` 对正确性评审也使用答案片段编号；一次校正前同时收集引用评审缺口和
完全遗漏的带引用陈述，避免专项校正修好一个字段后才发现另一类遗漏。
`v6.1` 在校正前逐条收集全部结构错误；仅当错误全部属于缺失引用核验时使用专项校正，
混有拒答说明或引句错误时反馈完整错误列表，仍最多一次校正。
`v6.2` 对“有支持但无引句”的条目单独做信息不足分类；只有模型明确判定为当前检索局限的
说明才移入 `information_limits`，事实断言仍需证据；引用核验的负面结论保持不变。

修订评审器后可重评保存的全部答案，保持生成结果与实际证据不变，原报告另行保留：

```shell
python3 scripts/e2e_retrieval_test.py --rejudge-report /path/to/source/report.json \
  --report /path/to/new/report.html --json-report /path/to/new/report.json
```

Web API 支持 `POST /api/runs/<来源运行ID>/rejudge`，继承来源运行的参数与门槛，
在独立运行目录中并发评审全部轮次。复评只调用评审模型，不调用 HTKnow；
报告明确展示来源运行、原报告 SHA-256 和复评标记，失败与未完成轮次仍留在计划分母中。
`--rejudge-errors-only` 或 API 的 `{"errors_only":true}` 仅重评模型评审异常；
其余已完成评分保持不变，报告分别标注复评与继承轮数，并保留来源版本及逐行继承记录。
若评审遗漏实际引用，唯一一次校正会逐对检查“陈述 + 指定引用来源”，
仅补齐这些引用的评审，不重写已有事实结论。格式不完整的模型 JSON 也纳入这一次校正并保留用量。
历史或兼容格式直接返回引句时继续使用下述严格匹配，不改变原有验收门槛。
引句优先逐字匹配；仅在匹配失败时忽略成对的加粗/斜体/内联代码标记，并将连续空白折叠成一个空格。
`htknow-answer-v4` 进一步兼容无序列表的项目符号；同级相邻列表项以中文句末标点结束时，
允许引句省略两项之间的排版空白。代码、数字编号、词间空白和中间的实质内容不会被删除。
校验成功后使用原文中的实际连续片段核验引用并写入报告。人名、数字、单位、标点、引用编号和实质文字
不会被改写，也不允许把不连续的来源拼接为引句；原始评审响应仍单独保存在运行目录中。
每个评审阶段遇到结构或引句校验失败时，最多请求一次格式校正，反馈具体校验错误并再次严格校验。
引句错误同时反馈字段路径（如 `claims[5].support[1].quote`）、来源编号与被拒绝的引句；
这份诊断保存在校正记录中，校正仍失败时也保留在逐题 JSON 的 `judge.validation_details` 中。
校正前后响应分别保存为 `judge-<阶段>-response.json` 和 `judge-<阶段>-repair-response.json`，
额外调用与 token 用量计入报告；校正后仍无效就保留评审失败状态，不重试对话生成。

`htknow-answer-v3` 按语义核验引用，区分资料的泛称（如“参数表 / 参数概览”）与精确出处声明。
Wiki 正文包含对应事实时，可支持该事实及资料内容的泛称，不因标题措辞不同直接扣分。
精确文件名、标题、原文逐字引用、原始页码或切片位置仍需对应证据；Wiki 不能替代原文证明出处。
每条实际引用仍须独立有效，其他来源包含相同事实不会自动修正错误引用。版本和提示哈希均写入报告。

`htknow-answer-v6.3` 还检查分类一致性：同一未引用片段不能同时被列为事实和信息不足说明。
发生冲突时请求一次结构校正，保留原始判定与校正记录；纯信息不足应归入说明，真实事实仍须保留，
不能用信息不足标签豁免未引用的正面事实。

`htknow-answer-v6.4` 修正跨文档推理的引用评审：计算、比较及实体映射中的来源可通过提供必要前提支持结论，
不要求每个来源独自记载最终答案。引用补查同时接收该答案片段实际附带的其他来源作为推理上下文，
不接收未引用的来源；仍须从当前待查来源选择支持其必要贡献的原文片段。答案自身及前文计算结果不能作为证据，
每一步都要追溯原始数字和关系。直接事实的无关引用仍判错，例如单价事实不需要只记载数量的来源。

内置题集有 80 个用例、100 轮请求。原有基线保持 60 个用例、70 轮：`facts` 15、
`cross_document` 10、`abstention` 10、`multi_turn` 10 个双轮用例、`wiki` 5、`safety` 10。
每次生成新项目名、人名、日期、金额和标记，
`--answer-seed` 可固定事实，题集 SHA-256 和实际种子写入报告。
用 `--answer-categories` 筛选分类、`--answer-limit` 限制用例数，`--answer-repeats` 重复独立对话。
`--answer-cases` 接受同样结构的自定义 JSON，文档和问题模板随运行实例化。

新增的 20 个复杂用例共 30 轮，使用独立的 `complex` 知识库，避免改变旧基线的检索上下文。
复杂事实使用独立随机序列，同一个种子生成的原有项目事实保持不变。按以下分类筛选：

| 分类 | 用例 / 轮数 | 验证重点 |
| --- | --- | --- |
| `complex_reasoning` | 10 / 10 | 已审批版本与草案、历史生效时间、AND准入条件与P0例外、跨文档关联、计划与实际数量、分步计费与一次性抵扣 |
| `partial_answer` | 5 / 5 | 已知部分仍回答；未知违约金、实测指标、备用联系人、到账金额与RPO不编造 |
| `advanced_multi_turn` | 5 / 15 | 三轮中的主体切换、连续计算、时间范围修正、错误前提纠正、拒答后恢复作答、假设结束后回到事实 |

复杂推理的金标由文档中的原始参数确定，金额计算结果不会预先写进文档。部分可回答题使用
`expected_behavior=partial`，同时检查已知事实完整性和对未知部分的明确保留；多轮历史始终采用服务
实际生成的回答。新增对照库解析失败或无法检索时保留所有计划轮次为失败，不把夹具故障当成正确拒答。

主知识库保存计划、财务、运维和不可信附件。权限测试另外创建非管理员所有者的私有知识库，
先验证所有者能检索到机密标记，再使用另一名普通用户访问，防止“文档没入库”造成权限测试假通过。
Wiki 测试另外创建 Wiki 对照库、手工 Wiki 页面和较新的冲突原文，检查证据类型与版本选择。
这些用例检验 Wiki 证据的使用；不评估 Wiki 自动生成质量。Wiki 搜索未启用或对照初始化失败时，
相关用例保留为失败，不从统计中跳过。所有测试库都遵循同一个 `--cleanup` 策略。

每轮保存全部 `status/sources/delta/done/error` 事件、累计来源列表和实际历史。
流必须有正常 `done`、非空正文，且来源编号不能被后续检索覆盖。
断流、超时、无访问权限却返回来源、伪造引用和输出机密/干扰标记都会失败。
后续追问使用真实已完成回答，按前端相同的最近 6 轮、16000 字符预算构造历史；
前一轮断流时后续问题记为无法执行，仍保留在分母中。

默认门禁：

| 项目 | 默认要求 |
| --- | --- |
| 正常完成率、语义评审覆盖率、规则通过率 | 100% |
| 答案正确率、参考事实覆盖率 | ≥ 85% |
| 事实陈述的证据忠实度 | ≥ 95% |
| 引用准确率 | ≥ 95% |
| 引用覆盖率 | ≥ 90% |
| 无答案题正确处理率 | ≥ 90% |
| 总耗时 / 首段文本 P95 | 默认不启用，先建立实测基线 |

正确率与参考事实覆盖率的分母包含计划用例中的失败项；忠实度和引用质量按已评审陈述计算，
并同时要求语义评审覆盖率 100%。不适用于当前筛选题集的指标显示未启用；
应当有实测数据但缺失的关键项视为未通过。仅规则模式 `--judge-mode rules` 的语义覆盖率为 0，
不会给出语义验收通过结论，预期退出码为 1。所有阈值都是初始基线，需结合人工复核校准。

HTML 报告支持按结果、题型和问题搜索逐题复盘，直接对照模型回答、参考事实、陈述评分与实际证据。
点击引用编号可定位对应原文；答案问题、评审未完成和执行异常分别展示，原始规则与评审 JSON 按需展开。
Web 中已有的答案报告会从原始 JSON 使用当前样式展示，不修改历史评分和保存的报告文件。

HTML / JSON 中保留完整答案和逐事实评分；报告旁的 `<报告名>-answers/` 中逐题保存：
`request.json`、`stream.sse`、`events.jsonl`、`response.json`、`evaluation.json`，
以及 `judge-accuracy-*`、`judge-grounding-*` 的请求与响应。评分只使用
`sources[].result.content`，不扩展到未传给生成模型的 Wiki 全文。评审模型版本、提示版本/哈希、
温度和提供方实际返回的 token 用量一并记录；不估算不存在的用量。

离线回归：`python3 -m unittest discover -s tests -v`，使用本地模拟 HTKnow 与评审服务，
验证协议和运行流程；该测试结果不代表真实生成模型的答案质量。

来源判定的真实模型回归可显式运行：
`E2E_JUDGE_LIVE_TESTS=1 python3 -m unittest discover -s tests -p test_judge_provenance.py -v`。
它使用已配置的评审接口，检查 Wiki 内容泛称、虚构事实、虚构文件名和原文出处声明；
只调用评审模型，不访问 HTKnow。默认离线回归会跳过这些外部模型调用。

## Web 控制台（e2e_web.py）

不想记命令行参数时，可以用零依赖的 Web 控制台：浏览器里输入 `HOST:PORT` 即可发起同样的全链路测试，
并实时查看日志、门禁与指标、HTML 报告与历史运行。

```shell
python3 scripts/e2e_web.py                              # 默认监听 127.0.0.1:8099
python3 scripts/e2e_web.py --bind 0.0.0.0 --port 8099   # 允许同网段访问（控制台无鉴权，自行评估）
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--bind` | `127.0.0.1` | 监听地址，`0.0.0.0` 表示对所有网卡开放 |
| `--port` | `8099` | 监听端口 |
| `--runs-dir` | `scripts/.e2e-web/runs` | 每次运行的日志 / HTML / JSON 报告存放目录 |
| `--max-concurrent` | `2` | 同时运行的测试数量上限 |
| `--max-history` | `50` | 启动时保留的历史运行条数，超出连同产物目录删除；`0` 表示不清理 |

行为说明：

- 每次测试是独立子进程（`python3 -u e2e_retrieval_test.py ...`），与 Web 服务解耦，可单独取消；
  取消或停止服务时会终止对应子进程，避免残留。
- 运行产物按 `runs/<时间戳>-<随机>/` 归档：`console.log`、`report.html`、`report.json`、`meta.json`；
  服务重启后历史仍可查看（重启时未结束的运行标记为「已中断」）。
- 页面支持 `?run=<运行 id>&tab=log|metrics|report` 深链，便于把某次结果直接分享给同事；
  运行头部悬停可看到等效命令行，可直接复制到终端复跑。
- HTTP API（供二次集成 / CI 调用）：`GET /api/info`、`GET /api/corpus`、`GET /api/defaults`、
  `GET|POST /api/runs`、`GET /api/runs/<id>`、`POST /api/runs/<id>/cancel`、
  `GET /api/runs/<id>/log?offset=`、`GET /api/runs/<id>/report`、`GET /api/runs/<id>/json`。
- 控制台没有鉴权：绑定到非回环地址前请确认所在网络可信，或置于反向代理 / VPN 之后。

### 用 Docker 部署 Web 控制台

容器化部署见 `deploy/e2e-web/`（含 `Dockerfile`、`build.sh`、`docker-compose.yml` 与说明）：

```shell
./deploy/e2e-web/build.sh                                     # 构建镜像；本机有语料缓存时烘进镜像，可完全离线
docker compose -f deploy/e2e-web/docker-compose.yml up -d     # 本机启动：host 网络，监听 8099
```

默认 host 网络（可测局域网与宿主机自身任意地址）+ `/data` 命名卷持久化日志与报告，
`restart: unless-stopped` 开机自拉起；启动后打开 `http://<本机IP>:8099`。
环境变量、桥接网络改法与运维命令见 [deploy/e2e-web/README.md](../deploy/e2e-web/README.md)。
