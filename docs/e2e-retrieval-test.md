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
