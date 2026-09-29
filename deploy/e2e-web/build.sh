#!/usr/bin/env bash
# 组装最小构建上下文并构建 htknow-e2e-web 镜像。
# 用法：./build.sh [镜像名:标签]   默认 htknow-e2e-web:latest
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
TAG="${1:-htknow-e2e-web:latest}"
CTX="$(mktemp -d "${TMPDIR:-/tmp}/htknow-e2e-web-ctx.XXXXXX")"
trap 'rm -rf "$CTX"' EXIT

cp "$REPO/scripts/e2e_web.py" "$REPO/scripts/e2e_retrieval_test.py" \
   "$REPO/scripts/e2e_corpus.json" "$CTX/"
cp "$HERE/Dockerfile" "$HERE/entrypoint.sh" "$CTX/"
mkdir -p "$CTX/corpus"

CORPUS=""
for cand in "$REPO/scripts/.corpus" "$REPO/scripts/corpus"; do
  if [ -d "$cand" ] && find "$cand" -maxdepth 1 -type f | grep -q .; then
    CORPUS="$cand"
    break
  fi
done
if [ -n "$CORPUS" ]; then
  find "$CORPUS" -maxdepth 1 -type f -exec cp {} "$CTX/corpus/" \;
  echo "语料已烘入镜像：$CORPUS（$(du -sh "$CTX/corpus" | cut -f1)）"
else
  echo "未找到本地语料缓存：镜像不含语料，首次运行将联网下载（或挂载语料卷）"
fi

docker build -t "$TAG" -f "$CTX/Dockerfile" "$CTX"
echo "构建完成：$TAG"
