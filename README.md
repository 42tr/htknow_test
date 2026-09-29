# htknow_test · HTKnow 端到端检索测试

对一次真实部署的 [HTKnow](https://github.com/42tr/htknow) 服务做
「建库 → 上传 → 解析 → 索引 → 检索 → 指标评估 → 报告」的全链路自动化验收。

- **零第三方依赖**：Python 3.8+ 标准库即可运行，无需 `pip install`
- **带 ground truth 的评测**：内置可公开下载的语料与逐条核实过原文的黄金问题集，
  计算 Hit@K / MRR@K / nDCG@K / 答案在上下文率 / 关键词覆盖率与延迟分位数
- **门禁化**：任一关键门禁未达标即以退出码 `1` 失败，可直接接入 CI
- **自包含报告**：单文件 HTML（内联 CSS + SVG，无 CDN），可离线在内网传阅、归档、打印

本仓只含测试工具，不含被测服务代码。

## 仓库结构

```
scripts/e2e_retrieval_test.py     测试主程序（CLI，可接入 CI）
scripts/e2e_web.py                Web 控制台（浏览器操作 + 历史运行 + HTTP API）
scripts/e2e_corpus.json           语料清单与黄金问题集（ground truth）
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
