#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTKnow 端到端检索测试脚本（零第三方依赖，Python 3.8+）。

测试流程
  1. 预检   探测 HOST:PORT 上的 HTKnow 服务与认证头是否可用
  2. 备料   从公网下载评测语料（内容寻址校验 + 魔数嗅探），并本地生成合成「大海捞针」文档
  3. 建库   POST /api/v1/knowledge/knowledge_base/ 创建一次性知识库
  4. 上传   POST /api/v1/knowledge/files/ 逐个上传，记录上传耗时
  5. 等解析 轮询 GET /api/v1/knowledge/files/{id} 至终态，记录解析耗时
            （客户端墙钟 + 服务端 updated_at-created_at 双口径）
  6. 检索   GET /api/v1/knowledge/search/ 执行黄金问题集，重复采样测延迟
  7. 评估   Hit@K / MRR@K / nDCG@K / Precision@K / Recall@K / 答案在上下文率 / 负样本对照 / 结果稳定性
  8. 报告   输出自包含 HTML（内联 CSS + SVG，无 CDN 依赖）与 JSON

用法
  python3 scripts/e2e_retrieval_test.py --target 192.168.0.46:11000
  python3 scripts/e2e_retrieval_test.py --host 127.0.0.1 --port 3000
  python3 scripts/e2e_retrieval_test.py --target 127.0.0.1:3000 --docs art_of_war,needle_synthetic
  python3 scripts/e2e_retrieval_test.py --target 127.0.0.1:3000 --dry-run

退出码  0=全部门禁达标  1=存在未达标项  2=脚本或环境错误
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_mod
import json
import math
import os
import random
import re
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CORPUS = SCRIPT_DIR / "e2e_corpus.json"
API_ROOT = "/api/v1/knowledge"

FILE_STATUS = {0: "待处理", 1: "已完成", 2: "处理中", 3: "已跳过", -1: "解析失败"}
TERMINAL_STATUS = {1, 3, -1}
TEXT_FORMATS = {"txt", "md", "markdown", "csv", "json", "log"}
MAGIC = {"pdf": (b"%PDF",), "docx": (b"PK\x03\x04",), "doc": (b"\xd0\xcf\x11\xe0",),
         "pptx": (b"PK\x03\x04",), "xlsx": (b"PK\x03\x04",)}

EXIT_OK, EXIT_GATE_FAILED, EXIT_ERROR = 0, 1, 2


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
class HarnessError(Exception):
    """环境或调用层面的致命错误（区别于被测服务的业务失败）。"""


class ApiFailure(Exception):
    def __init__(self, method: str, path: str, status: int, detail: str):
        self.method, self.path, self.status, self.detail = method, path, status, detail
        super().__init__(f"{method} {path} -> HTTP {status}: {detail}")


def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def human_bytes(n: Optional[float]) -> str:
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def human_ms(ms: Optional[float]) -> str:
    if ms is None:
        return "-"
    if ms < 1000:
        return f"{ms:.0f} ms"
    if ms < 60000:
        return f"{ms / 1000:.2f} s"
    return f"{int(ms // 60000)}m{(ms % 60000) / 1000:.0f}s"


def percentile(values: Sequence[float], p: float) -> Optional[float]:
    """线性插值百分位，与 numpy.percentile(method='linear') 一致。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    k = (len(ordered) - 1) * (p / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return float(ordered[int(k)])
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo))


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def norm_text(value: str) -> str:
    """检索关键词匹配用：小写化并压缩 PDF 解析常见的空格丢失/连字符换行。"""
    lowered = (value or "").lower()
    return re.sub(r"\s+", "", lowered)


def contains_keyword(haystack: str, needle: str) -> bool:
    """宽松包含判定：先原文匹配，再去空格匹配（兼容 MinerU 抽取时丢空格的版面）。"""
    if not needle:
        return True
    if needle.lower() in (haystack or "").lower():
        return True
    return norm_text(needle) in norm_text(haystack)


def snippet(text: str, limit: int = 220) -> str:
    flat = re.sub(r"\s+", " ", (text or "").strip())
    return flat if len(flat) <= limit else flat[:limit] + "…"


# --------------------------------------------------------------------------- #
# HTTP 客户端（标准库实现，含 multipart 编码与重试）
# --------------------------------------------------------------------------- #
def encode_multipart(fields: Sequence[Tuple[str, str]], filename: str,
                     file_bytes: bytes, file_field: str = "file",
                     content_type: str = "application/octet-stream") -> Tuple[str, bytes]:
    boundary = "----htke2e" + uuid.uuid4().hex
    crlf = b"\r\n"
    parts: List[bytes] = []
    for name, value in fields:
        parts += [
            f"--{boundary}".encode(),
            f'Content-Disposition: form-data; name="{name}"'.encode("utf-8"),
            b"", str(value).encode("utf-8"),
        ]
    # filename 按 RFC 7578 §4.2 用 UTF-8 原样传：服务端 multer 以严格 UTF-8 解析 quoted-string，
    # 早先把非 ASCII 替换成 `?` 会让中文文件名根本测不到。仅把会破坏头部结构的字符换成下划线。
    safe_name = "".join("_" if (ch in '"\\\r\n' or ord(ch) < 0x20) else ch for ch in filename) or "unnamed"
    parts += [
        f"--{boundary}".encode(),
        f'Content-Disposition: form-data; name="{file_field}"; filename="{safe_name}"'.encode("utf-8"),
        f"Content-Type: {content_type}".encode(),
        b"", file_bytes,
    ]
    parts += [f"--{boundary}--".encode(), b""]
    return f"multipart/form-data; boundary={boundary}", crlf.join(parts)


class Client:
    """HTKnow REST 客户端。所有请求都带上认证头并返回耗时（毫秒）。"""

    def __init__(self, base_url: str, user_id: str, role: str, user_name: str,
                 timeout: float, retries: int, insecure: bool, verbose: bool = True):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = max(0, retries)
        self.verbose = verbose
        self.headers = {"x-user-id": user_id, "x-role": role}
        if user_name:
            self.headers["x-user-name"] = user_name
        self._ctx = None
        if insecure and self.base_url.startswith("https"):
            import ssl
            self._ctx = ssl.create_default_context()
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE
        self.request_count = 0
        self.total_ms = 0.0

    def log(self, message: str) -> None:
        if self.verbose:
            print(message, flush=True)

    def _open(self, request: urllib.request.Request) -> Tuple[int, bytes, float]:
        started = time.perf_counter()
        response = self.open(request, self.timeout)
        try:
            status = getattr(response, "status", None) or response.getcode()
            payload = response.read()
        finally:
            response.close()
        return int(status), payload, (time.perf_counter() - started) * 1000.0

    def open(self, request: urllib.request.Request, timeout: float):
        opener = getattr(self, "_opener", None)
        return (opener.open(request, timeout=timeout) if opener else
                urllib.request.urlopen(request, timeout=timeout, context=self._ctx))

    def redact(self, value: str) -> str:
        for name, secret in self.headers.items():
            if name.lower() in ("x-api-key", "authorization") and secret:
                value = value.replace(secret, "[REDACTED]")
                if secret.startswith("Bearer "):
                    value = value.replace(secret[7:], "[REDACTED]")
        return value

    def request(self, method: str, path: str, *, query: Optional[Dict[str, Any]] = None,
                json_body: Any = None, body: Optional[bytes] = None,
                extra_headers: Optional[Dict[str, str]] = None,
                retryable: bool = True) -> Tuple[Any, float]:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        headers = dict(self.headers)
        headers.setdefault("Accept", "application/json")
        if extra_headers:
            headers.update(extra_headers)
        payload: Optional[bytes] = body
        if json_body is not None:
            payload = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            self.request_count += 1
            request = urllib.request.Request(url, data=payload, headers=headers, method=method)
            try:
                status, raw, elapsed = self._open(request)
                self.total_ms += elapsed
                decoded = self._decode(raw)
                if status >= 400:
                    raise ApiFailure(method, path, status, str(decoded)[:400])
                return decoded, elapsed
            except urllib.error.HTTPError as exc:  # 4xx/5xx 走这里
                elapsed = 0.0
                try:
                    raw = exc.read()
                    decoded = self._decode(raw)
                    detail = self.redact(str(decoded))[:400]
                except Exception:
                    detail = str(exc.reason)
                self.total_ms += elapsed
                if retryable and exc.code in (408, 429, 500, 502, 503, 504) and attempt < self.retries:
                    last_error = ApiFailure(method, path, exc.code, detail)
                    time.sleep(min(2 ** attempt * 0.5, 5))
                    continue
                raise ApiFailure(method, path, exc.code, detail) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                if retryable and attempt < self.retries:
                    last_error = exc
                    time.sleep(min(2 ** attempt * 0.5, 5))
                    continue
                raise ApiFailure(method, path, 0, f"连接失败: {reason}") from None
        raise ApiFailure(method, path, 0, f"重试耗尽: {last_error}")

    @staticmethod
    def _decode(raw: bytes) -> Any:
        if not raw:
            return None
        text = raw.decode("utf-8", "replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text[:800]

    # ---- 业务封装 ----
    def get(self, path: str, query: Optional[Dict[str, Any]] = None) -> Tuple[Any, float]:
        return self.request("GET", path, query=query)

    def post_json(self, path: str, body: Any) -> Tuple[Any, float]:
        return self.request("POST", path, json_body=body)

    def delete(self, path: str) -> Tuple[Any, float]:
        return self.request("DELETE", path, retryable=False)

    def upload(self, path: str, fields: Sequence[Tuple[str, str]], filename: str,
               file_bytes: bytes, content_type: str) -> Tuple[Any, float]:
        ctype, body = encode_multipart(fields, filename, file_bytes, content_type=content_type)
        return self.request("POST", path, body=body, extra_headers={"Content-Type": ctype},
                            retryable=False)

    def download(self, url: str, dest: Path, timeout: float) -> Tuple[int, float]:
        """下载语料到本地缓存，返回 (字节数, 毫秒)。"""
        request = urllib.request.Request(url, headers={"User-Agent": "htknow-e2e/1.0"})
        started = time.perf_counter()
        response = urllib.request.urlopen(request, timeout=timeout, context=self._ctx)
        total = 0
        try:
            with dest.open("wb") as handle:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    total += len(chunk)
        finally:
            response.close()
        return total, (time.perf_counter() - started) * 1000.0


# --------------------------------------------------------------------------- #
# 语料与黄金问题集
# --------------------------------------------------------------------------- #
@dataclass
class QuerySpec:
    id: str
    doc_id: str
    query: str
    intent: str = ""
    keywords_all: List[str] = field(default_factory=list)
    keywords_any: List[str] = field(default_factory=list)
    expect_rank_max: Optional[int] = None
    grade: int = 2


@dataclass
class NegativeSpec:
    id: str
    doc_id: str
    query: str
    forbidden: List[str] = field(default_factory=list)
    intent: str = "负样本对照：不应召回到未注入内容"


@dataclass
class DocSpec:
    id: str
    filename: str
    fmt: str
    language: str = ""
    description: str = ""
    license: str = ""
    homepage: str = ""
    optional: bool = False
    slice_type: Optional[str] = None
    sources: List[Dict[str, Any]] = field(default_factory=list)
    queries: List[QuerySpec] = field(default_factory=list)
    negatives: List[NegativeSpec] = field(default_factory=list)
    generator: Optional[str] = None
    generator_params: Dict[str, Any] = field(default_factory=dict)


FILLER_EN = [
    "The retrieval pipeline indexes every slice with both a sparse lexical representation and a dense vector.",
    "Operators should verify that the tokenizer, the embedding service and the reranker agree on the same text.",
    "A knowledge base stores documents, slices, embeddings and optional wiki pages behind one permission scope.",
    "Latency budgets are usually dominated by the embedding call, not by the inverted index lookup itself.",
    "Regression suites keep a golden set of questions whose expected evidence passages never change.",
]
FILLER_ZH = [
    "检索链路会同时为每个切片建立稀疏关键词索引与稠密向量索引，两路结果融合后再交给重排序模型。",
    "运维同学需要确认分词器、向量服务与重排序服务处理的是同一份文本，避免出现切片与向量错配。",
    "一个知识库包含文档、切片、向量以及可选的 Wiki 页面，并统一受权限范围约束。",
    "端到端延迟通常由向量服务的推理耗时主导，而不是倒排索引查询本身。",
    "回归测试会维护一组黄金问题，其期望证据段落保持长期稳定，用于衡量召回质量的漂移。",
]


def build_needle_document(spec: DocSpec, workdir: Path, run_id: str) -> DocSpec:
    """生成确定性「大海捞针」文档，并就地补全其黄金问题与负样本。

    每段埋入一个全局唯一 token；负样本使用同一命名空间但从未写入文档的 token，
    用来检验检索链路不会凭空召回不存在的内容（假阳性对照）。
    """
    params = spec.generator_params
    paragraphs = int(params.get("paragraphs", 64))
    query_count = int(params.get("queries", 6))
    negative_count = int(params.get("negatives", 3))
    prefix = str(params.get("token_prefix", "HTKN"))
    seed = int(params.get("seed", 20260928))
    rng = random.Random(f"{seed}:{run_id}")
    alphabet = string.ascii_uppercase + string.digits

    def make_token() -> str:
        return prefix + "".join(rng.choice(alphabet) for _ in range(8))

    used: Dict[str, int] = {}
    while len(used) < paragraphs:
        used[make_token()] = 0
    tokens = list(used)
    for index, token in enumerate(tokens):
        used[token] = index + 1

    unused: List[str] = []
    while len(unused) < negative_count:
        candidate = make_token()
        if candidate not in used and candidate not in unused:
            unused.append(candidate)

    blocks = [
        "HTKnow 端到端检索自检文档 / End-to-end retrieval self-check document.",
        f"运行标识 run id: {run_id}。本文档由测试脚本本地生成，不依赖任何外部解析质量。",
        f"全文共 {paragraphs} 节，每节埋入一个全局唯一标识符，用于验证 上传 → 切片 → 索引 → 检索 链路。",
    ]
    for index, token in enumerate(tokens, start=1):
        zh = FILLER_ZH[(index - 1) % len(FILLER_ZH)]
        en = FILLER_EN[index % len(FILLER_EN)]
        blocks.append(
            f"第 {index} 节 Section {index}\n"
            f"{zh} 本节的唯一标识符是 {token} ，记录编号 {index:04d}。\n"
            f"{en} The unique marker of this section is {token} (record {index:04d})."
        )
    blocks.append(f"文档结束。共写入 {paragraphs} 个唯一标识符，运行标识 {run_id}。")
    content = "\n\n".join(blocks) + "\n"

    path = workdir / spec.filename
    path.write_text(content, encoding="utf-8")

    picked = sorted(rng.sample(range(paragraphs), min(query_count, paragraphs)))
    spec.queries = []
    for slot, position in enumerate(picked, start=1):
        token = tokens[position]
        section_no = position + 1
        spec.queries.append(QuerySpec(
            id=f"{spec.id}-{slot:02d}", doc_id=spec.id,
            query=token if slot % 2 else f"哪一节的唯一标识符是 {token} ",
            intent=f"合成大海捞针：定位第 {section_no} 节（token={token}）",
            keywords_all=[token],
            expect_rank_max=3, grade=2,
        ))
    spec.negatives = [NegativeSpec(
        id=f"{spec.id}-neg-{slot:02d}", doc_id=spec.id, query=token,
        forbidden=[token], intent="负样本对照：该标识符从未写入语料，不应被召回",
    ) for slot, token in enumerate(unused, start=1)]
    spec.description = (spec.description +
                        f"（本次生成 {paragraphs} 节 / {len(spec.queries)} 条正样本 / {len(spec.negatives)} 条负样本）")
    return spec


def load_corpus(path: Path, docs_filter: Optional[Sequence[str]],
                exclude: Optional[Sequence[str]]) -> Tuple[List[DocSpec], Dict[str, Any]]:
    if not path.exists():
        raise HarnessError(f"语料清单不存在: {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    defaults = manifest.get("defaults", {}) or {}
    specs: List[DocSpec] = []
    for raw in manifest.get("documents", []):
        queries = [QuerySpec(
            id=q.get("id") or f"{raw['id']}-{i:02d}",
            doc_id=raw["id"],
            query=q["query"],
            intent=q.get("intent", ""),
            keywords_all=list(q.get("keywords_all") or []),
            keywords_any=list(q.get("keywords_any") or []),
            expect_rank_max=q.get("expect_rank_max"),
            grade=int(q.get("grade", defaults.get("grade", 2))),
        ) for i, q in enumerate(raw.get("queries", []), start=1)]
        negatives = [NegativeSpec(
            id=n.get("id") or f"{raw['id']}-neg-{i:02d}",
            doc_id=raw["id"], query=n["query"],
            forbidden=list(n.get("forbidden") or n.get("keywords_all") or []),
            intent=n.get("intent", "负样本对照"),
        ) for i, n in enumerate(raw.get("negatives", []), start=1)]
        specs.append(DocSpec(
            id=raw["id"], filename=raw["filename"], fmt=raw.get("format", "bin"),
            language=raw.get("language", ""), description=raw.get("description", ""),
            license=raw.get("license", ""), homepage=raw.get("homepage", ""),
            optional=bool(raw.get("optional", False)),
            slice_type=raw.get("slice_type") or defaults.get("slice_type"),
            sources=list(raw.get("sources", [])), queries=queries, negatives=negatives,
            generator=raw.get("generator"), generator_params=dict(raw.get(raw.get("generator") or "", {}) or {}),
        ))

    wanted = {d.strip() for d in docs_filter if d.strip()} if docs_filter else None
    dropped = {d.strip() for d in exclude if d.strip()} if exclude else set()
    unknown = ((wanted or set()) | dropped) - {s.id for s in specs}
    if unknown:
        raise HarnessError(f"--docs/--exclude 中存在未知文档 id: {', '.join(sorted(unknown))}；"
                           f"可选: {', '.join(s.id for s in specs)}")
    if wanted:
        specs = [s for s in specs if s.id in wanted]
    if dropped:
        specs = [s for s in specs if s.id not in dropped]
    if not specs:
        raise HarnessError("筛选后没有可测试的文档")
    return specs, manifest


def sniff_format(fmt: str, head: bytes) -> Optional[str]:
    """校验下载内容确实是指定格式，避免把 HTML 错误页当成 PDF 上传。"""
    expected = MAGIC.get(fmt)
    if expected:
        if not head.startswith(tuple(expected)):
            return f"魔数不匹配，期望 {fmt} 实际开头为 {head[:16]!r}"
        return None
    if fmt in TEXT_FORMATS:
        low = head[:4096].lower()
        if b"<html" in low or b"<!doctype html" in low:
            return "内容疑似 HTML 错误页而非纯文本"
        try:
            head.decode("utf-8")
        except UnicodeDecodeError:
            return "纯文本声明但无法按 UTF-8 解码"
    return None


def prepare_document(spec: DocSpec, cache_dir: Path, workdir: Path, client: Client,
                     download_timeout: float, strict: bool, offline: bool,
                     unique_marker: Optional[str]) -> Dict[str, Any]:
    """把语料准备到 workdir，返回来源与完整性信息。"""
    info: Dict[str, Any] = {"stage": "pending", "source_url": None, "integrity": "-",
                            "download_ms": None, "size_bytes": 0, "sha256": None, "notes": []}

    if spec.generator == "needle":
        build_needle_document(spec, workdir, unique_marker or "local")
        path = workdir / spec.filename
        info.update(stage="generated", source_url="本地生成", integrity="local",
                    size_bytes=path.stat().st_size, sha256=sha256_of(path), local_path=str(path))
        return info

    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / spec.filename
    pinned = [s.get("sha256") for s in spec.sources if s.get("sha256")]

    chosen: Optional[Path] = None
    if cached.exists() and cached.stat().st_size > 0:
        digest = sha256_of(cached)
        if not pinned or digest in pinned:
            chosen = cached
            info.update(source_url=f"本地缓存 {cached}", integrity="cached-verified", sha256=digest,
                        size_bytes=cached.stat().st_size)
            info["notes"].append("命中本地缓存，跳过下载（--refresh-corpus 可强制重新下载）")
        else:
            info["notes"].append(f"本地缓存校验失败（{digest[:12]}…），将重新下载")

    if chosen is None and offline:
        info.update(stage="download_failed",
                    error="--offline 模式且本地无可用缓存，无法获取语料")
        return info

    if chosen is None:
        last_error = None
        for source in spec.sources:
            url = source.get("url")
            if not url:
                continue
            tmp = cached.with_suffix(cached.suffix + ".part")
            try:
                size, elapsed = client.download(url, tmp, download_timeout)
            except Exception as exc:  # noqa: BLE001 - 逐个镜像回退
                last_error = f"{url} 下载失败: {exc}"
                info["notes"].append(f"镜像回退：{last_error}")
                if tmp.exists():
                    tmp.unlink()
                continue
            if size <= 0:
                last_error = f"{url} 返回空内容"
                info["notes"].append(f"镜像回退：{last_error}")
                tmp.unlink(missing_ok=True)
                continue
            head = tmp.open("rb").read(4096)
            problem = sniff_format(spec.fmt, head)
            if problem:
                last_error = f"{url} 内容校验失败: {problem}"
                info["notes"].append(f"镜像回退：{last_error}")
                tmp.unlink(missing_ok=True)
                continue
            digest = sha256_of(tmp)
            expected = source.get("sha256")
            if expected and digest != expected:
                if strict:
                    tmp.unlink(missing_ok=True)
                    last_error = f"{url} SHA-256 不匹配: 期望 {expected[:12]}… 实际 {digest[:12]}…"
                    continue
                info["integrity"] = "hash-mismatch"
                info["notes"].append(f"⚠ SHA-256 与清单不一致（上游可能已更新版本），仍继续使用。实际 {digest[:16]}…")
            else:
                info["integrity"] = "verified" if expected else "unverified"
                if not expected:
                    info["notes"].append("该镜像未固定 SHA-256，仅做格式嗅探校验")
            tmp.replace(cached)
            chosen = cached
            info.update(source_url=url, sha256=digest, size_bytes=size, download_ms=elapsed)
            break
        if chosen is None:
            info.update(stage="download_failed", error=last_error or "所有镜像均不可用")
            return info

    local = workdir / spec.filename
    data = chosen.read_bytes()
    if unique_marker and spec.fmt in TEXT_FORMATS:
        # 追加唯一标记，改变文件哈希，避免服务端 reuse_duplicate_files 复用历史解析结果而失真测时。
        data += f"\n\n[htknow-e2e-run:{unique_marker}]\n".encode("utf-8")
        info["notes"].append("已追加运行唯一标记以规避重复文件解析复用（仅文本类语料）")
    local.write_bytes(data)
    info.update(stage="ready", size_bytes=len(data), local_path=str(local))
    if info.get("sha256") is None:
        info["sha256"] = sha256_of(local)
    return info
# --------------------------------------------------------------------------- #
# 运行结果模型
# --------------------------------------------------------------------------- #
@dataclass
class DocResult:
    doc_id: str
    filename: str
    fmt: str
    language: str
    optional: bool
    description: str = ""
    license: str = ""
    homepage: str = ""
    stage: str = "pending"          # ready/generated/download_failed/upload_failed/parse_failed/parse_timeout/completed/skipped
    source_url: Optional[str] = None
    integrity: str = "-"
    notes: List[str] = field(default_factory=list)
    sha256: Optional[str] = None
    size_bytes: int = 0
    local_path: Optional[str] = None
    kb_file_id: Any = None
    status: Optional[int] = None
    backend_status: Optional[str] = None
    processing_status: Optional[int] = None
    wiki_status: Optional[str] = None
    server_log: str = ""
    slices: int = 0
    slice_chars: int = 0
    download_ms: Optional[float] = None
    upload_ms: Optional[float] = None
    parse_ms: Optional[float] = None
    server_parse_s: Optional[int] = None
    polls: int = 0
    reuse_detected: bool = False
    error: Optional[str] = None
    started_at: str = ""
    finished_at: str = ""


@dataclass
class Hit:
    rank: int
    slice_id: Any
    file_id: Any
    filename: str
    score: float
    content: str          # 完整切片正文，仅用于关键词判定，写入报告前会被替换为 preview
    doc_match: bool
    kw_match: bool
    is_wiki: bool
    preview: str = ""     # 报告中展示的截断正文


@dataclass
class QueryResult:
    query_id: str
    doc_id: str
    query: str
    intent: str
    keywords_all: List[str]
    keywords_any: List[str]
    expect_rank_max: Optional[int]
    grade: int
    expected_file_ids: List[int] = field(default_factory=list)
    skipped_reason: Optional[str] = None
    error: Optional[str] = None
    latencies_ms: List[float] = field(default_factory=list)
    returned_counts: List[int] = field(default_factory=list)
    stable: bool = True
    hits: List[Hit] = field(default_factory=list)
    # 指标
    hit: bool = False
    first_rank: Optional[int] = None
    rr: float = 0.0
    precision: float = 0.0
    recall: float = 0.0
    ndcg: float = 0.0
    rank_ok: Optional[bool] = None
    kw_matched: List[str] = field(default_factory=list)
    kw_missing: List[str] = field(default_factory=list)
    kw_any_matched: bool = True
    kw_rate: float = 0.0
    answer_in_context: bool = False


@dataclass
class NegativeResult:
    query_id: str
    doc_id: str
    query: str
    forbidden: List[str]
    intent: str
    skipped_reason: Optional[str] = None
    error: Optional[str] = None
    latencies_ms: List[float] = field(default_factory=list)
    passed: bool = False
    leaked: List[str] = field(default_factory=list)
    returned: int = 0
    hits: List[Hit] = field(default_factory=list)


@dataclass
class GateResult:
    name: str
    metric: str
    actual: Optional[float]
    threshold: Optional[float]
    comparator: str      # ">=" or "<="
    unit: str
    passed: bool
    critical: bool = True
    note: str = ""


# --------------------------------------------------------------------------- #
# 被测服务调用
# --------------------------------------------------------------------------- #
def make_client(args: argparse.Namespace, base_url: str, *, reader: bool = False) -> Client:
    client = Client(base_url, args.user_id, args.role, args.user_name,
                    args.http_timeout, args.retries, args.insecure, verbose=not args.quiet)
    if getattr(args, "backend", "htknow") == "weknora":
        from e2e_weknora import WeKnoraBackend
        client.backend = WeKnoraBackend(client, args, sys.modules[__name__], reader=reader)
    return client


def file_record(client: Client, file_id: Any):
    if getattr(client, "backend", None):
        return client.backend.file_record(file_id)
    return client.get(f"{API_ROOT}/files/{file_id}")


def delete_knowledge_base(client: Client, kb_id: Any):
    if getattr(client, "backend", None):
        return client.backend.delete_kb(kb_id)
    return client.delete(f"{API_ROOT}/knowledge_base/{kb_id}")


def create_wiki_page(client: Client, kb_id: Any, page: Dict[str, Any]):
    if getattr(client, "backend", None):
        return client.backend.create_wiki_page(kb_id, page)
    return client.post_json(f"{API_ROOT}/wiki/page", dict(page, kb_id=kb_id, status="published", page_type="concept"))


def preflight(client: Client) -> Dict[str, Any]:
    if getattr(client, "backend", None):
        return client.backend.preflight()
    env: Dict[str, Any] = {"base_url": client.base_url, "reachable": False,
                           "auth_ok": False, "openapi_title": None, "openapi_version": None,
                           "kb_list_ms": None, "existing_kbs": None}
    try:
        payload, elapsed = client.get(f"{API_ROOT}/knowledge_base/", {"page": 1, "size": 1})
        env.update(reachable=True, auth_ok=True, kb_list_ms=elapsed)
        if isinstance(payload, dict):
            env["existing_kbs"] = payload.get("total")
    except ApiFailure as exc:
        if exc.status in (401, 403):
            env["reachable"] = True
            raise HarnessError(f"认证失败（HTTP {exc.status}）。请检查 --user-id / --role：{exc.detail}") from None
        if exc.status == 0:
            raise HarnessError(f"无法连接 {client.base_url}：{exc.detail}") from None
        raise HarnessError(f"预检失败 HTTP {exc.status}：{exc.detail}") from None
    try:
        spec, _ = client.get("/api-docs/openapi.json")
        if isinstance(spec, dict):
            info = spec.get("info") or {}
            env["openapi_title"] = info.get("title")
            env["openapi_version"] = info.get("version")
    except Exception:  # noqa: BLE001 - OpenAPI 文档缺失不影响测试
        pass
    return env


def create_knowledge_base(client: Client, name: str, description: str,
                          parse_priority: int, is_public: bool, *, wiki: bool = False) -> Tuple[Any, float]:
    if getattr(client, "backend", None):
        if is_public:
            raise HarnessError("WeKnora 测试库使用真实空间权限，不支持 HTKnow 的 --public 参数")
        return client.backend.create_kb(name, description, wiki=wiki)
    payload, elapsed = client.post_json(f"{API_ROOT}/knowledge_base/", {
        "name": name, "description": description,
        "kb_type": "analysis", "is_public": is_public, "parse_priority": parse_priority,
    })
    if not isinstance(payload, dict) or "id" not in payload:
        raise HarnessError(f"创建知识库返回异常: {str(payload)[:300]}")
    return int(payload["id"]), elapsed


def upload_document(client: Client, kb_id: int, path: Path, slice_type: str,
                    tags: Sequence[str]) -> Tuple[int, float]:
    if getattr(client, "backend", None):
        return client.backend.upload(kb_id, path)
    ctype = {"pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             "doc": "application/msword", "xlsx": "application/vnd.openxmlformats-ms-excel",
             "md": "text/markdown"}.get(path.suffix.lstrip(".").lower(), "text/plain")
    fields = [("kb_id", str(kb_id)), ("slice_type", slice_type),
              ("immediate_parse", "true"), ("is_public", "false"),
              ("tags", json.dumps(list(tags), ensure_ascii=False))]
    payload, elapsed = client.upload(f"{API_ROOT}/files/", fields, path.name,
                                     path.read_bytes(), ctype)
    items = payload if isinstance(payload, list) else [payload]
    if not items or not isinstance(items[0], dict) or "id" not in items[0]:
        raise HarnessError(f"上传返回异常: {str(payload)[:300]}")
    return int(items[0]["id"]), elapsed


def wait_for_parse(client: Client, file_id: int, timeout: float, interval: float,
                   progress: bool = True) -> Tuple[Dict[str, Any], float, int]:
    """轮询文件状态直到终态。返回 (文件记录, 墙钟毫秒, 轮询次数)。"""
    started = time.perf_counter()
    polls = 0
    record: Dict[str, Any] = {}
    last_status: Any = None
    while True:
        payload, _ = file_record(client, file_id)
        polls += 1
        if isinstance(payload, dict):
            record = payload
        status = record.get("status")
        if status != last_status and progress:
            print(f"    · file#{file_id} 状态 {last_status} → {status}"
                  f"（{FILE_STATUS.get(status, status)}）", flush=True)
            last_status = status
        if status in TERMINAL_STATUS:
            return record, (time.perf_counter() - started) * 1000.0, polls
        if (time.perf_counter() - started) >= timeout:
            return record, (time.perf_counter() - started) * 1000.0, polls
        time.sleep(interval)


def fetch_slices(client: Client, file_id: int) -> Tuple[int, int]:
    if getattr(client, "backend", None):
        return client.backend.fetch_slices(file_id)
    payload, _ = client.get(f"{API_ROOT}/files/{file_id}/slices")
    if not isinstance(payload, list):
        return 0, 0
    chars = sum(len(str(item.get("content") or "")) for item in payload if isinstance(item, dict))
    return len(payload), chars


def run_search(client: Client, query: str, kb_id: int,
               file_ids: Optional[Sequence[int]] = None) -> Tuple[List[Dict[str, Any]], float]:
    if getattr(client, "backend", None):
        return client.backend.search(query, kb_id, file_ids)
    params: Dict[str, Any] = {"query": query, "kb_id": kb_id}
    if file_ids:
        params["file_id"] = ",".join(str(i) for i in file_ids)
    payload, elapsed = client.get(f"{API_ROOT}/search/", params)
    results = payload.get("results") if isinstance(payload, dict) else None
    return (results if isinstance(results, list) else []), elapsed


def normalize_hits(raw_results: Sequence[Dict[str, Any]], limit: int,
                   expected_file_ids: Sequence[int], keywords: Sequence[str]) -> List[Hit]:
    expected = set(expected_file_ids)
    hits: List[Hit] = []
    for rank, item in enumerate(raw_results[:limit], start=1):
        if not isinstance(item, dict):
            continue
        wiki = item.get("wiki")
        file_info = item.get("file") or {}
        file_id = item.get("file_id")
        if not file_id and isinstance(file_info, dict):
            file_id = file_info.get("id")
        content = str(item.get("content") or item.get("image_content") or "")
        if isinstance(wiki, dict):
            page = wiki.get("page") or {}
            content = content or str(page.get("content") or page.get("summary") or "")
        hits.append(Hit(
            rank=rank, slice_id=item.get("id") or 0,
            file_id=file_id or None,
            filename=str((file_info or {}).get("filename") or
                         ((wiki or {}).get("page") or {}).get("title") or
                         ("wiki" if wiki else "-")),
            score=float(item.get("score") or 0.0), content=content,
            preview=snippet(content, 600),
            doc_match=bool(expected) and file_id in expected,
            kw_match=all(contains_keyword(content, k) for k in keywords) if keywords else False,
            is_wiki=bool(wiki),
        ))
    return hits
# --------------------------------------------------------------------------- #
# 指标计算（标准信息检索评测口径）
# --------------------------------------------------------------------------- #
def evaluate_query(result: QueryResult, hits: Sequence[Hit], eval_k: int) -> None:
    result.hits = list(hits[:eval_k])
    expected = set(result.expected_file_ids)
    relevant_ranks = [h.rank for h in result.hits if h.doc_match]
    result.hit = bool(relevant_ranks)
    result.first_rank = relevant_ranks[0] if relevant_ranks else None
    result.rr = (1.0 / result.first_rank) if result.first_rank else 0.0
    result.precision = len(relevant_ranks) / eval_k if eval_k else 0.0
    found_docs = {h.file_id for h in result.hits if h.doc_match and h.file_id is not None}
    result.recall = (len(found_docs) / len(expected)) if expected else 0.0

    best_gain = result.grade if result.keywords_all else max(1, result.grade - 1)
    partial_gain = max(1, result.grade - 1)
    # qrels 是文档级，而命中是切片级：同一文档可能贡献多条切片。
    # 计算 DCG 时按 file_id 折叠（取该文档最好的排名与最高的增益），
    # 否则 DCG 会随切片数线性放大，超过按相关文档数构造的 IDCG，得到 >1 的 nDCG。
    doc_best_rank: Dict[int, int] = {}
    doc_best_gain: Dict[int, float] = {}
    for hit in result.hits:
        if not hit.doc_match or hit.file_id is None:
            continue
        gain = best_gain if (hit.kw_match and result.keywords_all) else partial_gain
        doc_best_rank[hit.file_id] = min(doc_best_rank.get(hit.file_id, hit.rank), hit.rank)
        doc_best_gain[hit.file_id] = max(doc_best_gain.get(hit.file_id, gain), gain)
    dcg = sum(gain / math.log2(doc_best_rank[doc_id] + 1) for doc_id, gain in doc_best_gain.items())
    ideal_count = max(1, min(eval_k, len(expected)))
    idcg = sum(best_gain / math.log2(i + 2) for i in range(ideal_count))
    result.ndcg = min(1.0, dcg / idcg) if idcg > 0 else 0.0

    if result.expect_rank_max is not None:
        result.rank_ok = bool(result.first_rank and result.first_rank <= result.expect_rank_max)

    # 关键词必须落在同一条切片里：拼接 Top-K 正文后再匹配，会让跨切片边界的词
    # 因为 norm_text 去掉空白而「假命中」，把答案不在上下文的情况误判为通过。
    def keyword_in_any_slice(keyword: str) -> bool:
        return any(contains_keyword(h.content, keyword) for h in result.hits)

    result.kw_matched = [k for k in result.keywords_all if keyword_in_any_slice(k)]
    result.kw_missing = [k for k in result.keywords_all if k not in result.kw_matched]
    result.kw_rate = (len(result.kw_matched) / len(result.keywords_all)) if result.keywords_all else 1.0
    result.kw_any_matched = (not result.keywords_any) or any(keyword_in_any_slice(k) for k in result.keywords_any)
    result.answer_in_context = (not result.kw_missing) and result.kw_any_matched


def aggregate(values: Iterable[float]) -> Dict[str, Optional[float]]:
    data = [float(v) for v in values]
    if not data:
        return {"n": 0, "mean": None, "min": None, "p50": None, "p90": None,
                "p95": None, "p99": None, "max": None, "stdev": None}
    mean = sum(data) / len(data)
    variance = sum((x - mean) ** 2 for x in data) / len(data) if len(data) > 1 else 0.0
    return {"n": len(data), "mean": mean, "min": min(data),
            "p50": percentile(data, 50), "p90": percentile(data, 90),
            "p95": percentile(data, 95), "p99": percentile(data, 99),
            "max": max(data), "stdev": math.sqrt(variance)}


def build_gates(args: argparse.Namespace, metrics: Dict[str, Any],
                parse_stats: Dict[str, Any]) -> List[GateResult]:
    gates: List[GateResult] = []

    def add(name: str, metric: str, actual: Optional[float], threshold: Optional[float],
            comparator: str, unit: str, critical: bool = True, note: str = "") -> None:
        if threshold is None:
            gates.append(GateResult(name, metric, actual, None, comparator, unit,
                                    True, False, note or "未设置阈值，不参与判定"))
            return
        if actual is None:
            gates.append(GateResult(name, metric, None, threshold, comparator, unit,
                                    False, critical, note or "缺少实测数据，关键门禁未通过"))
            return
        passed = actual >= threshold if comparator == ">=" else actual <= threshold
        gates.append(GateResult(name, metric, actual, threshold, comparator, unit, passed, critical, note))

    add("解析成功率", "parse_success_rate", parse_stats.get("success_rate"),
        args.min_parse_success if parse_stats.get("evaluated_required", 0) else None,
        ">=", "%", args.min_parse_success > 0,
        f"必测文档成功 {parse_stats.get('parsed_ok_required', 0)} / "
        f"{parse_stats.get('evaluated_required', 0)}（optional 文档不计入）")
    add("检索问题执行覆盖率", "query_coverage", metrics.get("query_coverage"), 100.0, ">=", "%",
        note="下载、解析、检索请求失败不能从必测问题中剔除")
    add(f"Hit@{metrics['eval_k']}", "hit_rate", metrics.get("hit_rate"), args.min_hit_rate, ">=", "%")
    add(f"MRR@{metrics['eval_k']}", "mrr", metrics.get("mrr"), args.min_mrr, ">=", "")
    add(f"nDCG@{metrics['eval_k']}", "ndcg", metrics.get("ndcg"), args.min_ndcg, ">=", "")
    add(f"答案在上下文率@{metrics['eval_k']}", "answer_in_context", metrics.get("answer_in_context"),
        args.min_answer_in_context, ">=", "%")
    add("关键词覆盖率", "keyword_coverage", metrics.get("keyword_coverage"), args.min_keyword_coverage,
        ">=", "%", False)
    add("负样本对照通过率", "negative_pass_rate", metrics.get("negative_pass_rate"),
        100.0 if metrics.get("negative_total", 0) else None, ">=", "%",
        args.require_negatives, "假阳性检验：未注入内容不应被召回")
    add("检索 P95 延迟", "search_p95_ms", (metrics.get("latency") or {}).get("p95"),
        args.max_p95_ms or None, "<=", "ms", args.max_p95_ms > 0)
    add("单文件解析 P95 耗时", "parse_p95_ms", (parse_stats.get("latency") or {}).get("p95"),
        args.max_parse_ms or None, "<=", "ms", args.max_parse_ms > 0)
    return gates
# --------------------------------------------------------------------------- #
# 报告渲染（自包含 HTML + 内联 SVG）
# --------------------------------------------------------------------------- #
def esc(value: Any) -> str:
    return html_mod.escape("" if value is None else str(value), quote=True)


def fmt_num(value: Optional[float], digits: int = 1, suffix: str = "") -> str:
    return "-" if value is None else f"{value:.{digits}f}{suffix}"


def badge(ok: Optional[bool], text_ok: str = "通过", text_bad: str = "未通过",
          text_na: str = "无数据") -> str:
    if ok is None:
        return f'<span class="badge na">{esc(text_na)}</span>'
    return (f'<span class="badge ok">{esc(text_ok)}</span>' if ok
            else f'<span class="badge bad">{esc(text_bad)}</span>')


def status_badge(stage: str) -> str:
    mapping = {
        "completed": ("ok", "解析完成"), "skipped": ("warn", "服务端跳过"),
        "parse_failed": ("bad", "解析失败"), "parse_timeout": ("bad", "解析超时"),
        "upload_failed": ("bad", "上传失败"), "download_failed": ("bad", "语料获取失败"),
        "ready": ("na", "已就绪"), "generated": ("na", "本地生成"),
    }
    cls, label = mapping.get(stage, ("na", stage))
    return f'<span class="badge {cls}">{esc(label)}</span>'


def _clip_label(label: str, limit: int = 42) -> str:
    return label if len(label) <= limit else label[: limit - 1] + "…"


def svg_hbar(rows: Sequence[Tuple[str, Optional[float], str]], unit: str = "ms",
             width: int = 900, bar_h: int = 22) -> str:
    """水平条形图：rows = [(label, value, css_class)]"""
    valid = [v for _, v, _ in rows if v is not None]
    if not valid:
        return '<p class="muted">无可用数据</p>'
    peak = max(valid) or 1.0
    longest = max(len(_clip_label(l)) for l, _, _ in rows)
    label_w = min(330, max(180, longest * 6.6 + 12))
    pad, gap = 96, 6
    height = len(rows) * (bar_h + gap) + 34
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" '
           f'aria-label="耗时条形图">']
    for index, (label, value, cls) in enumerate(rows):
        top = index * (bar_h + gap) + 22
        out.append(f'<text x="0" y="{top + bar_h - 6}" class="lbl">'
                   f'<title>{esc(label)}</title>{esc(_clip_label(label))}</text>')
        if value is None:
            out.append(f'<text x="{label_w + 8}" y="{top + bar_h - 6}" class="val muted-txt">未测得</text>')
            continue
        bar_w = max(2.0, (width - label_w - pad) * (value / peak))
        out.append(f'<rect x="{label_w}" y="{top}" width="{bar_w:.1f}" height="{bar_h}" '
                   f'rx="3" class="bar {cls}"><title>{esc(label)}: '
                   f'{esc(human_ms(value) if unit == "ms" else fmt_num(value))}</title></rect>')
        out.append(f'<text x="{label_w + bar_w + 8:.1f}" y="{top + bar_h - 6}" class="val">'
                   f'{human_ms(value) if unit == "ms" else fmt_num(value)}</text>')
    out.append(f'<line x1="{label_w}" y1="16" x2="{label_w}" y2="{height - 6}" class="axis"/>')
    out.append(f'<text x="{label_w}" y="12" class="tick">0</text>')
    out.append(f'<text x="{width - pad}" y="12" class="tick" text-anchor="end">'
               f'{human_ms(peak) if unit == "ms" else fmt_num(peak)}（满刻度）</text>')
    out.append("</svg>")
    return "".join(out)


def svg_latency(samples: Sequence[float], marks: Dict[str, Optional[float]],
                width: int = 900, height: int = 240) -> str:
    """排序后的延迟分布折线图，叠加 P50/P95/P99 参考线。"""
    data = sorted(float(s) for s in samples)
    if not data:
        return '<p class="muted">无可用数据</p>'
    pad_l, pad_r, pad_t, pad_b = 62, 16, 30, 34
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    peak = max(data[-1], max([v for v in marks.values() if v] or [0])) or 1.0
    peak *= 1.08
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="检索延迟分布">']
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        y = pad_t + plot_h * (1 - frac)
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" class="grid"/>')
        out.append(f'<text x="{pad_l - 8}" y="{y + 4:.1f}" class="tick" text-anchor="end">'
                   f'{peak * frac:.0f}</text>')
    out.append(f'<text x="{pad_l - 8}" y="14" class="tick" text-anchor="end">ms</text>')

    step = plot_w / max(1, len(data) - 1) if len(data) > 1 else 0
    pts = " ".join(f"{pad_l + i * step:.1f},{pad_t + plot_h * (1 - v / peak):.1f}"
                   for i, v in enumerate(data)) if len(data) > 1 else \
          f"{pad_l},{pad_t + plot_h * (1 - data[0] / peak):.1f}"
    if len(data) > 1:
        out.append(f'<polyline points="{pts}" class="line"/>')
    else:
        out.append(f'<circle cx="{pad_l}" cy="{pad_t + plot_h * (1 - data[0] / peak):.1f}" r="4" class="dot"/>')
    out.append(f'<line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{width - pad_r}" '
               f'y2="{pad_t + plot_h}" class="axis"/>')
    out.append(f'<text x="{pad_l}" y="{height - 10}" class="tick">第 1 次请求</text>')
    out.append(f'<text x="{width - pad_r}" y="{height - 10}" class="tick" text-anchor="end">'
               f'第 {len(data)} 次请求（按耗时升序）</text>')
    palette = {"P50": "#2563eb", "P95": "#d97706", "P99": "#dc2626"}
    for index, (name, value) in enumerate(marks.items()):
        if not value:
            continue
        y = pad_t + plot_h * (1 - value / peak)
        color = palette.get(name, "#64748b")
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
                   f'class="mark" stroke="{color}"/>')
        out.append(f'<text x="{width - pad_r - 4}" y="{y - 5:.1f}" class="tick" '
                   f'text-anchor="end" fill="{color}">{esc(name)} {human_ms(value)}</text>')
    out.append("</svg>")
    return "".join(out)


def metric_bars(items: Sequence[Tuple[str, Optional[float], str, Optional[float]]]) -> str:
    """KPI 进度条：items = [(label, percent_value_0_100, css, threshold)]"""
    out = ['<div class="bars">']
    for label, value, cls, threshold in items:
        width = 0 if value is None else max(0.0, min(100.0, float(value)))
        th = "" if threshold is None else \
            f'<span class="th" style="left:calc({max(0.0, min(100.0, threshold)):.1f}% )"></span>'
        out.append(
            f'<div class="barrow"><div class="barlabel">{esc(label)}</div>'
            f'<div class="bartrack"><div class="barfill {cls}" style="width:{width:.1f}%"></div>{th}</div>'
            f'<div class="barvalue">{"-" if value is None else f"{value:.1f}"}</div></div>')
    out.append("</div>")
    return "".join(out)
CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#0f172a;--muted:#64748b;--line:#e2e8f0;
--ok:#15803d;--okbg:#dcfce7;--bad:#b91c1c;--badbg:#fee2e2;--warn:#b45309;--warnbg:#fef3c7;
--na:#475569;--nabg:#e2e8f0;--accent:#2563eb}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB",
"Microsoft YaHei",Helvetica,Arial,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 64px}
header.top{background:linear-gradient(135deg,#0f172a,#1e3a8a);color:#fff;border-radius:14px;
padding:24px 26px;margin-bottom:22px;box-shadow:0 6px 24px rgba(15,23,42,.16)}
header.top h1{margin:0 0 6px;font-size:22px;letter-spacing:.3px}
header.top .sub{opacity:.82;font-size:13px}
header.top .meta{margin-top:14px;display:flex;flex-wrap:wrap;gap:8px}
header.top .chip{background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.22);
padding:3px 10px;border-radius:999px;font-size:12px}
.verdict{float:right;text-align:center;min-width:132px;padding:10px 14px;border-radius:12px;
background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.25)}
.verdict .big{font-size:26px;font-weight:700;line-height:1.2}
.verdict .small{font-size:12px;opacity:.85}
.verdict.pass .big{color:#86efac}.verdict.fail .big{color:#fca5a5}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:18px 20px;margin-bottom:18px;box-shadow:0 1px 2px rgba(15,23,42,.04)}
h2{margin:0 0 4px;font-size:16px}
h2 .hint{font-weight:400;color:var(--muted);font-size:12px;margin-left:8px}
h3{margin:20px 0 8px;font-size:14px;color:#334155}
p.desc{margin:2px 0 14px;color:var(--muted);font-size:12.5px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:#fcfdff}
.card .k{color:var(--muted);font-size:12px;margin-bottom:4px}
.card .v{font-size:21px;font-weight:700;letter-spacing:.2px}
.card .u{font-size:12px;color:var(--muted);margin-left:3px;font-weight:400}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{background:#f8fafc;color:#475569;font-weight:600;font-size:12px;
position:sticky;top:0;z-index:1}
tbody tr:nth-child(even){background:#fbfcfe}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.c,th.c{text-align:center}
.badge{display:inline-block;padding:1px 9px;border-radius:999px;font-size:11.5px;
font-weight:600;white-space:nowrap;border:1px solid transparent}
.badge.ok{color:var(--ok);background:var(--okbg);border-color:#bbf7d0}
.badge.bad{color:var(--bad);background:var(--badbg);border-color:#fecaca}
.badge.warn{color:var(--warn);background:var(--warnbg);border-color:#fde68a}
.badge.na{color:var(--na);background:var(--nabg);border-color:#cbd5e1}
.muted{color:var(--muted);font-size:12.5px}
.muted-txt{fill:var(--muted);font-size:11px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
.chart{width:100%;height:auto;margin:8px 0 4px;overflow:visible}
.chart .lbl{font-size:11.5px;fill:#334155}
.chart .val{font-size:11.5px;fill:#0f172a;font-variant-numeric:tabular-nums}
.chart .tick{font-size:10.5px;fill:var(--muted)}
.chart .grid{stroke:#eef2f7;stroke-width:1}
.chart .axis{stroke:#cbd5e1;stroke-width:1}
.chart .line{fill:none;stroke:var(--accent);stroke-width:2}
.chart .dot{fill:var(--accent)}
.chart .mark{stroke-dasharray:5 4;stroke-width:1.4;opacity:.85}
.bar.a{fill:#3b82f6}.bar.b{fill:#8b5cf6}.bar.c{fill:#14b8a6}
.bar.bad{fill:#ef4444}.bar.warn{fill:#f59e0b}.bar.skip{fill:#cbd5e1}
.bars{margin-top:10px}
.barrow{display:grid;grid-template-columns:190px 1fr 62px;gap:10px;align-items:center;margin:7px 0}
.barlabel{font-size:12.5px;color:#334155}
.bartrack{position:relative;height:14px;background:#eef2f7;border-radius:7px;overflow:hidden}
.barfill{height:100%;border-radius:7px}
.barfill.ok{background:linear-gradient(90deg,#22c55e,#16a34a)}
.barfill.warn{background:linear-gradient(90deg,#f59e0b,#d97706)}
.barfill.bad{background:linear-gradient(90deg,#f87171,#dc2626)}
.barvalue{font-size:12.5px;text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
.th{position:absolute;top:-2px;bottom:-2px;width:2px;background:#0f172a;opacity:.55}
details{border:1px solid var(--line);border-radius:9px;margin:8px 0;background:#fcfdff}
details>summary{cursor:pointer;padding:9px 12px;font-size:13px;list-style:none;
display:flex;flex-wrap:wrap;gap:8px;align-items:center}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:"▸";color:var(--muted);font-size:11px}
details[open]>summary::before{content:"▾"}
details[open]>summary{border-bottom:1px solid var(--line);background:#f8fafc}
.qid{font-family:ui-monospace,monospace;font-size:11.5px;color:var(--muted)}
.qtext{font-weight:600}
.detail-body{padding:10px 12px}
.hitrow{border-top:1px dashed var(--line);padding:8px 0}
.hitrow:first-child{border-top:none}
.hitmeta{font-size:11.5px;color:var(--muted);margin-bottom:3px;display:flex;gap:10px;flex-wrap:wrap}
.hitcontent{font-size:12.5px;background:#f8fafc;border-left:3px solid #cbd5e1;
padding:6px 9px;border-radius:0 6px 6px 0;white-space:pre-wrap;word-break:break-word}
.hitcontent.match{border-left-color:#22c55e;background:#f0fdf4}
.callout{border-radius:9px;padding:10px 13px;margin:10px 0;font-size:13px;border:1px solid}
.callout.bad{background:var(--badbg);border-color:#fecaca;color:#7f1d1d}
.callout.warn{background:var(--warnbg);border-color:#fde68a;color:#78350f}
.callout.ok{background:var(--okbg);border-color:#bbf7d0;color:#14532d}
.callout.info{background:#eff6ff;border-color:#bfdbfe;color:#1e3a8a}
ul.tight{margin:6px 0 0;padding-left:20px}ul.tight li{margin:3px 0}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:26px}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:4px 22px}
.kv div{display:flex;justify-content:space-between;gap:12px;padding:5px 0;
border-bottom:1px dashed var(--line);font-size:12.5px}
.kv span:first-child{color:var(--muted)}
.kv span:last-child{font-family:ui-monospace,monospace;text-align:right;word-break:break-all}
@media print{body{background:#fff}section{box-shadow:none;break-inside:avoid}
header.top{background:#0f172a !important;-webkit-print-color-adjust:exact}}
"""


def render_html(report: Dict[str, Any]) -> str:
    if report["config"].get("mode") == "chat":
        from e2e_answer_eval import render_report
        return render_report(report, CSS)
    cfg, metrics = report["config"], report["metrics"]
    parse, gate_rows = report["parse"], report["gates"]
    queries: List[Dict[str, Any]] = report["queries"]
    negatives: List[Dict[str, Any]] = report["negatives"]
    docs: List[Dict[str, Any]] = report["documents"]
    overall = report["overall_pass"]
    eval_k = metrics["eval_k"]
    backend_label = "WeKnora" if cfg.get("backend") == "weknora" else "HTKnow"

    out: List[str] = [
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
        f"<title>{backend_label} 端到端检索测试报告 · {esc(cfg['run_id'])}</title>",
        f"<style>{CSS}</style></head><body><div class=\"wrap\">",
    ]

    # ---- 头部 ----
    verdict_cls = "pass" if overall else "fail"
    verdict_txt = "PASS" if overall else "FAIL"
    failed_gates = [g for g in gate_rows
                    if g["critical"] and not g["passed"]
                    and g["threshold"] is not None]
    out.append(f"""
<header class="top"><div class="verdict {verdict_cls}"><div class="big">{verdict_txt}</div>
<div class="small">{len(failed_gates)} 项关键门禁未达标</div></div>
<h1>{backend_label} 端到端检索测试报告</h1>
<div class="sub">建库 → 上传 → 解析 → 检索 → 指标评估 全链路自动化验收</div>
<div class="meta">
<span class="chip">目标 {esc(cfg['target'])}</span>
<span class="chip">知识库 #{esc(cfg['kb_id'])} {esc(cfg['kb_name'])}</span>
<span class="chip">运行 {esc(cfg['run_id'])}</span>
<span class="chip">开始 {esc(cfg['started_at'])}</span>
<span class="chip">总耗时 {esc(human_ms(cfg['total_ms']))}</span>
<span class="chip">Top-K = {esc(eval_k)}</span>
</div></header>""")

    # ---- 概览卡片 ----
    lat = metrics.get("latency") or {}
    cards = [
        ("文档总数", len(docs), "个"),
        ("解析成功", parse.get("parsed_ok", 0),
         f"/ {parse.get('evaluated', 0)}（必测 {parse.get('parsed_ok_required', 0)}/"
         f"{parse.get('evaluated_required', 0)}）"),
        ("切片总数", parse.get("total_slices", 0), "条"),
        ("黄金问题", metrics.get("total_queries", 0), "条"),
        (f"Hit@{eval_k}", fmt_num(metrics.get("hit_rate"), 1), "%"),
        (f"MRR@{eval_k}", fmt_num(metrics.get("mrr"), 3), ""),
        (f"nDCG@{eval_k}", fmt_num(metrics.get("ndcg"), 3), ""),
        ("答案在上下文率", fmt_num(metrics.get("answer_in_context"), 1), "%"),
        ("检索 P95", fmt_num(lat.get("p95"), 0), "ms"),
        ("解析 P95", fmt_num((parse.get("latency") or {}).get("p95"), 0), "ms"),
    ]
    out.append('<section><h2>结果概览</h2><p class="desc">'
               '解析成功率按全部必测文档统计，检索质量按有效问题统计，并单独检查执行覆盖率；'
               '答案评估保留所有计划用例，单列语义评审覆盖率。</p><div class="cards">')
    for label, value, unit in cards:
        out.append(f'<div class="card"><div class="k">{esc(label)}</div>'
                   f'<div class="v">{esc(value)}<span class="u">{esc(unit)}</span></div></div>')
    out.append("</div>")

    if report.get("warnings"):
        out.append('<div class="callout warn"><strong>运行告警</strong><ul class="tight">')
        out += [f"<li>{esc(w)}</li>" for w in report["warnings"]]
        out.append("</ul></div>")
    out.append("</section>")

    # ---- 门禁 ----
    out.append('<section><h2>验收门禁<span class="hint">阈值可通过命令行调整</span></h2>'
               '<table><thead><tr><th>门禁项</th><th>指标</th><th class="num">实测</th>'
               '<th class="c">要求</th><th class="num">阈值</th><th class="c">关键</th>'
               '<th class="c">结果</th><th>备注</th></tr></thead><tbody>')
    for gate in gate_rows:
        actual = gate["actual"]
        if gate["unit"] == "%":
            shown = fmt_num(actual, 1, "%")
            threshold = fmt_num(gate["threshold"], 1, "%")
        elif gate["unit"] == "ms":
            shown = human_ms(actual)
            threshold = human_ms(gate["threshold"])
        else:
            shown = fmt_num(actual, 3)
            threshold = fmt_num(gate["threshold"], 3)
        critical = "是" if gate["critical"] else "否"
        if gate["threshold"] is None:
            state_cell = '<span class="badge na">未启用</span>'
            threshold = "-"
        elif actual is None:
            state_cell = '<span class="badge bad">无数据，未通过</span>' if gate["critical"] else '<span class="badge na">无数据</span>'
        else:
            state_cell = badge(gate["passed"])
        out.append(f'<tr><td>{esc(gate["name"])}</td><td class="mono">{esc(gate["metric"])}</td>'
                   f'<td class="num">{esc(shown)}</td><td class="c mono">{esc(gate["comparator"])}</td>'
                   f'<td class="num">{esc(threshold)}</td><td class="c muted">{esc(critical)}</td>'
                   f'<td class="c">{state_cell}</td><td class="muted">{esc(gate["note"])}</td></tr>')
    out.append("</tbody></table>")
    if failed_gates:
        out.append('<div class="callout bad"><strong>未达标项：</strong>'
                   + esc("；".join(f'{g["name"]}（实测 {fmt_num(g["actual"], 2)} {g["unit"]}）'
                                    for g in failed_gates)) + "</div>")
    out.append("</section>")

    # ---- 环境 ----
    env = report["environment"]
    out.append('<section><h2>被测环境与运行配置</h2><div class="kv">')
    for key, value in [
        ("被测系统", backend_label), ("服务地址", cfg["target"]),
        ("API 根路径", "/api/v1" if cfg.get("backend") == "weknora" else API_ROOT),
        ("认证方式", "服务端 API Key / JWT" if cfg.get("backend") == "weknora" else "x-user-id / x-role"),
        ("HTKnow 用户 ID", cfg["user_id"] if cfg.get("backend", "htknow") == "htknow" else "不适用"),
        ("OpenAPI 标题", env.get("openapi_title") or "-"),
        ("OpenAPI 版本", env.get("openapi_version") or "-"),
        ("知识库列表接口耗时", human_ms(env.get("kb_list_ms"))),
        ("运行前已有知识库数", env.get("existing_kbs") if env.get("existing_kbs") is not None else "-"),
        ("切片方式", cfg["slice_type"]), ("评测 Top-K（请求值）", cfg["top_k"]),
        ("评测 Top-K（实际生效）", eval_k), ("每条问题重复次数", cfg["repeats"]),
        ("解析等待超时", human_ms(cfg["parse_timeout"] * 1000)),
        ("轮询间隔", f'{cfg["poll_interval"]} s'),
        ("HTTP 超时", f'{cfg["http_timeout"]} s'), ("HTTP 重试", cfg["retries"]),
        ("总 HTTP 请求数", cfg["request_count"]),
        ("语料清单", cfg["corpus_file"]), ("测试开始", cfg["started_at"]),
        ("测试结束", cfg["finished_at"]),
    ]:
        out.append(f"<div><span>{esc(key)}</span><span>{esc(value)}</span></div>")
    out.append("</div>")
    if cfg.get("kb_disposition"):
        out.append(f'<p class="muted" style="margin-top:10px">知识库处置：{esc(cfg["kb_disposition"])}'
                   f'</p>')
    out.append("</section>")

    # ---- 文件解析 ----
    out.append('<section><h2>文件解析<span class="hint">上传耗时与解析耗时分开计量</span></h2>'
               '<p class="desc">解析耗时 = 客户端从「上传返回」到「文件进入终态」的墙钟时间，'
               '包含服务端后台队列等待；「服务端口径」取文件记录的 updated_at − created_at。</p>'
               '<table><thead><tr><th>文档</th><th class="c">格式</th><th class="c">语言</th>'
               '<th class="num">大小</th><th class="c">状态</th><th class="num">切片数</th>'
               '<th class="num">切片字符</th><th class="num">上传耗时</th><th class="num">解析耗时</th>'
               '<th class="num">服务端口径</th><th class="num">吞吐</th><th>来源/完整性</th>'
               '</tr></thead><tbody>')
    for doc in docs:
        throughput = "-"
        if doc.get("parse_ms") and doc.get("size_bytes"):
            throughput = f"{doc['size_bytes'] / 1024 / (doc['parse_ms'] / 1000):.1f} KB/s"
        integrity = doc.get("integrity", "-")
        integrity_badge = {"verified": "ok", "cached-verified": "ok", "local": "na",
                           "unverified": "warn", "hash-mismatch": "bad"}.get(integrity, "na")
        server_parse = doc.get("server_parse_s")
        server_cell = "-" if server_parse is None else f"{server_parse} s"
        reuse_cell = '<span class="badge warn">复用历史解析</span>' if doc.get("reuse_detected") else ""
        sha_cell = (doc.get("sha256") or "-")[:16]
        out.append(
            f'<tr><td><div><strong>{esc(doc["filename"])}</strong></div>'
            f'<div class="qid">{esc(doc["doc_id"])} · file#{esc(doc.get("kb_file_id"))}</div>'
            f'<div class="muted">{esc(doc.get("description", ""))}</div></td>'
            f'<td class="c mono">{esc(doc["fmt"])}</td>'
            f'<td class="c mono">{esc(doc.get("language") or "-")}</td>'
            f'<td class="num">{esc(human_bytes(doc.get("size_bytes")))}</td>'
            f'<td class="c">{status_badge(doc["stage"])} {reuse_cell}</td>'
            f'<td class="num">{esc(doc.get("slices") or 0)}</td>'
            f'<td class="num">{esc(doc.get("slice_chars") or 0)}</td>'
            f'<td class="num">{esc(human_ms(doc.get("upload_ms")))}</td>'
            f'<td class="num">{esc(human_ms(doc.get("parse_ms")))}</td>'
            f'<td class="num">{esc(server_cell)}</td>'
            f'<td class="num">{esc(throughput)}</td>'
            f'<td class="muted"><span class="badge {integrity_badge}">{esc(integrity)}</span>'
            f'<div class="mono" style="margin-top:3px;word-break:break-all">'
            f'{esc(doc.get("source_url") or "-")}</div>'
            f'<div class="mono" style="opacity:.7">sha256:{esc(sha_cell)}…</div></td></tr>')
    out.append("</tbody></table>")

    parse_rows = [(f'{d["filename"]}', d.get("parse_ms"),
                   {"completed": "a", "skipped": "skip"}.get(d["stage"], "bad"))
                  for d in docs if d.get("parse_ms") is not None]
    if parse_rows:
        out.append("<h3>解析耗时对比</h3>")
        out.append(svg_hbar(parse_rows))
    upload_rows = [(d["filename"], d.get("upload_ms"), "c") for d in docs
                   if d.get("upload_ms") is not None]
    if upload_rows:
        out.append("<h3>上传耗时对比</h3>")
        out.append(svg_hbar(upload_rows))

    failures = [d for d in docs if d.get("error") or d["stage"] in
                ("parse_failed", "parse_timeout", "upload_failed", "download_failed")]
    if failures:
        out.append("<h3>失败与异常诊断</h3>")
        for doc in failures:
            out.append(f'<details open><summary><span class="badge bad">{esc(doc["stage"])}</span>'
                       f'<span class="qtext">{esc(doc["filename"])}</span>'
                       f'<span class="qid">file#{esc(doc.get("kb_file_id"))}</span></summary>'
                       f'<div class="detail-body">')
            if doc.get("error"):
                out.append(f'<div class="callout bad">{esc(doc["error"])}</div>')
            if doc.get("server_log"):
                out.append(f'<div class="hitmeta">服务端 log 字段</div>'
                           f'<div class="hitcontent">{esc(doc["server_log"])}</div>')
            for note in doc.get("notes", []):
                out.append(f'<div class="muted">· {esc(note)}</div>')
            out.append("</div></details>")
    out.append("</section>")

    # ---- 检索质量 ----
    per_doc = metrics.get("per_document") or {}
    out.append('<section><h2>检索质量指标<span class="hint">标准信息检索评测口径</span></h2>'
               f'<p class="desc">以文档为相关性单元（每条黄金问题绑定其证据所在文档），'
               f'在 Top-{eval_k} 上计算。「答案在上下文率」进一步要求期望关键词出现在召回切片正文中，'
               f'等价于 RAG 场景的 Answer-in-Context。</p>')
    out.append(metric_bars([
        (f"Hit@{eval_k}（命中率 %）", metrics.get("hit_rate"), "ok", cfg["min_hit_rate"]),
        (f"MRR@{eval_k}（×100）", None if metrics.get("mrr") is None else metrics["mrr"] * 100,
         "ok", None if cfg["min_mrr"] is None else cfg["min_mrr"] * 100),
        (f"nDCG@{eval_k}（×100）", None if metrics.get("ndcg") is None else metrics["ndcg"] * 100,
         "ok", None if cfg["min_ndcg"] is None else cfg["min_ndcg"] * 100),
        (f"答案在上下文率@{eval_k}（%）", metrics.get("answer_in_context"), "ok",
         cfg["min_answer_in_context"]),
        ("关键词覆盖率（%）", metrics.get("keyword_coverage"), "warn", cfg["min_keyword_coverage"]),
        (f"Precision@{eval_k}（%）", metrics.get("precision"), "warn", None),
        (f"Recall@{eval_k}（%）", metrics.get("recall"), "warn", None),
        ("结果稳定性（%）", metrics.get("stability"), "warn", None),
        ("负样本对照通过率（%）", metrics.get("negative_pass_rate"), "ok", 100.0),
    ]))
    out.append('<table><thead><tr><th>口径</th><th class="num">全量</th>')
    for doc_id in per_doc:
        out.append(f'<th class="num">{esc(doc_id)}</th>')
    out.append("</tr></thead><tbody>")
    metric_rows = [
        (f"Hit@{eval_k} (%)", "hit_rate", 1), (f"MRR@{eval_k}", "mrr", 3),
        (f"nDCG@{eval_k}", "ndcg", 3), (f"Precision@{eval_k} (%)", "precision", 1),
        (f"Recall@{eval_k} (%)", "recall", 1), ("答案在上下文率 (%)", "answer_in_context", 1),
        ("关键词覆盖率 (%)", "keyword_coverage", 1), ("结果稳定性 (%)", "stability", 1),
        ("样本数", "total_queries", 0),
    ]
    for label, key, digits in metric_rows:
        out.append(f'<tr><td>{esc(label)}</td>'
                   f'<td class="num"><strong>{esc(fmt_num(metrics.get(key), digits))}</strong></td>')
        for doc_id, stats in per_doc.items():
            out.append(f'<td class="num">{esc(fmt_num(stats.get(key), digits))}</td>')
        out.append("</tr>")
    out.append("</tbody></table>")
    out.append('<p class="muted">口径说明：① 黄金问题集为「单证据文档」设计，故 Recall@K 与 Hit@K 数值一致，'
               '如需区分请在语料清单中为同一问题声明多个证据文档。'
               '② nDCG 采用文档级 qrels 展开到切片级：同一文档的多个切片均视为相关，'
               'IDCG 按「理想排序应容纳的相关切片数」构造，因此完美 rank=1 也可能小于 1，'
               '它衡量的是全部相关切片的整体排位质量，而非首命中位置（首命中请看 MRR）。'
               '③「答案在上下文率」要求 keywords_all 全部出现且 keywords_any 至少命中一个，'
               '匹配时兼容 PDF 解析常见的空格丢失（去空格再比对一次）。</p></section>')

    # ---- 延迟 ----
    out.append('<section><h2>耗时统计<span class="hint">解析与检索分别计量</span></h2>')
    lat_rows = [("检索请求", metrics.get("latency") or {}),
                ("文件解析（墙钟）", parse.get("latency") or {}),
                ("文件上传", parse.get("upload_latency") or {})]
    out.append('<table><thead><tr><th>阶段</th><th class="num">样本</th><th class="num">均值</th>'
               '<th class="num">最小</th><th class="num">P50</th><th class="num">P90</th>'
               '<th class="num">P95</th><th class="num">P99</th><th class="num">最大</th>'
               '<th class="num">标准差</th></tr></thead><tbody>')
    for label, stats in lat_rows:
        cells = "".join(f'<td class="num">{esc(human_ms(stats.get(k)))}</td>'
                        for k in ("mean", "min", "p50", "p90", "p95", "p99", "max", "stdev"))
        out.append(f'<tr><td>{esc(label)}</td><td class="num">{esc(stats.get("n", 0))}</td>{cells}</tr>')
    out.append("</tbody></table>")
    samples = report.get("search_samples") or []
    if samples:
        out.append("<h3>检索延迟分布（全部请求按耗时升序）</h3>")
        out.append(svg_latency(samples, {"P50": (metrics.get("latency") or {}).get("p50"),
                                         "P95": (metrics.get("latency") or {}).get("p95"),
                                         "P99": (metrics.get("latency") or {}).get("p99")}))
    qps = metrics.get("throughput_qps")
    if qps:
        out.append(f'<p class="muted">串行检索吞吐约 {qps:.2f} QPS（不含思考间隔，单客户端）。</p>')
    out.append("</section>")

    # ---- 逐条问题 ----
    out.append('<section><h2>黄金问题集逐条明细<span class="hint">点击展开查看 Top-K 命中</span></h2>')
    for query in queries:
        if query.get("skipped_reason"):
            head = (f'<span class="badge na">跳过</span>'
                    f'<span class="qid">{esc(query["query_id"])}</span>'
                    f'<span class="qtext">{esc(query["query"])}</span>'
                    f'<span class="muted">{esc(query["skipped_reason"])}</span>')
            out.append(f"<details><summary>{head}</summary><div class=\"detail-body\">"
                       f'<p class="muted">{esc(query["skipped_reason"])}</p></div></details>')
            continue
        state = query["hit"] and query["answer_in_context"]
        if query.get("error"):
            state = False
        latency_cell = human_ms(percentile(query["latencies_ms"], 50)) if query["latencies_ms"] else "-"
        head = (
            f'{badge(state, "命中", "未命中")}'
            f'<span class="qid">{esc(query["query_id"])} · {esc(query["doc_id"])}</span>'
            f'<span class="qtext">{esc(query["query"])}</span>'
            f'<span class="badge na">rank {esc(query["first_rank"] or "-")}</span>'
            f'<span class="badge na">nDCG {fmt_num(query["ndcg"], 2)}</span>'
            f'<span class="badge na">关键词 {esc(len(query["kw_matched"]))}/{esc(len(query["keywords_all"]))}</span>'
            f'<span class="badge na">P50 {esc(latency_cell)}</span>')
        out.append(f'<details{" open" if query.get("error") or not state else ""}>'
                   f'<summary>{head}</summary><div class="detail-body">')
        if query.get("intent"):
            out.append(f'<div class="muted">意图：{esc(query["intent"])}</div>')
        if query.get("error"):
            out.append(f'<div class="callout bad">{esc(query["error"])}</div>')
        out.append('<div class="hitmeta">'
                   f'<span>期望文档 file_id: {esc(query["expected_file_ids"] or "-")}</span>'
                   f'<span>返回条数: {esc(query["returned_counts"] or "-")}</span>'
                   f'<span>重复采样一致性: {"稳定" if query["stable"] else "不稳定"}</span>'
                   f'<span>延迟样本(ms): {esc(", ".join(f"{v:.0f}" for v in query["latencies_ms"]) or "-")}</span>'
                   f'<span>期望最大排名: {esc(query["expect_rank_max"] or "不限")}</span>'
                   "</div>")
        if query["keywords_all"]:
            missing = query["kw_missing"]
            if missing:
                out.append(f'<div class="callout warn">未召回的期望关键词：{esc("、".join(missing))}</div>')
            if not query["kw_any_matched"]:
                out.append('<div class="callout warn">keywords_any 中的候选词一个都没出现在 Top-K 正文里</div>')
        if not query["hits"]:
            out.append('<div class="callout bad">检索返回 0 条结果</div>')
        for hit in query["hits"]:
            cls = "hitcontent match" if (hit["doc_match"] and hit["kw_match"]) else "hitcontent"
            score_cell = f"{hit['score']:.4f}"
            flags = []
            if hit["doc_match"]:
                flags.append('<span class="badge ok">目标文档</span>')
            if hit["kw_match"]:
                flags.append('<span class="badge ok">含期望关键词</span>')
            if hit["is_wiki"]:
                flags.append('<span class="badge na">Wiki</span>')
            out.append(
                f'<div class="hitrow"><div class="hitmeta">'
                f'<span>#{esc(hit["rank"])}</span><span>slice {esc(hit["slice_id"])}</span>'
                f'<span>file {esc(hit["file_id"])}</span><span>{esc(hit["filename"])}</span>'
                f'<span>score {esc(score_cell)}</span>'
                f'{"".join(flags)}</div>'
                f'<div class="{cls}">{esc(snippet(hit["content"], 420))}</div></div>')
        out.append("</div></details>")
    out.append("</section>")

    # ---- 负样本 ----
    out.append('<section><h2>负样本对照<span class="hint">假阳性检验</span></h2>'
               '<p class="desc">使用与语料同命名空间但从未写入任何文档的标识符发起检索，'
               '断言 Top-K 正文中不出现该标识符。用于验证检索链路不会凭空召回不存在的内容。</p>'
               '<table><thead><tr><th>用例</th><th>查询</th><th>禁止出现的词</th>'
               '<th class="num">返回条数</th><th class="num">P50 延迟</th>'
               '<th class="c">结果</th></tr></thead><tbody>')
    for negative in negatives:
        if negative.get("skipped_reason"):
            out.append(f'<tr><td class="qid">{esc(negative["query_id"])}</td>'
                       f'<td class="mono">{esc(negative["query"])}</td>'
                       f'<td class="mono">{esc("、".join(negative["forbidden"]))}</td>'
                       f'<td class="num">-</td><td class="num">-</td>'
                       f'<td class="c"><span class="badge na">跳过</span></td></tr>')
            continue
        median = percentile(negative["latencies_ms"], 50)
        leaked = negative["leaked"]
        out.append(f'<tr><td class="qid">{esc(negative["query_id"])}</td>'
                   f'<td class="mono">{esc(negative["query"])}</td>'
                   f'<td class="mono">{esc("、".join(negative["forbidden"]))}</td>'
                   f'<td class="num">{esc(negative["returned"])}</td>'
                   f'<td class="num">{esc(human_ms(median))}</td>'
                   f'<td class="c">{badge(negative["passed"], "无泄漏", "泄漏: " + "、".join(leaked))}</td></tr>')
    out.append("</tbody></table></section>")

    if report.get("answers"):
        from e2e_answer_eval import render_section
        out.append(render_section(report["answers"]))

    # ---- 结论 ----
    out.append('<section><h2>结论与排查建议</h2>')
    if overall:
        out.append('<div class="callout ok"><strong>全部关键门禁达标。</strong>'
                   '建库、上传、解析、索引与检索链路在本次运行中表现符合验收阈值。</div>')
    else:
        out.append('<div class="callout bad"><strong>存在未达标项。</strong>建议按下列顺序排查。</div>')
    hints = report.get("hints") or []
    if hints:
        out.append("<ul class=\"tight\">")
        out += [f"<li>{esc(h)}</li>" for h in hints]
        out.append("</ul>")
    out.append("</section>")

    out.append(f'<footer>由 <span class="mono">scripts/e2e_retrieval_test.py</span> 生成 · '
               f'run {esc(cfg["run_id"])} · {esc(cfg["finished_at"])} · '
               f'共发出 {esc(cfg["request_count"])} 次 HTTP 请求</footer>')
    out.append("</div></body></html>")
    result = "".join(out)
    if cfg.get("mode") == "both":
        result = result.replace("端到端检索测试报告", "检索与答案测试报告")
    return result
# --------------------------------------------------------------------------- #
# 诊断建议
# --------------------------------------------------------------------------- #
def build_hints(docs: Sequence[Dict[str, Any]], metrics: Dict[str, Any],
                parse: Dict[str, Any], warnings: Sequence[str]) -> List[str]:
    hints: List[str] = []
    logs = " ".join(str(d.get("server_log") or "") for d in docs).lower()
    if "embedding" in logs and ("connect" in logs or "timeout" in logs or "refused" in logs):
        hints.append("解析失败原因指向 Embedding 服务：检查 HTKNOW_EMBEDDING_URL / HTKNOW_EMBEDDING_KEY，"
                     "长文档可调大 HTKNOW_EMBEDDING_BATCH_TIMEOUT_SECS 与 HTKNOW_EMBEDDING_BATCH_MAX_CHARS。")
    if "mineru" in logs or "file_parse" in logs:
        hints.append("PDF 解析失败指向 MinerU：检查 HTKNOW_MINERU_URL 是否可达，"
                     "或用 HTKNOW_MINERU_MAX_PAGES 限制单次解析页数。")
    if "convert" in logs and ("docx" in logs or "office" in logs or "pdf" in logs):
        hints.append("Office 转换失败：检查 HTKNOW_OFFICE_CONVERT_URL；该文档已标记 optional，"
                     "可用 --exclude calibre_docx_demo 跳过。")
    if parse.get("timeout_count"):
        hints.append("存在解析超时：确认服务端 HTKNOW_PARSE_ENABLED=true，"
                     "并用 --parse-timeout 放宽等待时间；也可查服务端日志中 parse_timing 事件定位慢步骤。")
    if parse.get("reuse_count"):
        hints.append("检测到解析结果复用（文件哈希与历史重复），本次解析耗时不代表真实解析开销："
                     "文本类语料已自动追加运行标记，二进制语料可设置 HTKNOW_REUSE_DUPLICATE_FILES=false 后重测。")
    if metrics.get("eval_k") and metrics["eval_k"] < metrics.get("requested_k", metrics["eval_k"]):
        hints.append(f"服务端每次最多返回 {metrics['eval_k']} 条结果，小于请求的 Top-K="
                     f"{metrics['requested_k']}；如需更深评测请调大 HTKNOW_SEARCH_LIMIT 后重启服务。")
    if metrics.get("hit_rate") is not None and metrics["hit_rate"] < 60:
        hints.append("命中率偏低：先确认对应文档解析成功且切片数 > 0，再检查中文词典/同义词是否加载，"
                     "必要时用 GET /api/v1/knowledge/files/{id}/slices 核对切片正文是否包含期望关键词。")
    if metrics.get("answer_in_context") is not None and metrics.get("hit_rate") is not None \
            and metrics["answer_in_context"] < metrics["hit_rate"] - 15:
        hints.append("文档命中但关键词覆盖不足，通常是切片粒度过粗或过细："
                     "尝试更换 --slice-type（smart / fixed / paragraph）后对比。")
    if metrics.get("negative_pass_rate") is not None and metrics["negative_pass_rate"] < 100:
        hints.append("负样本出现泄漏，说明检索可能返回了不相关内容（向量检索阈值过松或重排序未生效）："
                     "检查 HTKNOW_RERANK_URL 是否配置。")
    for warning in warnings:
        if warning not in hints:
            hints.append(warning)
    return hints


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def resolve_target(args: argparse.Namespace) -> str:
    if args.base_url:
        return args.base_url.rstrip("/")
    if not args.target and getattr(args, "target_positional", None):
        args.target = args.target_positional
    host = args.host
    if not host and args.target:
        raw = args.target.strip()
        if "://" in raw:
            scheme, rest = raw.split("://", 1)
            args.scheme = scheme
            raw = rest
        raw = raw.split("/", 1)[0]
        if raw.startswith("["):                      # IPv6 字面量
            host, _, port = raw.partition("]:")
            host = host.strip("[]")
            args.port = int(port) if port else args.port
        elif ":" in raw:
            host, port = raw.rsplit(":", 1)
            args.port = int(port)
        else:
            host = raw
    if not host:
        raise HarnessError("必须通过 --target HOST:PORT 或 --host/--port 指定被测服务地址")
    return f"{args.scheme}://{host}:{args.port}"


def prepare_corpus(args: argparse.Namespace, client: Client, workdir: Path,
                   cache_dir: Path, run_id: str,
                   specs: Sequence[DocSpec]) -> Tuple[List[DocResult], List[str]]:
    results: List[DocResult] = []
    warnings: List[str] = []
    for spec in specs:
        record = DocResult(doc_id=spec.id, filename=spec.filename, fmt=spec.fmt,
                           language=spec.language, optional=spec.optional,
                           description=spec.description, license=spec.license,
                           homepage=spec.homepage, started_at=now_iso())
        print(f"  · 准备语料 {spec.id:<22} {spec.filename}", flush=True)
        try:
            info = prepare_document(spec, cache_dir, workdir, client,
                                    args.download_timeout, args.strict_integrity,
                                    args.offline, run_id if args.unique_marker else None)
        except Exception as exc:  # noqa: BLE001
            info = {"stage": "download_failed", "error": f"{type(exc).__name__}: {exc}",
                    "notes": [], "integrity": "-"}
        for key in ("stage", "source_url", "integrity", "download_ms", "size_bytes",
                    "sha256", "local_path", "error", "notes"):
            if key in info:
                setattr(record, key, info[key])
        record.finished_at = now_iso()
        if record.stage in ("download_failed",):
            message = f"语料 {spec.id} 获取失败: {record.error}"
            if spec.optional:
                warnings.append(message + "（optional，跳过不影响判定）")
                print(f"    ! {message}", flush=True)
            else:
                warnings.append(message)
                print(f"    ! {message}", flush=True)
        results.append(record)
    return results, warnings


def upload_and_wait(client: Client, args: argparse.Namespace, kb_id: int,
                    specs: Sequence[DocSpec], docs: List[DocResult],
                    workdir: Path) -> List[str]:
    warnings: List[str] = []
    by_id = {d.doc_id: d for d in docs}
    ready = [(s, by_id[s.id]) for s in specs if by_id[s.id].local_path]

    def do_upload(spec: DocSpec, record: DocResult) -> None:
        path = Path(record.local_path)  # type: ignore[arg-type]
        slice_type = spec.slice_type or args.slice_type
        try:
            file_id, elapsed = upload_document(client, kb_id, path, slice_type, args.tags)
        except ApiFailure as exc:
            record.stage, record.error = "upload_failed", str(exc)
            warnings.append(f"上传失败 {spec.id}: {exc}")
            return
        record.kb_file_id, record.upload_ms = file_id, elapsed
        record.size_bytes = path.stat().st_size
        print(f"  · 已上传 {spec.id:<22} file#{file_id}  "
              f"{human_bytes(record.size_bytes)} / {human_ms(elapsed)}", flush=True)

    if args.upload_mode == "batch":
        for spec, record in ready:
            do_upload(spec, record)
        pending = [(s, r) for s, r in ready if r.kb_file_id]
    else:
        pending = []
        for spec, record in ready:
            do_upload(spec, record)
            if record.kb_file_id:
                pending.append((spec, record))
                wait_one(client, args, spec, record)
        return warnings

    # batch 模式：全部上传后统一等待
    deadline = time.perf_counter() + args.parse_timeout
    outstanding = {r.kb_file_id: (s, r) for s, r in pending}
    started = time.perf_counter()
    while outstanding and time.perf_counter() < deadline:
        for file_id in list(outstanding):
            spec, record = outstanding[file_id]
            try:
                payload, _ = file_record(client, file_id)
            except ApiFailure as exc:
                record.stage, record.error = "upload_failed", str(exc)
                del outstanding[file_id]
                continue
            status = payload.get("status") if isinstance(payload, dict) else None
            if status in TERMINAL_STATUS:
                finalize_parse(client, args, spec, record, payload,
                               (time.perf_counter() - started) * 1000.0, 1)
                del outstanding[file_id]
        if outstanding:
            time.sleep(args.poll_interval)
    for spec, record in outstanding.values():
        record.stage = "parse_timeout"
        record.error = f"等待解析超过 {args.parse_timeout:.0f}s 仍未进入终态"
        record.parse_ms = (time.perf_counter() - started) * 1000.0
        warnings.append(f"解析超时 {spec.id}")
    return warnings


def wait_one(client: Client, args: argparse.Namespace, spec: DocSpec, record: DocResult) -> None:
    assert record.kb_file_id is not None
    print(f"  · 等待解析 {spec.id:<22} file#{record.kb_file_id} "
          f"（超时 {args.parse_timeout:.0f}s）", flush=True)
    try:
        payload, elapsed, polls = wait_for_parse(client, record.kb_file_id,
                                                 args.parse_timeout, args.poll_interval,
                                                 progress=args.verbose_status)
    except ApiFailure as exc:
        record.stage, record.error = "upload_failed", str(exc)
        return
    finalize_parse(client, args, spec, record, payload, elapsed, polls)


def finalize_parse(client: Client, args: argparse.Namespace, spec: DocSpec, record: DocResult,
                   payload: Dict[str, Any], elapsed_ms: float, polls: int) -> None:
    record.parse_ms = elapsed_ms
    record.polls = polls
    record.status = payload.get("status")
    record.backend_status = payload.get("backend_status")
    record.processing_status = payload.get("processing_status")
    record.wiki_status = payload.get("wiki_status")
    record.server_log = str(payload.get("log") or "")
    if payload.get("parse_warning"):
        record.notes.append(str(payload["parse_warning"]))
    created, updated = payload.get("created_at"), payload.get("updated_at")
    if isinstance(created, int) and isinstance(updated, int):
        record.server_parse_s = max(0, updated - created)
    elif isinstance(created, str) and isinstance(updated, str):
        try:
            record.server_parse_s = max(0, (datetime.fromisoformat(updated.replace("Z", "+00:00")) -
                                            datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds())
        except ValueError:
            pass
    if "reus" in record.server_log.lower():
        record.reuse_detected = True
    if record.status not in TERMINAL_STATUS:
        record.stage = "parse_timeout"
        record.error = (f"等待 {args.parse_timeout:.0f}s 后状态仍为 "
                        f"{record.status}（{FILE_STATUS.get(record.status or 0, '未知')}）")
        print(f"    ! {record.error}", flush=True)
        return
    if record.status == 1:
        record.stage = "completed"
        try:
            record.slices, record.slice_chars = fetch_slices(client, record.kb_file_id)  # type: ignore[arg-type]
        except ApiFailure as exc:
            record.notes.append(f"读取切片失败: {exc}")
        print(f"    ✓ 解析完成，用时 {human_ms(elapsed_ms)}，切片 {record.slices} 条 / "
              f"{record.slice_chars} 字符", flush=True)
        if record.slices == 0:
            record.notes.append("⚠ 解析标记成功但切片数为 0，检索必然失败")
    elif record.status == 3:
        record.stage = "skipped"
        record.notes.append("服务端标记为跳过（storage 型知识库 / 压缩包 / 不解析类型）")
        print(f"    - 服务端跳过解析：{record.server_log[:120]}", flush=True)
    else:
        record.stage = "parse_failed"
        record.error = f"解析失败：{record.server_log[:600] or '服务端未返回错误信息'}"
        print(f"    ✗ 解析失败：{record.server_log[:200]}", flush=True)
def run_retrieval(client: Client, args: argparse.Namespace, kb_id: int,
                  specs: Sequence[DocSpec], docs: Sequence[DocResult]
                  ) -> Tuple[List[QueryResult], List[NegativeResult], List[float]]:
    file_id_of = {d.doc_id: d.kb_file_id for d in docs if d.kb_file_id}
    parsed_docs = {d.doc_id for d in docs if d.stage == "completed" and d.slices > 0}
    query_results: List[QueryResult] = []
    negative_results: List[NegativeResult] = []
    all_samples: List[float] = []

    specs_with_queries = [s for s in specs if s.queries or s.negatives]
    total = sum(len(s.queries) + len(s.negatives) for s in specs_with_queries)
    counter = 0
    for spec in specs_with_queries:
        for qspec in spec.queries:
            counter += 1
            expected = [fid for fid in [file_id_of.get(spec.id)] if fid]
            result = QueryResult(query_id=qspec.id, doc_id=spec.id, query=qspec.query,
                                 intent=qspec.intent, keywords_all=list(qspec.keywords_all),
                                 keywords_any=list(qspec.keywords_any),
                                 expect_rank_max=qspec.expect_rank_max, grade=qspec.grade,
                                 expected_file_ids=expected)
            if spec.id not in parsed_docs:
                record = next((d for d in docs if d.doc_id == spec.id), None)
                reason = "所属文档未成功解析入索引" if record else "所属文档不在本次运行范围"
                if record and record.stage != "completed":
                    reason += f"（当前状态：{record.stage}）"
                result.skipped_reason = reason
                query_results.append(result)
                print(f"  [{counter}/{total}] {qspec.id:<22} 跳过：{reason}", flush=True)
                continue
            try:
                signatures: List[Tuple[Any, ...]] = []
                for repeat in range(max(1, args.repeats)):
                    raw, elapsed = run_search(client, qspec.query, kb_id)
                    result.latencies_ms.append(elapsed)
                    all_samples.append(elapsed)
                    result.returned_counts.append(len(raw))
                    hits = normalize_hits(raw, args.top_k, expected, qspec.keywords_all)
                    signatures.append(tuple((h.slice_id, h.file_id) for h in hits))
                    if repeat == 0:
                        evaluate_query(result, hits, args.top_k)
                    if repeat:
                        time.sleep(args.think_time)
                result.stable = len(set(signatures)) <= 1
            except ApiFailure as exc:
                result.error = str(exc)
            state = "命中" if result.hit and result.answer_in_context else "未命中"
            rank_txt = f"rank={result.first_rank or '-'}"
            print(f"  [{counter}/{total}] {qspec.id:<22} {state:<4} {rank_txt:<9} "
                  f"nDCG={result.ndcg:.2f} 关键词={len(result.kw_matched)}/{len(result.keywords_all)} "
                  f"P50={human_ms(percentile(result.latencies_ms, 50))}"
                  + (f"  ⚠ {result.error}" if result.error else ""), flush=True)
            query_results.append(result)

        for nspec in spec.negatives:
            counter += 1
            negative = NegativeResult(query_id=nspec.id, doc_id=spec.id, query=nspec.query,
                                      forbidden=list(nspec.forbidden), intent=nspec.intent)
            if spec.id not in parsed_docs:
                negative.skipped_reason = "所属文档未成功解析入索引"
                negative_results.append(negative)
                print(f"  [{counter}/{total}] {nspec.id:<22} 跳过：{negative.skipped_reason}",
                      flush=True)
                continue
            try:
                raw, elapsed = run_search(client, nspec.query, kb_id)
                negative.latencies_ms.append(elapsed)
                all_samples.append(elapsed)
                negative.returned = len(raw)
                hits = normalize_hits(raw, args.top_k, [], nspec.forbidden)
                negative.hits = hits
                blob = "\n".join(h.content for h in hits)
                negative.leaked = [term for term in nspec.forbidden if contains_keyword(blob, term)]
                negative.passed = not negative.leaked
            except ApiFailure as exc:
                negative.error = str(exc)
                negative.passed = False
            print(f"  [{counter}/{total}] {nspec.id:<22} "
                  f"{'无泄漏' if negative.passed else '泄漏 ' + '、'.join(negative.leaked)}"
                  + (f"  ⚠ {negative.error}" if negative.error else ""), flush=True)
            negative_results.append(negative)
        time.sleep(args.think_time)
    return query_results, negative_results, all_samples


def metric_block(results: Sequence[QueryResult], eval_k: int) -> Dict[str, Any]:
    scored = [r for r in results if not r.skipped_reason and not r.error]
    if not scored:
        return {"total_queries": len(results), "evaluated_queries": 0, "hit_rate": None,
                "mrr": None, "ndcg": None, "precision": None, "recall": None,
                "answer_in_context": None, "keyword_coverage": None, "stability": None}

    def pct(value: float) -> float:
        return value * 100.0

    n = len(scored)
    return {
        "total_queries": len(results),
        "evaluated_queries": n,
        "skipped_queries": len(results) - n,
        "hit_rate": pct(sum(1 for r in scored if r.hit) / n),
        "mrr": sum(r.rr for r in scored) / n,
        "ndcg": sum(r.ndcg for r in scored) / n,
        "precision": pct(sum(r.precision for r in scored) / n),
        "recall": pct(sum(r.recall for r in scored) / n),
        "answer_in_context": pct(sum(1 for r in scored if r.answer_in_context) / n),
        "keyword_coverage": pct(sum(r.kw_rate for r in scored) / n),
        "stability": pct(sum(1 for r in scored if r.stable) / n),
        "eval_k": eval_k,
        "strict_rank_pass": (pct(sum(1 for r in scored if r.rank_ok) /
                                 len([r for r in scored if r.expect_rank_max]))
                             if any(r.expect_rank_max for r in scored) else None),
    }


def summarize(args: argparse.Namespace, docs: Sequence[DocResult],
              queries: Sequence[QueryResult], negatives: Sequence[NegativeResult],
              samples: Sequence[float]) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    observed_max = max((max(r.returned_counts) for r in queries if r.returned_counts), default=0)
    eval_k = max(1, min(args.top_k, observed_max)) if observed_max else args.top_k
    warnings: List[str] = []
    if observed_max and observed_max < args.top_k:
        warnings.append(f"服务端单次检索最多返回 {observed_max} 条结果，小于请求的 Top-K={args.top_k}；"
                        f"指标已按 K={eval_k} 计算。如需更深评测请调大服务端 HTKNOW_SEARCH_LIMIT。")

    if eval_k != args.top_k:
        for result in queries:
            if not result.skipped_reason and result.hits is not None:
                evaluate_query(result, result.hits, eval_k)

    metrics = metric_block(queries, eval_k)
    metrics["requested_k"] = args.top_k
    metrics["eval_k"] = eval_k
    metrics["latency"] = aggregate([s for s in (r.latencies_ms for r in queries) for s in s] +
                                   [s for n in negatives for s in n.latencies_ms])
    if samples and metrics["latency"].get("mean"):
        metrics["throughput_qps"] = 1000.0 / metrics["latency"]["mean"] if metrics["latency"]["mean"] else None

    per_doc: Dict[str, Any] = {}
    for doc_id in dict.fromkeys(q.doc_id for q in queries):
        subset = [q for q in queries if q.doc_id == doc_id]
        block = metric_block(subset, eval_k)
        block["latency"] = aggregate([s for q in subset for s in q.latencies_ms])
        per_doc[doc_id] = block
    metrics["per_document"] = per_doc

    evaluated_docs = [d for d in docs if d.kb_file_id is not None]
    parsed_ok = [d for d in docs if d.stage == "completed" and d.slices > 0]
    failed = [d for d in docs if d.stage in ("parse_failed", "parse_timeout", "upload_failed")]
    skipped_optional = [d for d in docs if d.stage in ("download_failed", "skipped")]
    # optional=True 的文档依赖可选外部服务（如 Office 转换器），其失败降级为告警，不参与成功率门禁。
    # Missing downloads/uploads still count as failed required documents. Using
    # only files with a server id silently removed those failures from the gate.
    required_docs = [d for d in docs if not d.optional]
    required_ok = [d for d in required_docs if d.stage == "completed" and d.slices > 0]
    for doc in docs:
        if doc.optional and doc.stage in ("parse_failed", "parse_timeout", "upload_failed",
                                          "download_failed"):
            warnings.append(f"可选文档 {doc.doc_id} 未通过（{doc.stage}）："
                            f"{(doc.error or '无详细信息')[:180]}；不计入解析成功率门禁")
        for note in doc.notes:
            if str(note).startswith("WeKnora 文件处于 finalizing"):
                warnings.append(f"文档 {doc.doc_id}：{note}")
    parse = {
        "evaluated": len(evaluated_docs),
        "evaluated_required": len(required_docs),
        "parsed_ok": len(parsed_ok),
        "parsed_ok_required": len(required_ok),
        "failed": len(failed),
        "skipped_optional": len(skipped_optional),
        "timeout_count": len([d for d in docs if d.stage == "parse_timeout"]),
        "reuse_count": len([d for d in docs if d.reuse_detected]),
        "total_slices": sum(d.slices for d in docs),
        "total_slice_chars": sum(d.slice_chars for d in docs),
        "success_rate": (len(required_ok) / len(required_docs) * 100.0) if required_docs else None,
        "success_rate_all": (len(parsed_ok) / len(evaluated_docs) * 100.0) if evaluated_docs else None,
        "latency": aggregate([d.parse_ms for d in docs if d.parse_ms is not None]),
        "upload_latency": aggregate([d.upload_ms for d in docs if d.upload_ms is not None]),
        "total_parse_ms": sum(d.parse_ms or 0 for d in docs),
        "total_upload_ms": sum(d.upload_ms or 0 for d in docs),
    }
    if parse["reuse_count"]:
        warnings.append(f"{parse['reuse_count']} 个文件命中服务端重复文件解析复用，其解析耗时不代表真实解析开销。")
    if any(d.stage == "completed" and d.slices == 0 for d in docs):
        warnings.append("存在解析成功但切片数为 0 的文件，检索必然无结果，请检查切片与索引写入。")

    neg_evaluated = [n for n in negatives if not n.skipped_reason and not n.error]
    metrics["negative_total"] = len(negatives)
    metrics["negative_evaluated"] = len(neg_evaluated)
    metrics["negative_passed"] = sum(1 for n in neg_evaluated if n.passed)
    metrics["negative_pass_rate"] = (metrics["negative_passed"] / len(neg_evaluated) * 100.0) \
        if neg_evaluated else None
    optional_ids = {d.doc_id for d in docs if d.optional}
    required_queries = [q for q in queries if q.doc_id not in optional_ids] or list(queries)
    metrics["query_coverage"] = (100.0 * sum(not q.skipped_reason and not q.error for q in required_queries)
                                 / len(required_queries)) if required_queries else 0.0
    return metrics, parse, warnings
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="e2e_retrieval_test.py",
        description="HTKnow 端到端检索测试：建库 → 上传 → 解析 → 检索 → 指标评估 → HTML 报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python3 scripts/e2e_retrieval_test.py 192.168.0.46:11000\n"
               "  python3 scripts/e2e_retrieval_test.py --target 192.168.0.46:11000\n"
               "  python3 scripts/e2e_retrieval_test.py --target 127.0.0.1:3000 --docs needle_synthetic\n"
               "  python3 scripts/e2e_retrieval_test.py --target 127.0.0.1:3000 --exclude calibre_docx_demo\n"
               "  python3 scripts/e2e_retrieval_test.py --target 127.0.0.1:3000 --dry-run\n")
    parser.add_argument("target_positional", nargs="?", metavar="HOST:PORT",
                        help="位置参数形式的被测地址，等价于 --target")
    target = parser.add_argument_group("被测服务")
    target.add_argument("--backend", choices=["htknow", "weknora"], default="htknow", help="被测系统，默认 htknow")
    for name in ("template-kb", "embedding-model-id", "chat-model-id", "rerank-model-id", "tenant-id"):
        target.add_argument("--weknora-" + name, default="", help="WeKnora 配置；也可使用对应的 WEKNORA_* 环境变量")
    target.add_argument("--target", help="HOST:PORT，例如 192.168.0.46:11000（也接受带 scheme 的写法）")
    target.add_argument("--host", help="主机名或 IP（与 --port 搭配使用）")
    target.add_argument("--port", type=int, default=3000, help="端口，默认 3000")
    target.add_argument("--scheme", default="http", choices=["http", "https"], help="默认 http")
    target.add_argument("--base-url", help="直接给出完整根地址，优先级最高（反向代理场景）")
    target.add_argument("--user-id", default="1", help="认证头 x-user-id，默认 1")
    target.add_argument("--role", default="admin", help="认证头 x-role，默认 admin")
    target.add_argument("--user-name", default="htknow-e2e", help="认证头 x-user-name，默认 htknow-e2e")
    target.add_argument("--insecure", action="store_true", help="HTTPS 时跳过证书校验")

    corpus = parser.add_argument_group("语料")
    corpus.add_argument("--corpus-file", default=str(DEFAULT_CORPUS),
                        help=f"语料与黄金问题清单，默认 {DEFAULT_CORPUS}")
    corpus.add_argument("--docs", help="只测试指定文档 id，逗号分隔")
    corpus.add_argument("--exclude", help="排除指定文档 id，逗号分隔")
    corpus.add_argument("--corpus-cache", default=None,
                        help="语料目录；默认优先使用脚本同级的 corpus/（离线包自带），"
                             "否则使用 scripts/.corpus 下载缓存")
    corpus.add_argument("--refresh-corpus", action="store_true", help="忽略缓存强制重新下载")
    corpus.add_argument("--offline", action="store_true", help="只用本地缓存，不联网下载")
    corpus.add_argument("--download-timeout", type=float, default=180.0, help="单文件下载超时秒数")
    corpus.add_argument("--strict-integrity", action="store_true",
                        help="SHA-256 不匹配即视为失败（默认仅告警并继续使用）")
    corpus.add_argument("--no-unique-marker", dest="unique_marker", action="store_false",
                        help="不给文本语料追加运行唯一标记（默认追加，用于规避重复文件解析复用）")
    corpus.add_argument("--extra-file", action="append", default=[],
                        help="额外上传的本地文件路径，可重复；不参与黄金问题评估，只统计解析")
    corpus.add_argument("--slice-type", default="smart", choices=["smart", "fixed", "paragraph"],
                        help="上传时使用的切片方式，默认 smart")
    corpus.add_argument("--tags", action="append", default=["e2e-test"], help="上传标签，可重复")

    run = parser.add_argument_group("执行")
    run.add_argument("--mode", choices=["retrieval", "chat", "both"], default="retrieval",
                     help="retrieval=仅检索（默认）；chat=答案评估；both=完整链路")
    run.add_argument("--kb-name", help="知识库名称，默认 E2E检索测试-<时间戳>-<随机>")
    run.add_argument("--parse-priority", type=int, default=90, help="知识库解析优先级 0-100，默认 90")
    run.add_argument("--public", action="store_true", help="创建为公开知识库")
    run.add_argument("--upload-mode", default="sequential", choices=["sequential", "batch"],
                     help="sequential=逐个上传并等待（默认，单文件解析耗时更纯净）；"
                          "batch=先全部上传再统一等待（测排队吞吐）")
    run.add_argument("--parse-timeout", type=float, default=1800.0, help="单文件解析等待超时秒数，默认 1800")
    run.add_argument("--poll-interval", type=float, default=2.0, help="状态轮询间隔秒数，默认 2")
    run.add_argument("--top-k", type=int, default=10, help="评测 Top-K，默认 10（对齐 HTKNOW_SEARCH_LIMIT）")
    run.add_argument("--repeats", type=int, default=3, help="每条问题重复检索次数，用于延迟采样与稳定性检验")
    run.add_argument("--think-time", type=float, default=0.1, help="请求间隔秒数，默认 0.1")
    run.add_argument("--http-timeout", type=float, default=120.0, help="单次 HTTP 超时秒数，默认 120")
    run.add_argument("--retries", type=int, default=2, help="幂等 GET 的重试次数，默认 2")
    run.add_argument("--cleanup", default="delete-on-pass",
                     choices=["keep", "delete", "delete-on-pass"],
                     help="测试后知识库处置，默认 delete-on-pass（通过则删除，失败则保留现场）")
    run.add_argument("--dry-run", action="store_true", help="只准备并校验语料，不连接被测服务")

    answer = parser.add_argument_group("生成答案评估（chat / both）")
    answer.add_argument("--answer-cases", default=str(SCRIPT_DIR / "e2e_answer_cases.json"), help="答案题集 JSON")
    answer.add_argument("--answer-categories", help="只测指定答案分类，逗号分隔")
    answer.add_argument("--answer-limit", type=int, default=0, help="用例数量上限，0=全部")
    answer.add_argument("--answer-repeats", type=int, default=1, help="每个对话用例重复次数，历史各自独立")
    answer.add_argument("--answer-seed", help="合成事实随机种子，默认本次运行 ID；报告记录实际种子")
    answer.add_argument("--rejudge-report", help="保留来源 JSON 报告的答案与证据，使用当前版本重新评审全部轮次")
    answer.add_argument("--rejudge-errors-only", action="store_true", help="仅重评模型评审异常，继承其余评分并记录来源版本")
    answer.add_argument("--answer-timeout", type=float, default=310, help="每轮对话总超时秒数")
    answer.add_argument("--judge-mode", choices=["llm", "rules"], default="llm",
                        help="llm=完整语义评估；rules=仅规则检查，语义门禁保持未通过")
    answer.add_argument("--judge-url", help="独立评审模型的兼容接口，默认 E2E_JUDGE_URL")
    answer.add_argument("--judge-model", help="评审模型名称，默认 E2E_JUDGE_MODEL")
    answer.add_argument("--judge-timeout", type=float, default=120, help="单次评审超时秒数")
    answer.add_argument("--min-answer-correctness", type=float, default=85)
    answer.add_argument("--min-answer-faithfulness", type=float, default=95)
    answer.add_argument("--min-citation-precision", type=float, default=95)
    answer.add_argument("--min-citation-coverage", type=float, default=90)
    answer.add_argument("--min-abstention-rate", type=float, default=90)
    answer.add_argument("--max-answer-p95-ms", type=float, default=0, help="对话总耗时 P95 上限，0=不设门禁")
    answer.add_argument("--max-first-text-ms", type=float, default=0, help="首段文本 P95 上限，0=不设门禁")

    gate = parser.add_argument_group("验收门禁")
    gate.add_argument("--min-parse-success", type=float, default=100.0, help="解析成功率下限 %%，默认 100")
    gate.add_argument("--min-hit-rate", type=float, default=80.0, help="Hit@K 下限 %%，默认 80")
    gate.add_argument("--min-mrr", type=float, default=0.60, help="MRR@K 下限，默认 0.60")
    gate.add_argument("--min-ndcg", type=float, default=0.40,
                      help="nDCG@K 下限，默认 0.40（文档级 qrels 展开到切片级后，"
                           "完美 rank=1 通常落在 0.5~1.0 区间）")
    gate.add_argument("--min-answer-in-context", type=float, default=70.0,
                      help="答案在上下文率下限 %%，默认 70")
    gate.add_argument("--min-keyword-coverage", type=float, default=60.0,
                      help="关键词覆盖率下限 %%（非关键门禁），默认 60")
    gate.add_argument("--max-p95-ms", type=float, default=5000.0, help="检索 P95 延迟上限 ms，默认 5000")
    gate.add_argument("--max-parse-ms", type=float, default=0,
                      help="单文件解析 P95 上限 ms，0 表示不设门禁（默认）")
    gate.add_argument("--require-negatives", action="store_true", default=True,
                      help="负样本泄漏视为关键失败（默认开启）")
    gate.add_argument("--allow-negative-failure", dest="require_negatives", action="store_false",
                      help="负样本泄漏仅告警，不影响退出码")

    report = parser.add_argument_group("报告")
    report.add_argument("--report", help="HTML 报告输出路径，默认 scripts/reports/e2e-report-<run>.html")
    report.add_argument("--json-report", help="JSON 报告输出路径，默认与 HTML 同名 .json")
    report.add_argument("--quiet", action="store_true", help="只输出关键进度")
    parser.add_argument("--verbose-status", action="store_true",
                        help="打印每次文件状态变化（默认只在阶段切换时打印）")
    args = parser.parse_args(argv)
    if args.rejudge_errors_only and not args.rejudge_report:
        parser.error("--rejudge-errors-only 需要 --rejudge-report")
    if args.repeats < 1:
        parser.error("--repeats 至少为 1")
    if args.top_k < 1:
        parser.error("--top-k 至少为 1")
    if args.answer_limit < 0 or not 1 <= args.answer_repeats <= 20:
        parser.error("--answer-limit 必须非负；--answer-repeats 必须为 1 到 20")
    for key in ("answer_timeout", "judge_timeout"):
        if not math.isfinite(getattr(args, key)) or getattr(args, key) <= 0:
            parser.error(f"--{key.replace('_', '-')} 必须是正数")
    for key in ("min_answer_correctness", "min_answer_faithfulness", "min_citation_precision", "min_citation_coverage", "min_abstention_rate"):
        if not math.isfinite(getattr(args, key)) or not 0 <= getattr(args, key) <= 100:
            parser.error(f"--{key.replace('_', '-')} 必须为 0 到 100")
    for key in ("max_answer_p95_ms", "max_first_text_ms"):
        if not math.isfinite(getattr(args, key)) or getattr(args, key) < 0:
            parser.error(f"--{key.replace('_', '-')} 必须非负")
    return args


def compact_report_payloads(queries: Sequence[Dict[str, Any]],
                            negatives: Sequence[Dict[str, Any]]) -> None:
    """把命中结果的完整切片正文替换为截断预览，控制报告体积。"""
    for item in list(queries) + list(negatives):
        for hit in item.get("hits") or []:
            hit["content"] = hit.get("preview") or snippet(str(hit.get("content") or ""), 600)
            hit.pop("preview", None)


def console_summary(report: Dict[str, Any]) -> None:
    cfg, metrics, parse = report["config"], report["metrics"], report["parse"]
    gates = report["gates"]
    line = "─" * 72
    print(f"\n{line}\n测试结果汇总  ·  {cfg['target']}  ·  run {cfg['run_id']}\n{line}")
    print(f"文档    总计 {len(report['documents'])}，解析成功 {parse['parsed_ok']}"
          f"（必测 {parse['parsed_ok_required']}/{parse['evaluated_required']}），"
          f"失败 {parse['failed']}，跳过 {parse['skipped_optional']}，"
          f"切片 {parse['total_slices']} 条")
    print(f"耗时    上传合计 {human_ms(parse['total_upload_ms'])}，"
          f"解析合计 {human_ms(parse['total_parse_ms'])}，"
          f"解析 P95 {human_ms((parse['latency'] or {}).get('p95'))}")
    lat = metrics.get("latency") or {}
    print(f"检索    问题 {metrics['total_queries']} 条（有效 {metrics.get('evaluated_queries', 0)}），"
          f"P50 {human_ms(lat.get('p50'))}，P95 {human_ms(lat.get('p95'))}，"
          f"最大 {human_ms(lat.get('max'))}")
    print(f"质量    Hit@{metrics['eval_k']} {fmt_num(metrics.get('hit_rate'), 1)}%  "
          f"MRR {fmt_num(metrics.get('mrr'), 3)}  nDCG {fmt_num(metrics.get('ndcg'), 3)}  "
          f"答案在上下文 {fmt_num(metrics.get('answer_in_context'), 1)}%  "
          f"负样本 {fmt_num(metrics.get('negative_pass_rate'), 0)}%")
    print(f"{line}\n门禁")
    for gate in gates:
        mark = ("·" if gate["threshold"] is None or (gate["actual"] is None and not gate["critical"])
                else ("✓" if gate["passed"] else "✗"))
        actual = fmt_num(gate["actual"], 2, " " + gate["unit"] if gate["unit"] else "")
        threshold = fmt_num(gate["threshold"], 2, " " + gate["unit"] if gate["unit"] else "")
        flag = "" if gate["critical"] else "（非关键）"
        print(f"  {mark} {gate['name']:<22} {actual:>16}  {gate['comparator']} {threshold:<14}{flag}")
    verdict = "PASS" if report["overall_pass"] else "FAIL"
    print(f"{line}\n总体结论：{verdict}")
    print(f"HTML 报告：{report['config']['report_path']}")
    print(f"JSON 报告：{report['config']['json_report_path']}\n{line}\n")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.rejudge_report:
        from e2e_answer_eval import rejudge_report
        return rejudge_report(args, sys.modules[__name__])
    run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    report_started = now_iso()
    started = time.perf_counter()
    warnings: List[str] = []

    answer_module = None
    judge = None
    if args.mode != "retrieval":
        import e2e_answer_eval as answer_module
        from e2e_judge import Judge, JudgeError
        if not args.dry_run:
            try:
                judge = Judge(args.judge_url, args.judge_model, args.judge_timeout, args.judge_mode)
            except JudgeError as exc:
                raise HarnessError(str(exc)) from None
    if args.mode == "chat" and not args.docs and not args.exclude and args.corpus_file == str(DEFAULT_CORPUS):
        # Chat fixtures are self contained and do not require corpus downloads.
        specs, manifest = [], {}
    else:
        specs, manifest = load_corpus(Path(args.corpus_file),
                                      (args.docs or "").split(",") if args.docs else None,
                                      (args.exclude or "").split(",") if args.exclude else None)
    label = {"retrieval": "端到端检索测试", "chat": "生成答案测试", "both": "检索与答案完整测试"}[args.mode]
    print(f"{args.backend} {label} · run {run_id}")
    print(f"语料清单 {args.corpus_file}（version {manifest.get('version', '-')}），"
          f"选中文档 {len(specs)} 个，黄金问题 {sum(len(s.queries) for s in specs)} 条")

    extra_pairs: List[Tuple[DocSpec, DocResult]] = []
    for index, raw_path in enumerate(args.extra_file, start=1):
        path = Path(raw_path).expanduser()
        if not path.exists():
            raise HarnessError(f"--extra-file 不存在: {path}")
        spec = DocSpec(id=f"extra{index}_{path.stem}", filename=path.name,
                       fmt=path.suffix.lstrip(".").lower() or "bin", optional=True,
                       description="命令行额外上传的本地文件（不参与黄金问题评估）")
        extra_pairs.append((spec, DocResult(
            doc_id=spec.id, filename=spec.filename, fmt=spec.fmt, language="", optional=True,
            stage="ready", source_url=str(path), integrity="local",
            size_bytes=path.stat().st_size, local_path=str(path), sha256=sha256_of(path),
            description=spec.description, started_at=report_started)))

    if args.backend == "weknora" and not (args.target or args.target_positional or args.host or args.base_url):
        args.base_url = os.getenv("WEKNORA_BASE_URL", "")
    base_url = None if args.dry_run else resolve_target(args)
    client = (Client("http://127.0.0.1:1", args.user_id, args.role, args.user_name,
                     args.http_timeout, args.retries, args.insecure, verbose=False) if args.dry_run
              else make_client(args, base_url))

    if args.corpus_cache:
        cache_dir = Path(args.corpus_cache).expanduser()
    elif (SCRIPT_DIR / "corpus").is_dir():
        cache_dir = SCRIPT_DIR / "corpus"
    else:
        cache_dir = SCRIPT_DIR / ".corpus"
    print(f"语料目录 {cache_dir}")
    if args.refresh_corpus and cache_dir.exists():
        for item in cache_dir.iterdir():
            if item.is_file():
                item.unlink()
        print("已清空语料缓存")
    workdir = cache_dir / "_workdir" / run_id
    workdir.mkdir(parents=True, exist_ok=True)
    answer_suite = None
    if answer_module:
        try:
            answer_suite = answer_module.prepare_suite(args, workdir, run_id, sys.modules[__name__])
        except answer_module.AnswerError as exc:
            raise HarnessError(str(exc)) from None
        known_ids = {s.id for s in specs} | {s.id for s, _ in extra_pairs}
        if known_ids & {p["spec"].id for p in answer_suite["pairs"]}:
            raise HarnessError("答案 fixture 文档 ID 与原有语料或额外文件冲突")
        extra_pairs += [(p["spec"], p["record"]) for p in answer_suite["pairs"] if p["scope"] == "main"]
        print(f"答案用例 {len(answer_suite['cases'])} 个，随机种子 {answer_suite['seed']}")

    print("\n[1/6] 准备评测语料")
    docs, prep_warnings = prepare_corpus(args, client, workdir, cache_dir, run_id, specs)
    warnings += prep_warnings
    docs += [record for _, record in extra_pairs]
    specs = list(specs) + [spec for spec, _ in extra_pairs]

    if args.dry_run:
        print("\n--dry-run：跳过被测服务调用")
        for record in docs:
            print(f"  · {record.doc_id:<24} {record.stage:<16} "
                  f"{human_bytes(record.size_bytes):>10}  {record.filename}")
        return EXIT_OK

    print(f"\n[2/6] 预检服务 {base_url}")
    environment = preflight(client)
    print(f"  ✓ 服务可达，认证通过（{human_ms(environment['kb_list_ms'])}）"
          f"，OpenAPI: {environment.get('openapi_title') or '-'} "
          f"{environment.get('openapi_version') or ''}")

    kb_name = args.kb_name or f"E2E检索测试-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    print(f"\n[3/6] 创建知识库「{kb_name}」")
    kb_id, kb_ms = create_knowledge_base(client, kb_name,
                                         f"由 e2e_retrieval_test.py 自动创建，run {run_id}",
                                         args.parse_priority, args.public)
    print(f"  ✓ kb_id={kb_id}（{human_ms(kb_ms)}）")

    print(f"\n[4/6] 上传文件并等待解析（模式 {args.upload_mode}）")
    try:
        warnings += upload_and_wait(client, args, kb_id, specs, docs, workdir)
    except KeyboardInterrupt:
        warnings.append("用户中断，已上传文件可能仍在解析")
        raise

    if args.mode != "chat":
        print(f"\n[5/6] 执行黄金问题集检索（Top-K={args.top_k}，每条重复 {args.repeats} 次）")
    else:
        print("\n[5/6] 执行生成答案评估")
    queries, negatives, samples = (run_retrieval(client, args, kb_id, specs, docs)
                                   if args.mode != "chat" else ([], [], []))

    report_path = Path(args.report) if args.report else SCRIPT_DIR / "reports" / f"e2e-report-{run_id}.html"
    json_path = Path(args.json_report) if args.json_report else report_path.with_suffix(".json")
    answers = None
    fixture_cleanup = []
    session_clients = []
    if answer_module and answer_suite:
        print("\n执行对话答案评估（完整事件流与评审结果逐题保存）")
        answers = answer_module.run_suite(answer_suite, args, client, sys.modules[__name__], workdir,
                                          kb_id, docs, judge, report_path.parent / (report_path.stem + "-answers"))
        fixture_cleanup = answers.pop("_cleanup")
        session_clients = answers.pop("_session_clients", [])

    print("\n[6/6] 计算指标与生成报告")
    metrics, parse, summarize_warnings = summarize(args, docs, queries, negatives, samples)
    warnings += summarize_warnings
    gates = build_gates(args, metrics, parse)
    if args.mode == "chat":
        gates = [g for g in gates if g.metric in ("parse_success_rate", "parse_p95_ms")]
    if answers:
        gates += [GateResult(**g) for g in answers["gates"]]
    overall = all(g.passed for g in gates if g.critical and g.threshold is not None)

    disposition = "保留（--cleanup keep）"
    if args.cleanup == "delete" or (args.cleanup == "delete-on-pass" and overall):
        try:
            delete_knowledge_base(client, kb_id)
            disposition = f"已删除 kb_id={kb_id}"
            print(f"  ✓ 已清理测试知识库 #{kb_id}")
        except ApiFailure as exc:
            disposition = f"删除失败 kb_id={kb_id}: {exc}"
            warnings.append(f"清理知识库 #{kb_id} 失败：{exc}")
    else:
        disposition = f"保留现场 kb_id={kb_id}（{args.cleanup}）"
    fixture_dispositions = []
    for fixture_client, fixture_id in fixture_cleanup:
        state = "保留现场"
        if args.cleanup == "delete" or (args.cleanup == "delete-on-pass" and overall):
            try:
                delete_knowledge_base(fixture_client, fixture_id)
                state = "已删除"
            except ApiFailure:
                state = "删除失败"
                warnings.append(f"答案 fixture 知识库 #{fixture_id} 清理失败")
        fixture_dispositions.append({"kb_id": fixture_id, "disposition": state})
    if answers:
        answers["fixture_dispositions"] = fixture_dispositions
        answers["session_dispositions"] = []
        for session_client in session_clients:
            sessions = list(session_client.backend.sessions)
            state = "保留现场"
            if args.cleanup == "delete" or (args.cleanup == "delete-on-pass" and overall):
                try:
                    session_client.backend.cleanup_sessions()
                    state = "已删除"
                except ApiFailure:
                    state = "删除失败"
                    warnings.append("WeKnora 测试会话清理失败")
            answers["session_dispositions"].extend({"session_id": sid, "disposition": state} for sid in sessions)
        if answers["semantic_status"] != "complete":
            warnings.append("答案语义评估未完整完成；规则检查结果不能作为语义验收通过结论。")

    total_ms = (time.perf_counter() - started) * 1000.0
    report_path.parent.mkdir(parents=True, exist_ok=True)

    config_snapshot = {
        "run_id": run_id, "backend": args.backend, "mode": args.mode, "target": base_url, "kb_id": kb_id, "kb_name": kb_name,
        "backend_config": environment.get("models", {}),
        "report_path": str(report_path), "json_report_path": str(json_path),
        "kb_disposition": disposition, "user_id": args.user_id, "role": args.role,
        "user_name": args.user_name, "slice_type": args.slice_type if args.backend == "htknow" else "WeKnora 知识库配置",
        "top_k": args.top_k, "repeats": args.repeats, "parse_timeout": args.parse_timeout,
        "poll_interval": args.poll_interval, "http_timeout": args.http_timeout,
        "retries": args.retries, "upload_mode": args.upload_mode,
        "corpus_file": str(Path(args.corpus_file).resolve()),
        "corpus_manifest_version": manifest.get("version"),
        "request_count": client.request_count, "cleanup": args.cleanup,
        "started_at": report_started, "finished_at": now_iso(), "total_ms": total_ms,
        "min_hit_rate": args.min_hit_rate, "min_mrr": args.min_mrr,
        "min_ndcg": args.min_ndcg, "min_answer_in_context": args.min_answer_in_context,
        "min_keyword_coverage": args.min_keyword_coverage, "max_p95_ms": args.max_p95_ms,
        "min_parse_success": args.min_parse_success, "max_parse_ms": args.max_parse_ms,
        "argv": list(sys.argv),
    }
    document_rows = [asdict(d) for d in docs]
    query_rows = [asdict(q) for q in queries]
    negative_rows = [asdict(n) for n in negatives]
    compact_report_payloads(query_rows, negative_rows)
    report = {
        "schema_version": 2, "config": config_snapshot, "environment": environment,
        "documents": document_rows, "queries": query_rows, "negatives": negative_rows,
        "metrics": metrics, "parse": parse,
        "gates": [asdict(g) for g in gates], "warnings": warnings,
        "search_samples": sorted(samples), "overall_pass": overall,
        "hints": build_hints(document_rows, metrics, parse, warnings),
    }
    if answers:
        report["answers"] = answers
        report["hints"] += list(dict.fromkeys(row["diagnosis"] for row in answers["rows"] if not row["passed"]))[:12]
        print("答案评估：" + json.dumps(answers["metrics"], ensure_ascii=False))
    report_path.write_text(render_html(report), encoding="utf-8")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                         encoding="utf-8")
    console_summary(report)
    return EXIT_OK if overall else EXIT_GATE_FAILED


if __name__ == "__main__":
    try:
        sys.exit(main())
    except HarnessError as exc:
        print(f"\n✗ 测试中止：{exc}", file=sys.stderr)
        sys.exit(EXIT_ERROR)
    except ApiFailure as exc:
        print(f"\n✗ 接口调用失败：{exc}", file=sys.stderr)
        sys.exit(EXIT_ERROR)
    except KeyboardInterrupt:
        print("\n✗ 用户中断", file=sys.stderr)
        sys.exit(EXIT_ERROR)
