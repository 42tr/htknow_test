# htknow_test · HTKnow 检索与答案评估

对一次真实部署的 [HTKnow](https://github.com/42tr/htknow) 服务做
「建库 → 上传 → 解析 → 索引 → 检索 → 指标评估 → 报告」的全链路自动化验收。

- **零第三方依赖**：Python 3.8+ 标准库即可运行，无需 `pip install`
- **带 ground truth 的评测**：内置可公开下载的语料与逐条核实过原文的黄金问题集，
  计算 Hit@K / MRR@K / nDCG@K / 答案在上下文率 / 关键词覆盖率与延迟分位数
- **门禁化**：任一关键门禁未达标即以退出码 `1` 失败，可直接接入 CI
- **自包含报告**：单文件 HTML（内联 CSS + SVG，无 CDN），可离线在内网传阅、归档、打印
- **生成答案评估**：60 个合成事实用例、70 轮请求，覆盖事实、汇总、无答案、多轮、Wiki 与权限隔离；
  分别计算答案正确性、证据忠实度、引用质量与评审覆盖率

本仓只含测试工具，不含被测服务代码。

## 仓库结构

```
scripts/e2e_retrieval_test.py     测试主程序（CLI，可接入 CI）
scripts/e2e_web.py                Web 控制台（浏览器操作 + 历史运行 + HTTP API）
scripts/e2e_corpus.json           语料清单与黄金问题集（ground truth）
scripts/e2e_answer_eval.py        对话评估入口、完整 SSE 采集与逐题证据报告
scripts/e2e_answer_report.py      可筛选的逐题复盘、参考事实对照与引用证据定位
scripts/e2e_answer_cases.json     60 个答案评估用例及运行时合成事实模板
scripts/e2e_judge.py              独立模型评审、评分结构与证据引句校验
tests/test_answer_eval.py         离线协议与端到端回归测试
docs/e2e-retrieval-test.md        指标口径、门禁阈值、语料说明与排查建议
deploy/e2e-web/                   Web 控制台的容器化部署（Dockerfile / compose / build.sh）
```

运行产物（自动生成，已 gitignore）：`scripts/.corpus/`（语料下载缓存）、
`scripts/reports/`（HTML / JSON 报告）、`scripts/.e2e-web/runs/`（控制台历史运行）。

## 快速开始（命令行）

```shell
# 最常用：指定被测服务的 HOST:PORT
python3 scripts/e2e_retrieval_test.py --target 192.168.0.46:11000

# 只测纯文本与合成文档（不依赖 MinerU / Office 转换服务）
python3 scripts/e2e_retrieval_test.py --target 127.0.0.1:3000 \
  --docs art_of_war,sanguo_yanyi,needle_synthetic

# 排除需要 Office 转换服务的 DOCX
python3 scripts/e2e_retrieval_test.py --target 127.0.0.1:3000 --exclude calibre_docx_demo

# 只准备并校验语料，不连服务（排查网络/清单问题）
python3 scripts/e2e_retrieval_test.py --dry-run
```

退出码：`0` 全部关键门禁达标；`1` 存在未达标项；`2` 脚本或环境错误。

完整参数、测试流程、门禁阈值与失败排查见 [docs/e2e-retrieval-test.md](docs/e2e-retrieval-test.md)。

## 生成答案评估

默认仍为仅检索。答案评估使用独立的 OpenAI 兼容评审接口，与 HTKnow 自身的生成模型分别配置。
评审接口支持完整的 `/chat/completions` 地址，也接受会自动追加该路径的 base URL。

```shell
export E2E_JUDGE_URL='https://your-provider.example/v1/chat/completions'
export E2E_JUDGE_MODEL='your-judge-model'
# 如果接口需要鉴权，在运行环境中设置 E2E_JUDGE_API_KEY。

# 只评估对话：使用本地合成文档，不下载原有公开语料
python3 scripts/e2e_retrieval_test.py --target 192.168.0.46:11000 --mode chat

# 检索与答案共用主知识库，全部评估完成后再按整体结论清理
python3 scripts/e2e_retrieval_test.py --target 192.168.0.46:11000 --mode both

# 小规模试跑并固定事实，便于对比两个版本
python3 scripts/e2e_answer_eval.py --target 192.168.0.46:11000 \
  --answer-categories facts,multi_turn --answer-limit 3 --answer-seed baseline-v1

# 只准备题集和事实文档，无需被测服务或评审模型
python3 scripts/e2e_retrieval_test.py --mode chat --dry-run --offline

# 只验证协议、精确事实与引用规则；语义门禁未通过，预期退出码为 1
python3 scripts/e2e_retrieval_test.py --target 192.168.0.46:11000 \
  --mode chat --judge-mode rules --answer-limit 2
```

Web 页面可选择「仅检索 / 对话答案 / 完整链路」，填写独立评审接口和模型，或继承启动进程的环境配置。
密钥由服务端读取，不通过页面传入。配置了服务端密钥时，页面不能将该密钥转发到其他评审地址。
本机也可将这三项写入已忽略的 `scripts/.e2e-web/judge.json`，供 CLI 和控制台在重启后读取；
环境变量优先，`E2E_JUDGE_CONFIG` 可指定其他配置文件。密钥为空或 `None` 表示不附加鉴权头。

完整答案、实际截断后的来源正文、历史、金标事实和评分保存在 HTML / JSON 报告中。
报告旁的 `<报告名>-answers/<用例>/repeat-N/turn-N/` 保存原始 SSE、事件时间线和两次独立评审的请求、结果。
没有评审配置时会在建库前报错；评审失败和断流会保留在计划分母内，不会作为通过样本。

当前内置答案基线为运行时合成的业务事实，Wiki 对照页面由测试直接创建，用于检验证据使用和冲突处理；
Wiki 自动生成质量不在这组用例的评估范围内。原有公开 TXT/PDF/DOCX 语料继续用于检索测试。
`--answer-cases` 可加载自定义文本事实与问题；初始语义阈值需要结合人工复核校准。

运行离线回归测试：`python3 -m unittest discover -s tests -v`。

## Web 控制台

不想记命令行参数时，用零依赖的 Web 控制台：浏览器里输入 `HOST:PORT` 即可发起同样的全链路测试，
实时查看日志、门禁指标与 HTML 报告，并保留历史运行。

```shell
python3 scripts/e2e_web.py                              # 默认监听 127.0.0.1:8099
python3 scripts/e2e_web.py --bind 0.0.0.0 --port 8099   # 允许同网段访问（控制台无鉴权，自行评估）
```

支持 `?run=<运行 id>&tab=log|metrics|report` 深链分享单次结果，
并提供 `/api/runs` 等 HTTP API 供二次集成或 CI 调用。

> **安全提示**：控制台没有鉴权，绑定到非回环地址前请确认所在网络可信，
> 或置于反向代理 / VPN / 防火墙白名单之后。

## 容器化部署

```shell
./deploy/e2e-web/build.sh                                     # 构建 htknow-e2e-web:latest
docker compose -f deploy/e2e-web/docker-compose.yml up -d     # 本机启动：host 网络，监听 8099
```

镜像基于 `python:3.12-slim`，运行时同样零第三方依赖；本机若已有语料缓存
（`scripts/.corpus/`）会烘进镜像，容器可**完全离线**测试。默认 host 网络
（能直接测局域网与宿主机自身任意地址）+ `/data` 命名卷持久化日志与报告，
`restart: unless-stopped` 开机自拉起，启动后打开 `http://<本机IP>:8099`。

环境变量、桥接网络改法与运维命令见 [deploy/e2e-web/README.md](deploy/e2e-web/README.md)。

## 内置语料

均可公开下载，许可证与 SHA-256 见 `scripts/e2e_corpus.json`：

| id | 内容 | 考察点 |
| --- | --- | --- |
| `art_of_war` | 《孙子兵法》Giles 英译全本 | 纯文本解析、英文长文档切片与召回 |
| `sanguo_yanyi` | 《三國演義》繁体中文全本 | jieba 中文分词、中文长文本召回 |
| `rag_paper` | RAG 原始论文 PDF（arXiv 2005.11401） | MinerU PDF 解析、学术事实召回 |
| `somatosensory` | 多栏学术小 PDF | 小文件解析速度、复杂版面召回稳定性 |
| `calibre_docx_demo` | Word DOCX 样例（`optional`） | Office → PDF 转换链路；缺服务时降级为告警 |
| `needle_synthetic` | 本地生成的「大海捞针」文档 | 隔离验证上传→切片→索引→检索链路，并提供负样本 |
