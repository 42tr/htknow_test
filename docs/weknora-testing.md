# WeKnora 测试

同一套 CLI 和 Web 控制台支持 `htknow`（默认）与 `weknora`。WeKnora 适配负责建库、上传、解析轮询、分页读取分块、检索、服务端会话、SSE 和测试资源清理。题集、独立评审、指标及验收阈值共用。

## 配置

在运行 CLI 或启动 Web 控制台的进程环境中配置：

```sh
export WEKNORA_BASE_URL='http://127.0.0.1:8080'
export WEKNORA_API_KEY='your-api-key'
export WEKNORA_TEMPLATE_KB='an-existing-knowledge-base-id'
```

也可以用 `WEKNORA_TOKEN` 配置 JWT，不能与 `WEKNORA_API_KEY` 同时设置。密钥只从进程环境读取，不通过命令行、页面或报告传递。有凭据时目标根地址必须与 `WEKNORA_BASE_URL` 一致；适配器拒绝重定向。代理部署可在 CLI 中使用包含代理前缀的根地址。

模板知识库只读，用来复制 Embedding、摘要模型、分块和存储配置；测试会创建独立知识库，不会上传到模板库或删除模板库。不使用模板时，设置 `WEKNORA_EMBEDDING_MODEL_ID` 或 `--weknora-embedding-model-id`。聊天模型和重排序模型可以用 `WEKNORA_CHAT_MODEL_ID`、`WEKNORA_RERANK_MODEL_ID` 覆盖；未指定时，聊天优先使用模板摘要模型，其余沿用 WeKnora 的空间默认设置。报告记录模型选择及后端类型。

可选配置与 CLI 参数：

| 环境变量 | CLI 参数 |
| --- | --- |
| `WEKNORA_TEMPLATE_KB` | `--weknora-template-kb` |
| `WEKNORA_EMBEDDING_MODEL_ID` | `--weknora-embedding-model-id` |
| `WEKNORA_CHAT_MODEL_ID` | `--weknora-chat-model-id` |
| `WEKNORA_RERANK_MODEL_ID` | `--weknora-rerank-model-id` |
| `WEKNORA_TENANT_ID` | `--weknora-tenant-id` |

凭据应能管理测试知识库、导入文档、检索、创建和删除会话。受知识库白名单限制的 key 还需要允许访问新建测试库。`--public` 属于 HTKnow 的权限模型，WeKnora 不接受该参数。分块采用 WeKnora 知识库配置，不将 HTKnow 的切片策略直接映射为另一种算法。

## 运行

```sh
# 仅检索：本地生成语料，无公网文档下载
python3 scripts/e2e_retrieval_test.py --backend weknora \
  --docs needle_synthetic --offline

# 基础问答和真实多轮；独立评审沿用 E2E_JUDGE_* 配置
python3 scripts/e2e_retrieval_test.py --backend weknora --mode chat \
  --answer-categories facts,cross_document,abstention,multi_turn \
  --answer-seed comparison-v1

# 复杂推理、部分回答和条件切换
python3 scripts/e2e_retrieval_test.py --backend weknora --mode chat \
  --answer-categories complex_reasoning,partial_answer,advanced_multi_turn \
  --answer-seed comparison-v1

# HTKnow 仍使用原命令；也可以显式选择后端
python3 scripts/e2e_retrieval_test.py --backend htknow --target 127.0.0.1:13000 --mode chat
```

Web 控制台选择“被测系统 → WeKnora”，填写目标地址和可选模板 ID。凭据、其余模型配置由服务端环境提供。每次运行固定自己的后端与配置，可以与 HTKnow 测试并存。Docker Compose 已透传上述环境变量及独立权限对照凭据。

## 对话与引用

WeKnora 使用 `POST /api/v1/sessions` 创建会话，同一用例的追问复用该会话。不同用例和重复测试创建新会话，不能将历史助手答案直接注入一个新会话。知识问答走 `/knowledge-chat/:session_id`，关闭 Agent 和联网搜索，避免将外部检索与本轮知识库混评。

适配器识别 `response_type`，只将 `answer` 内容计为正文；`answer.done` 不代表整轮结束，必须收到有效的 `complete`。断流、错误、空回答和输出截断仍失败。原始 SSE、事件时间线、服务端会话 ID、请求正文和 `raw_answer` 均保存。

部分部署会把模型内部推理作为 `<think>...</think>` 包在 `answer` 事件中。适配器保留完整 `raw_answer`，但从用于事实和引用评审的正文中移除这类内部推理，避免把模型草稿误计为用户可见的未引用陈述。

`<kb doc="..." kb_id="..." chunk_id="..."/>` 转换为统一的 `[n]`，只改变引用标记，保留原文和映射。Wiki 链接仅在本轮返回了唯一对应的 Wiki 正文来源时转换；不依据页面标题猜测、不为没有引用的回答追加引用。无法解析的标记保留为执行错误。评审只读取接口返回的引用正文，不额外拉取全文当作生成时证据。报告的证据范围标为 `returned_references`，不宣称掌握服务端全部上下文。

知识库、文档和分块的原始字符串 ID 保留；用于答案展示的引用编号单独分配。来源归属只取实际返回的 `knowledge_base_id`，缺失或越界不能用请求中的知识库 ID 补齐。WeKnora 原始 `parse_status` 也写入文档记录。

`--cleanup keep/delete/delete-on-pass` 同时控制知识库和会话清理；失败时默认保留现场。

## Wiki 与权限对照

Wiki 用例会建立启用 Wiki 索引的独立测试库，创建题集的已发布页面，并等待其可检索。Wiki 生成与索引行为取决于部署版本，这部分需要结合实际实例联调，不能仅凭接口模拟测试判定兼容。

权限对照使用真实的独立凭据：`WEKNORA_READER_API_KEY` 或 `WEKNORA_READER_TOKEN`，可选 `WEKNORA_READER_TENANT_ID`。原凭据作为所有者，先验证私有对照事实可检索，再用无访问权限的读者测试。不会伪造 `x-user-id/x-role`。未提供独立凭据会明确记录初始化失败，相关用例保留在计划分母中；HTTP 拒绝保留真实状态，不伪造为模型拒答或成功完成。首次接入建议先运行上面的通用分类，再核对目标版本的权限契约。

## 兼容基线与验证范围

接口实现依据 [Tencent/WeKnora 的固定源码版本 e0c28bef](https://github.com/Tencent/WeKnora/tree/e0c28bef3562de7d2d78d6c1fa480a045468a40f)，包括官方 API 文档及前端引用标记解析。目标部署版本不由该引用版本代替；不同版本需要真实实例验证。

`tests/test_weknora.py` 通过本地 HTTP/SSE 模拟服务验证完整检索、重复多轮问答、解析轮询与分页、资源清理、错误引用、来源变更、断流与截断、凭据保护和控制台参数。测试不调用真实 WeKnora 或付费模型。

```sh
python3 -m unittest discover -s tests
```
