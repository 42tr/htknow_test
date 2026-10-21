# HTKnow 端到端检索测试 Web 控制台镜像。
# 零第三方依赖：运行时只需要 Python 标准库；语料可烘进镜像实现完全离线测试。
# 请用同目录 build.sh 构建（它会先组装只含必要文件的构建上下文）：
#   ./deploy/e2e-web/build.sh
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    E2E_WEB_PORT=8099 \
    E2E_RUNS_DIR=/data/runs \
    E2E_MAX_CONCURRENT=2 \
    E2E_MAX_HISTORY=50

WORKDIR /app

COPY e2e_web.py e2e_retrieval_test.py e2e_weknora.py e2e_corpus.json e2e_answer_eval.py e2e_judge.py e2e_answer_report.py e2e_answer_cases.json ./
COPY entrypoint.sh /usr/local/bin/e2e-web-entrypoint
# 可选：build.sh 会把本地语料缓存放进 corpus/；为空则镜像不烘语料，
# 首次联网运行时自动从公网镜像下载并做 SHA-256 校验。
COPY corpus/ ./corpus/

RUN chmod +x /usr/local/bin/e2e-web-entrypoint \
    && python3 -m py_compile e2e_web.py e2e_retrieval_test.py e2e_weknora.py e2e_answer_eval.py e2e_judge.py e2e_answer_report.py \
    && mkdir -p /data/runs

# 运行产物（日志 / HTML / JSON 报告 / 历史元数据）持久化在 /data
VOLUME ["/data"]
EXPOSE 8099

ENTRYPOINT ["/usr/local/bin/e2e-web-entrypoint"]
