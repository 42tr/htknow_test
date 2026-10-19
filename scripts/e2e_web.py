#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTKnow 端到端检索测试 Web 控制台（零第三方依赖，Python 3.8+）。

在浏览器里输入 HOST:PORT 即可对目标部署发起 e2e_retrieval_test.py 全链路测试：
实时日志、门禁与指标、自包含 HTML 报告、历史运行记录、一键取消。

每次测试以独立子进程运行（python3 -u e2e_retrieval_test.py ...），互不干扰且可终止；
运行产物（日志 / HTML / JSON 报告）保存在 --runs-dir 下，服务重启后仍可查阅。

启动
  python3 scripts/e2e_web.py                      # 监听 127.0.0.1:8099
  python3 scripts/e2e_web.py --bind 0.0.0.0 --port 8099   # 局域网访问
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

SCRIPT_DIR = Path(__file__).resolve().parent
TEST_SCRIPT = SCRIPT_DIR / "e2e_retrieval_test.py"
DEFAULT_RUNS_DIR = SCRIPT_DIR / ".e2e-web" / "runs"

TARGET_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,253}$")
STATUS_LABEL = {"running": "运行中", "pass": "通过", "fail": "未达标",
                "error": "异常", "canceled": "已取消", "interrupted": "已中断"}

NUMERIC_OPTIONS: Dict[str, Tuple[type, float, float]] = {
    "repeats": (int, 1, 20),
    "top_k": (int, 1, 50),
    "parse_timeout": (float, 10, 86400),
    "poll_interval": (float, 0.5, 60),
    "http_timeout": (float, 5, 3600),
    "think_time": (float, 0, 10),
    "download_timeout": (float, 10, 3600),
    "min_parse_success": (float, 0, 100),
    "min_hit_rate": (float, 0, 100),
    "min_mrr": (float, 0, 1),
    "min_ndcg": (float, 0, 1),
    "min_answer_in_context": (float, 0, 100),
    "min_keyword_coverage": (float, 0, 100),
    "max_p95_ms": (float, 0, 3600000),
    "max_parse_ms": (float, 0, 3600000),
    "parse_priority": (int, 0, 100),
    "answer_limit": (int, 0, 500),
    "answer_repeats": (int, 1, 20),
    "answer_timeout": (float, 1, 3600),
    "judge_timeout": (float, 1, 3600),
    "min_answer_correctness": (float, 0, 100),
    "min_answer_faithfulness": (float, 0, 100),
    "min_citation_precision": (float, 0, 100),
    "min_citation_coverage": (float, 0, 100),
    "min_abstention_rate": (float, 0, 100),
    "max_answer_p95_ms": (float, 0, 3600000),
    "max_first_text_ms": (float, 0, 3600000),
}
TEXT_OPTIONS = {"user_id", "role", "user_name", "docs", "exclude", "slice_type",
                "cleanup", "upload_mode", "corpus_cache", "mode", "judge_mode",
                "judge_url", "judge_model", "answer_categories", "answer_seed"}
BOOL_OPTIONS = {"offline", "strict_integrity", "public", "refresh_corpus",
                "allow_negative_failure", "unique_marker"}
CHOICES = {
    "slice_type": {"smart", "fixed", "paragraph"},
    "cleanup": {"keep", "delete", "delete-on-pass"},
    "upload_mode": {"sequential", "batch"},
    "mode": {"retrieval", "chat", "both"},
    "judge_mode": {"llm", "rules"},
}
DEFAULT_PARAMS: Dict[str, Any] = {
    "user_id": "1", "role": "admin", "user_name": "htknow-e2e-web",
    "slice_type": "smart", "cleanup": "delete-on-pass", "upload_mode": "sequential",
    "repeats": 3, "top_k": 10, "parse_timeout": 1800, "poll_interval": 2,
    "http_timeout": 120, "think_time": 0.1,
    "min_parse_success": 100, "min_hit_rate": 80, "min_mrr": 0.6, "min_ndcg": 0.4,
    "min_answer_in_context": 70, "min_keyword_coverage": 60,
    "max_p95_ms": 5000, "max_parse_ms": 0,
    "offline": False, "unique_marker": True,
    "mode": "retrieval", "judge_mode": "llm", "answer_limit": 0, "answer_repeats": 1,
    "answer_timeout": 310, "judge_timeout": 120,
    "min_answer_correctness": 85, "min_answer_faithfulness": 95,
    "min_citation_precision": 95, "min_citation_coverage": 90, "min_abstention_rate": 90,
    "max_answer_p95_ms": 0, "max_first_text_ms": 0,
}


def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


@dataclass
class Run:
    id: str
    target: str
    params: Dict[str, Any]
    argv: List[str]
    run_dir: Path
    status: str = "running"
    returncode: Optional[int] = None
    started_at: str = field(default_factory=now_iso)
    finished_at: Optional[str] = None
    pid: Optional[int] = None
    error: Optional[str] = None
    metrics: Optional[Dict[str, Any]] = None
    proc: Optional[subprocess.Popen] = field(default=None, repr=False)

    @property
    def log_path(self) -> Path:
        return self.run_dir / "console.log"

    @property
    def report_html(self) -> Path:
        return self.run_dir / "report.html"

    @property
    def report_json(self) -> Path:
        return self.run_dir / "report.json"

    @property
    def meta_path(self) -> Path:
        return self.run_dir / "meta.json"

    def elapsed_s(self) -> float:
        end = self.finished_at
        start = datetime.fromisoformat(self.started_at)
        stop = datetime.fromisoformat(end) if end else datetime.now().astimezone().replace(microsecond=0)
        return max(0.0, (stop - start).total_seconds())

    def summary(self) -> Dict[str, Any]:
        return {
            "id": self.id, "target": self.target, "status": self.status,
            "status_label": STATUS_LABEL.get(self.status, self.status),
            "returncode": self.returncode, "started_at": self.started_at,
            "finished_at": self.finished_at, "elapsed_s": round(self.elapsed_s(), 1),
            "pid": self.pid, "error": self.error,
            "command": shlex.join([sys.executable, "-u", *self.argv]),
            "has_report": self.report_html.exists(),
            "has_json": self.report_json.exists(),
            "metrics": self.metrics,
            "params": {k: v for k, v in self.params.items() if k != "target"},
        }


class RunManager:
    def __init__(self, runs_dir: Path, max_concurrent: int):
        self.runs_dir = runs_dir
        self.max_concurrent = max_concurrent
        # 可重入锁：start() 持锁时会调用 running_count()，普通 Lock 会自锁死
        self.lock = threading.RLock()
        self.runs: Dict[str, Run] = {}
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._load_history()
        self._reaper = threading.Thread(target=self._reap_loop, daemon=True)
        self._reaper.start()

    # ---- 历史 ----
    def _load_history(self) -> None:
        for meta_path in sorted(self.runs_dir.glob("*/meta.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            run = Run(id=meta.get("id", meta_path.parent.name), target=meta.get("target", "?"),
                      params=meta.get("params", {}), argv=meta.get("argv", []),
                      run_dir=meta_path.parent, status=meta.get("status", "error"),
                      returncode=meta.get("returncode"), started_at=meta.get("started_at", now_iso()),
                      finished_at=meta.get("finished_at"), pid=meta.get("pid"),
                      error=meta.get("error"), metrics=meta.get("metrics"))
            if run.status == "running":
                run.status = "interrupted"
                run.error = "服务重启时该运行尚未结束"
            self.runs[run.id] = run

    def _persist(self, run: Run) -> None:
        payload = {k: v for k, v in run.summary().items() if k != "metrics"}
        payload.update(argv=run.argv, metrics=run.metrics)
        try:
            run.run_dir.mkdir(parents=True, exist_ok=True)
            run.meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        except OSError:
            pass

    # ---- 生命周期 ----
    def running_count(self) -> int:
        with self.lock:
            return sum(1 for r in self.runs.values() if r.status == "running")

    def start(self, params: Dict[str, Any]) -> Run:
        rejudge_source = None
        if params.get("rejudge_of"):
            rejudge_source = self.get(params["rejudge_of"])
            if rejudge_source.status == "running" or not rejudge_source.report_json.exists():
                raise ValueError("来源测试必须结束并生成 JSON 报告后才能重新评审")
            if not json.loads(rejudge_source.report_json.read_text()).get("answers", {}).get("rows"):
                raise ValueError("来源报告没有对话答案，无法重新评审")
            params = dict(params, mode=rejudge_source.params.get("mode", "chat"))
        target = str(params.get("target") or "").strip()
        if not target:
            raise ValueError("必须填写目标地址 HOST:PORT")
        try:
            parsed = urlparse(target if "://" in target else "http://" + target)
            if (parsed.scheme not in ("http", "https") or parsed.username or parsed.password
                    or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                    or not TARGET_RE.fullmatch(parsed.netloc) or (parsed.port is not None and parsed.port <= 0)):
                raise ValueError("invalid target")
        except ValueError:
            raise ValueError("目标地址须为 HOST:PORT 或 HTTP(S) 根地址，不能包含凭据、路径或查询参数") from None
        if params.get("mode", "retrieval") != "retrieval":
            from e2e_judge import Judge, JudgeError, judge_settings
            try:
                configured = Judge(params.get("judge_url"), params.get("judge_model"),
                                   params.get("judge_timeout", 120), params.get("judge_mode", "llm"))
                settings = judge_settings()
                if settings["E2E_JUDGE_API_KEY"] and params.get("judge_url"):
                    server_url = settings["E2E_JUDGE_URL"].rstrip("/")
                    if server_url and not server_url.endswith("/chat/completions"):
                        server_url += "/chat/completions"
                    if configured.url != server_url:
                        raise JudgeError("服务端已配置评审密钥；页面只能使用 E2E_JUDGE_URL 指定的接口，避免向其他地址发送密钥。")
            except JudgeError as exc:
                raise ValueError(str(exc)) from None
        with self.lock:
            if self.running_count() >= self.max_concurrent:
                raise RuntimeError(f"已有 {self.max_concurrent} 个测试在运行，请等待完成或取消后再试")
            run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
            run_dir = self.runs_dir / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            argv = build_argv(target, params, run_dir)
            if rejudge_source:
                argv += ["--rejudge-report", str(rejudge_source.report_json)]
                if params.get("rejudge_errors_only"):
                    argv += ["--rejudge-errors-only"]
            log_handle = run_dir.joinpath("console.log").open("wb")
            proc = subprocess.Popen([sys.executable, "-u", *argv], cwd=str(SCRIPT_DIR),
                                    stdout=log_handle, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
            log_handle.close()
            run = Run(id=run_id, target=target, params=params, argv=argv, run_dir=run_dir,
                      pid=proc.pid, proc=proc)
            self.runs[run_id] = run
            self._persist(run)
            return run

    def cancel(self, run_id: str) -> Run:
        run = self.get(run_id)
        if run.status != "running":
            raise ValueError(f"运行 {run_id} 当前状态为 {run.status}，无法取消")
        proc = run.proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            try:
                run.returncode = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        run.status = "canceled"
        run.finished_at = now_iso()
        run.error = "用户手动取消（测试进程已终止，目标服务上可能残留本次创建的知识库，可在管理端删除）"
        self._finalize_metrics(run)
        self._persist(run)
        return run

    def shutdown(self) -> int:
        """服务退出时终止所有仍在运行的测试子进程，并标记为已中断。"""
        stopped = 0
        for run in self.list():
            if run.status != "running":
                continue
            proc = run.proc
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                stopped += 1
            run.returncode = proc.returncode if proc is not None else run.returncode
            run.status = "interrupted"
            run.finished_at = now_iso()
            run.error = "Web 服务已停止，测试进程被终止"
            self._finalize_metrics(run)
            self._persist(run)
        return stopped

    def prune(self, keep: int) -> int:
        """只保留最近 keep 次已结束的运行，删除更早的运行目录（keep<=0 表示不清理）。"""
        if keep <= 0:
            return 0
        with self.lock:
            finished = [r for r in self.runs.values() if r.status != "running"]
        finished.sort(key=lambda r: r.started_at, reverse=True)
        removed = 0
        for run in finished[keep:]:
            shutil.rmtree(run.run_dir, ignore_errors=True)
            with self.lock:
                self.runs.pop(run.id, None)
            removed += 1
        return removed

    def get(self, run_id: str) -> Run:
        with self.lock:
            run = self.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    def list(self) -> List[Run]:
        with self.lock:
            runs = list(self.runs.values())
        runs.sort(key=lambda r: r.started_at, reverse=True)
        return runs

    # ---- 回收 ----
    def _reap_loop(self) -> None:
        while True:
            time.sleep(0.6)
            with self.lock:
                pending = [r for r in self.runs.values() if r.status == "running"]
            for run in pending:
                proc = run.proc
                if proc is None or proc.poll() is None:
                    continue
                run.returncode = proc.returncode
                run.status = {0: "pass", 1: "fail"}.get(run.returncode, "error")
                run.finished_at = now_iso()
                if run.status == "error":
                    run.error = (find_abort_reason(run)
                                 or f"测试进程退出码 {run.returncode}（2=脚本或环境错误）")
                self._finalize_metrics(run)
                self._persist(run)

    def _finalize_metrics(self, run: Run) -> None:
        if not run.report_json.exists():
            return
        try:
            report = json.loads(run.report_json.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        metrics = report.get("metrics") or {}
        parse = report.get("parse") or {}
        latency = metrics.get("latency") or {}
        run.metrics = {
            "mode": (report.get("config") or {}).get("mode", "retrieval"),
            "overall_pass": report.get("overall_pass"),
            "eval_k": metrics.get("eval_k"),
            "hit_rate": metrics.get("hit_rate"),
            "mrr": metrics.get("mrr"),
            "ndcg": metrics.get("ndcg"),
            "precision": metrics.get("precision"),
            "recall": metrics.get("recall"),
            "answer_in_context": metrics.get("answer_in_context"),
            "keyword_coverage": metrics.get("keyword_coverage"),
            "stability": metrics.get("stability"),
            "negative_pass_rate": metrics.get("negative_pass_rate"),
            "total_queries": metrics.get("total_queries"),
            "evaluated_queries": metrics.get("evaluated_queries"),
            "search_p50_ms": latency.get("p50"),
            "search_p95_ms": latency.get("p95"),
            "search_max_ms": latency.get("max"),
            "parse_success_rate": parse.get("success_rate"),
            "parsed_ok": parse.get("parsed_ok"),
            "parsed_ok_required": parse.get("parsed_ok_required"),
            "evaluated_required": parse.get("evaluated_required"),
            "total_slices": parse.get("total_slices"),
            "parse_p95_ms": (parse.get("latency") or {}).get("p95"),
            "gates": report.get("gates") or [],
            "warnings": report.get("warnings") or [],
            "hints": report.get("hints") or [],
            "documents": [
                {"doc_id": d.get("doc_id"), "filename": d.get("filename"),
                 "stage": d.get("stage"), "slices": d.get("slices"),
                 "size_bytes": d.get("size_bytes"), "upload_ms": d.get("upload_ms"),
                 "parse_ms": d.get("parse_ms"), "error": d.get("error")}
                for d in (report.get("documents") or [])
            ],
        }
        if report.get("answers"):
            answers = report["answers"]
            run.metrics["answers"] = {"metrics": answers["metrics"], "semantic_status": answers["semantic_status"],
                                      "judge": answers["judge"], "seed": answers["seed"],
                                      "rows": [{key: row.get(key) for key in
                                                ("case_id", "category", "repeat", "turn", "passed", "diagnosis")}
                                               for row in answers["rows"]]}


def build_argv(target: str, params: Dict[str, Any], run_dir: Path) -> List[str]:
    argv: List[str] = [str(TEST_SCRIPT), target,
                       "--report", str(run_dir / "report.html"),
                       "--json-report", str(run_dir / "report.json")]
    for key in ("user_id", "role", "user_name", "docs", "exclude", "corpus_cache", "judge_url", "judge_model",
                "answer_categories", "answer_seed"):
        value = params.get(key)
        if value:
            argv += [f"--{key.replace('_', '-')}", str(value)]
    for key in ("slice_type", "cleanup", "upload_mode", "mode", "judge_mode"):
        value = params.get(key)
        if value:
            argv += [f"--{key.replace('_', '-')}", str(value)]
    for key, (cast, lo, hi) in NUMERIC_OPTIONS.items():
        value = params.get(key)
        if value in (None, ""):
            continue
        try:
            number = cast(float(value))
        except (TypeError, ValueError):
            continue
        number = max(lo, min(hi, number))
        argv += [f"--{key.replace('_', '-')}", str(number)]
    if params.get("offline"):
        argv.append("--offline")
    if params.get("refresh_corpus"):
        argv.append("--refresh-corpus")
    if params.get("strict_integrity"):
        argv.append("--strict-integrity")
    if params.get("public"):
        argv.append("--public")
    if params.get("allow_negative_failure"):
        argv.append("--allow-negative-failure")
    if params.get("unique_marker") is False:
        argv.append("--no-unique-marker")
    return argv


# --------------------------------------------------------------------------- #
# HTTP 服务
# --------------------------------------------------------------------------- #
MANAGER: Optional[RunManager] = None
SERVER_INFO: Dict[str, Any] = {}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message
        super().__init__(message)


def read_log(run: Run, offset: int, limit: int = 262144) -> Dict[str, Any]:
    path = run.log_path
    if not path.exists():
        return {"offset": 0, "chunk": "", "done": run.status != "running"}
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        if offset > size:
            offset = 0
        handle.seek(offset)
        chunk = handle.read(limit)
    text = chunk.decode("utf-8", "replace")
    if len(chunk) == limit:
        # 避免在多字节字符中间截断
        cut = text.rfind("\n")
        if cut > 0:
            text = text[: cut + 1]
    return {"offset": offset + len(text.encode("utf-8")), "chunk": text,
            "done": run.status != "running"}


def read_report_html(run: Run) -> bytes:
    """Use the current answer presentation for history without changing artifacts."""
    original = run.report_html.read_bytes()
    if not run.report_json.exists():
        return original
    try:
        report = json.loads(run.report_json.read_text(encoding="utf-8"))
        if isinstance(report, dict) and report.get("answers"):
            from e2e_retrieval_test import render_html
            return render_html(report).encode("utf-8")
    except (OSError, ValueError, KeyError, TypeError):
        # Older or incomplete artifacts remain readable in their saved format.
        return original
    return original


CORPUS_PATH = SCRIPT_DIR / "e2e_corpus.json"
_CORPUS_CACHE: Dict[str, Any] = {"mtime": None, "payload": None}


def find_abort_reason(run: Run, tail_bytes: int = 16384) -> Optional[str]:
    """从控制台日志尾部提取「✗ ...」中止原因，前端无需翻日志即可看到失败点。"""
    path = run.log_path
    if not path.exists():
        return None
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - tail_bytes))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("✗"):
            return stripped.lstrip("✗").strip()
    return None


def corpus_summary() -> Dict[str, Any]:
    """读取语料清单，供前端展示可勾选的文档；读不到就返回空列表而不抛错。"""
    empty = {"path": str(CORPUS_PATH), "available": False, "documents": [], "queries_total": 0}
    try:
        mtime = CORPUS_PATH.stat().st_mtime
    except OSError:
        return empty
    if _CORPUS_CACHE["payload"] is not None and _CORPUS_CACHE["mtime"] == mtime:
        return _CORPUS_CACHE["payload"]
    try:
        raw = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    documents: List[Dict[str, Any]] = []
    total = 0
    for entry in raw.get("documents") or []:
        queries = len(entry.get("queries") or [])
        generated = bool(entry.get("generator"))
        total += queries
        documents.append({
            "id": entry.get("id"), "filename": entry.get("filename"),
            "format": entry.get("format"), "language": entry.get("language"),
            "optional": bool(entry.get("optional")), "generated": generated,
            "queries": queries, "description": (entry.get("description") or "").strip(),
        })
    payload = {"path": str(CORPUS_PATH), "available": True,
               "documents": documents, "queries_total": total}
    _CORPUS_CACHE.update(mtime=mtime, payload=payload)
    return payload


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "htknow-e2e-web/1.0"
    timeout = 120  # 空闲 keep-alive 连接超时，避免浏览器长连接堆积

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默访问日志
        pass

    # ---- 基础设施 ----
    def _send(self, status: int, body: bytes, content_type: str,
              extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ApiError(400, f"请求体不是合法 JSON: {exc}") from None
        if not isinstance(data, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return data

    def _route(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path in ("/", "/index.html"):
                self._send(200, UI_HTML.encode("utf-8"), "text/html; charset=utf-8")
                return
            if method == "GET" and path == "/api/info":
                self._send_json(200, SERVER_INFO)
                return
            if method == "GET" and path == "/api/defaults":
                self._send_json(200, {"params": DEFAULT_PARAMS,
                                      "choices": {k: sorted(v) for k, v in CHOICES.items()},
                                      "numeric": {k: {"type": v[0].__name__, "min": v[1], "max": v[2]}
                                                  for k, v in NUMERIC_OPTIONS.items()}})
                return
            if method == "GET" and path == "/api/corpus":
                self._send_json(200, corpus_summary())
                return
            if method == "GET" and path == "/api/runs":
                assert MANAGER is not None
                self._send_json(200, {"runs": [r.summary() for r in MANAGER.list()],
                                      "running": MANAGER.running_count(),
                                      "max_concurrent": MANAGER.max_concurrent})
                return
            if method == "POST" and path == "/api/runs":
                assert MANAGER is not None
                params = sanitize_params(self._read_json())
                run = MANAGER.start(params)
                self._send_json(201, run.summary())
                return
            parts = [p for p in path.split("/") if p]
            if len(parts) >= 3 and parts[0] == "api" and parts[1] == "runs":
                assert MANAGER is not None
                run = MANAGER.get(parts[2])
                if len(parts) == 3 and method == "GET":
                    self._send_json(200, run.summary())
                    return
                if len(parts) == 4 and parts[3] == "cancel" and method == "POST":
                    self._send_json(200, MANAGER.cancel(run.id).summary())
                    return
                if len(parts) == 4 and parts[3] == "rejudge" and method == "POST":
                    raw = dict(run.params)
                    overrides = self._read_json()
                    errors_only = overrides.pop("errors_only", False)
                    if type(errors_only) is not bool:
                        raise ValueError("errors_only 必须是布尔值")
                    raw.update(overrides)
                    raw["target"] = run.target
                    params = sanitize_params(raw)
                    params["rejudge_of"] = run.id
                    params["rejudge_errors_only"] = errors_only
                    self._send_json(201, MANAGER.start(params).summary())
                    return
                if len(parts) == 4 and parts[3] == "log" and method == "GET":
                    offset = int((query.get("offset") or ["0"])[0])
                    self._send_json(200, read_log(run, max(0, offset)))
                    return
                if len(parts) == 4 and parts[3] in ("report", "json") and method == "GET":
                    target = run.report_html if parts[3] == "report" else run.report_json
                    if not target.exists():
                        raise ApiError(404, "报告尚未生成（测试可能仍在运行）")
                    ctype = ("text/html; charset=utf-8" if parts[3] == "report"
                             else "application/json; charset=utf-8")
                    body = read_report_html(run) if parts[3] == "report" else target.read_bytes()
                    self._send(200, body, ctype)
                    return
            raise ApiError(404, f"未知路径 {path}")
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except KeyError:
            self._send_json(404, {"error": "运行不存在"})
        except (ValueError, RuntimeError) as exc:
            self._send_json(400, {"error": str(exc)})
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - 兜底，避免服务线程崩溃
            self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_GET(self) -> None:
        self._route("GET")

    def do_HEAD(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")


def sanitize_params(raw: Dict[str, Any]) -> Dict[str, Any]:
    params: Dict[str, Any] = dict(DEFAULT_PARAMS)
    params["target"] = str(raw.get("target") or "").strip()
    for key in TEXT_OPTIONS:
        if key in raw and raw[key] not in (None, ""):
            value = str(raw[key]).strip()
            if key in CHOICES and value not in CHOICES[key]:
                raise ValueError(f"{key} 只能是 {'/'.join(sorted(CHOICES[key]))}")
            if key in ("docs", "exclude"):
                cleaned = ",".join(p.strip() for p in value.split(",") if p.strip())
                value = cleaned
            if len(value) > 512:
                raise ValueError(f"{key} 过长")
            params[key] = value
    for key, (cast, lo, hi) in NUMERIC_OPTIONS.items():
        if key in raw and raw[key] not in (None, ""):
            try:
                number = float(raw[key])
                if not math.isfinite(number):
                    raise ValueError("not finite")
                params[key] = max(lo, min(hi, cast(number)))
            except (TypeError, ValueError):
                raise ValueError(f"{key} 不是合法数字") from None
    for key in BOOL_OPTIONS:
        if key in raw:
            params[key] = bool(raw[key])
    return params


UI_HTML = r"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <meta name="theme-color" content="#f6f7f5" />
    <title>HTKnow · 评测工作台</title>
    <style>
      :root {
        --bg: #f6f7f5;
        --card: #fff;
        --ink: #252c29;
        --muted: #687268;
        --line: #e7eae6;
        --accent: #26664c;
        --accentbg: #edf5ef;
        --ok: #26664c;
        --okbg: #edf5ef;
        --bad: #b34c42;
        --badbg: #fbefec;
        --warn: #946b26;
        --warnbg: #fcf7e9;
        --na: #737b76;
        --nabg: #f0f2ef;
        --radius: 12px;
      }
      * {
        box-sizing: border-box;
      }
      html {
        scroll-behavior: smooth;
        scroll-padding-top: 24px;
      }
      body {
        margin: 0;
        background: var(--bg);
        color: var(--ink);
        font:
          14px/1.6 -apple-system,
          BlinkMacSystemFont,
          "Segoe UI",
          "PingFang SC",
          "Hiragino Sans GB",
          "Microsoft YaHei",
          sans-serif;
        -webkit-font-smoothing: antialiased;
      }
      button,
      input,
      select {
        font: inherit;
      }
      button,
      a,
      input,
      select,
      summary {
        -webkit-tap-highlight-color: transparent;
      }
      button {
        cursor: pointer;
      }
      button:disabled {
        opacity: 0.4;
        cursor: not-allowed;
      }
      button:focus-visible,
      a:focus-visible,
      summary:focus-visible {
        outline: 3px solid #a6cbb5;
        outline-offset: 3px;
      }
      ::selection {
        background: #dcecdf;
      }
      a {
        color: var(--accent);
      }
      h1,
      h2,
      h3,
      p {
        margin: 0;
      }
      button {
        border: 1px solid var(--line);
        background: #fff;
        color: var(--ink);
        padding: 8px 13px;
        border-radius: 7px;
        transition:
          background 0.15s,
          border-color 0.15s;
      }
      button:hover:not(:disabled) {
        background: #f3f5f1;
        border-color: #cdd6cd;
      }
      button.primary {
        color: #fff;
        background: var(--accent);
        border-color: var(--accent);
        font-weight: 600;
      }
      button.primary:hover:not(:disabled) {
        background: #1c513b;
        border-color: #1c513b;
      }
      .danger {
        color: var(--bad);
      }
      button.danger:hover:not(:disabled) {
        background: var(--badbg);
        border-color: #edc9c2;
      }
      .text-button {
        border: 0;
        background: transparent;
        padding: 5px 0;
        color: var(--accent);
        font-size: 13px;
      }
      .text-button:hover:not(:disabled) {
        background: transparent;
        text-decoration: underline;
      }
      .muted {
        color: var(--muted);
        font-size: 12px;
      }
      .mono {
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        font-size: 12px;
      }
      .eyebrow {
        color: var(--muted);
        font-size: 10px;
        font-weight: 600;
        letter-spacing: 1.6px;
        line-height: 1.4;
      }
      .sr-only {
        position: absolute;
        width: 1px;
        height: 1px;
        padding: 0;
        margin: -1px;
        overflow: hidden;
        clip: rect(0, 0, 0, 0);
        white-space: nowrap;
        border: 0;
      }
      [hidden] {
        display: none !important;
      }
      .topbar {
        height: 72px;
        background: #fff;
        border-bottom: 1px solid var(--line);
      }
      .topbar-inner {
        max-width: 1536px;
        padding: 0 40px;
        margin: auto;
        height: 100%;
        display: flex;
        align-items: center;
        gap: 24px;
      }
      .brand {
        display: flex;
        align-items: center;
        gap: 15px;
        text-decoration: none;
        color: var(--ink);
      }
      .wordmark {
        font-size: 23px;
        letter-spacing: -1px;
        font-weight: 750;
      }
      .brand-tag {
        border-left: 1px solid #dce1db;
        padding-left: 15px;
        font-size: 12px;
        color: var(--muted);
        letter-spacing: 0.5px;
      }
      .topbar-right {
        margin-left: auto;
        display: flex;
        align-items: center;
        gap: 24px;
      }
      .service-status {
        display: flex;
        align-items: center;
        gap: 8px;
        color: var(--muted);
        font-size: 12px;
      }
      .service-dot {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        background: #b7bcb8;
        flex: none;
      }
      .service-status.ready .service-dot {
        background: #538563;
      }
      .topbar .new-test {
        padding: 7px 15px;
        font-size: 12px;
      }
      .workspace {
        max-width: 1536px;
        margin: auto;
        padding: 34px 40px 48px;
      }
      .page-intro {
        display: flex;
        align-items: flex-end;
        justify-content: space-between;
        gap: 24px;
        margin-bottom: 28px;
      }
      .breadcrumb {
        font-size: 12px;
        color: var(--muted);
        margin-bottom: 8px;
      }
      .breadcrumb span {
        padding: 0 9px;
        color: #b9beb9;
      }
      .page-intro h1 {
        font-size: 29px;
        font-weight: 650;
        letter-spacing: -0.8px;
        line-height: 1.4;
      }
      .page-intro p {
        color: var(--muted);
        margin-top: 7px;
        font-size: 13px;
      }
      .intro-note {
        color: var(--muted);
        font-size: 12px;
        text-align: right;
        white-space: nowrap;
        padding-bottom: 3px;
      }
      .intro-note strong {
        color: var(--ink);
        font-weight: 600;
        margin-right: 4px;
      }
      .workspace-grid {
        display: grid;
        grid-template-columns: 304px minmax(0, 1fr);
        gap: 24px;
        align-items: start;
      }
      .workspace-grid > * {
        min-width: 0;
      }
      .panel {
        background: var(--card);
        border: 1px solid var(--line);
        border-radius: var(--radius);
      }
      .config-panel {
        position: sticky;
        top: 20px;
        overflow: hidden;
      }
      .panel-heading {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 22px 22px 18px;
      }
      .panel-heading h2 {
        font-size: 17px;
        font-weight: 650;
        line-height: 1.4;
        margin-top: 5px;
      }
      .config-toggle {
        display: none;
      }
      .config-body {
        padding: 0 22px 18px;
      }
      .field {
        margin-bottom: 17px;
      }
      .field:last-child {
        margin-bottom: 0;
      }
      label,
      .field-label {
        display: block;
        color: #505b53;
        font-size: 12px;
        font-weight: 500;
        margin-bottom: 7px;
      }
      .req {
        color: var(--accent);
        margin-left: 3px;
      }
      .field-help {
        font-size: 11px;
        color: var(--muted);
        margin-top: 6px;
        line-height: 1.7;
      }
      input[type="text"],
      input[type="number"],
      input[type="url"],
      input[type="search"],
      select {
        display: block;
        width: 100%;
        min-width: 0;
        border: 1px solid #dfe4dd;
        border-radius: 7px;
        padding: 9px 11px;
        line-height: 1.5;
        background: #fff;
        color: var(--ink);
        transition:
          border-color 0.15s,
          box-shadow 0.15s;
      }
      input::placeholder {
        color: #9aa29b;
        opacity: 1;
      }
      input:focus,
      select:focus {
        outline: none;
        border-color: #6e9c7d;
        box-shadow: 0 0 0 3px #edf5ef;
      }
      input[type="number"] {
        font-variant-numeric: tabular-nums;
      }
      select {
        padding-right: 23px;
      }
      .row {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 12px;
      }
      .row > div {
        min-width: 0;
      }
      .check {
        display: flex;
        gap: 9px;
        align-items: flex-start;
        font-size: 12px;
        font-weight: 400;
        margin: 13px 0;
        color: #5f6861;
      }
      .check input {
        flex: none;
        accent-color: var(--accent);
        margin: 4px 0 0;
        width: 14px;
        height: 14px;
      }
      .mode-switch {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        padding: 4px;
        background: #f0f3ee;
        border-radius: 8px;
        gap: 2px;
      }
      .mode-switch button {
        border: 0;
        background: transparent;
        border-radius: 5px;
        color: #778077;
        font-size: 12px;
        padding: 7px 0;
        white-space: nowrap;
      }
      .mode-switch button[aria-pressed="true"] {
        color: var(--accent);
        background: #fff;
        box-shadow: 0 1px 4px #23382612;
        font-weight: 600;
      }
      .mode-description {
        margin-top: 9px;
        color: var(--muted);
        font-size: 11px;
        line-height: 1.8;
        min-height: 20px;
      }
      .config-separator {
        border: 0;
        border-top: 1px solid var(--line);
        margin: 20px 0;
      }
      .adv {
        border-top: 1px solid var(--line);
        padding: 13px 0;
      }
      .adv > summary {
        cursor: pointer;
        font-size: 12px;
        font-weight: 500;
        color: #5e685f;
        list-style-position: inside;
      }
      .adv > summary::marker {
        font-size: 9px;
        color: #9da79d;
      }
      .adv[open] > summary {
        margin-bottom: 16px;
        color: var(--ink);
      }
      .adv .field {
        margin-bottom: 13px;
      }
      .adv .row {
        margin-bottom: 13px;
      }
      .adv .field-help {
        overflow-wrap: anywhere;
      }
      .answer-options {
        padding-top: 18px;
        border-top: 1px solid var(--line);
        margin-top: 18px;
      }
      .judge-status {
        padding: 8px 10px;
        background: #f5f8f3;
        border-radius: 6px;
        margin: 8px 0 14px;
        color: #66816b;
        font-size: 11px;
        overflow-wrap: anywhere;
      }
      .chips {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        margin-top: 9px;
      }
      .chip-doc {
        padding: 3px 7px;
        border-radius: 4px;
        background: #f6f7f4;
        color: #7c847b;
        font-size: 10px;
        max-width: 100%;
        overflow-wrap: anywhere;
      }
      .chip-doc.on {
        color: var(--accent);
        border-color: #9ab9a3;
        background: var(--accentbg);
      }
      .config-footer {
        padding: 16px 22px 18px;
        border-top: 1px solid var(--line);
        background: #fcfdfb;
      }
      .config-footer .primary {
        width: 100%;
        padding: 11px;
      }
      .config-footer p {
        font-size: 10px;
        text-align: center;
        color: #939c92;
        margin-top: 8px;
      }
      .content-column {
        min-width: 0;
        display: grid;
        gap: 20px;
      }
      .run-panel {
        padding: 26px 28px 0;
        min-width: 0;
        overflow: hidden;
      }
      .run-heading {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 16px;
      }
      .run-heading h2 {
        font-size: 20px;
        font-weight: 600;
        letter-spacing: -0.3px;
        margin-top: 6px;
        overflow-wrap: anywhere;
        line-height: 1.5;
      }
      .run-meta {
        font-size: 11px;
        color: #899188;
        margin-top: 5px;
        overflow-wrap: anywhere;
      }
      .run-toolbar {
        margin: 17px 0 18px;
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
      }
      .run-date {
        font-size: 12px;
        color: var(--muted);
      }
      .actions {
        display: flex;
        gap: 7px;
        flex-wrap: wrap;
      }
      .actions button {
        font-size: 11px;
        padding: 5px 10px;
      }
      .badge {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        padding: 3px 8px;
        border-radius: 5px;
        font-size: 10px;
        font-weight: 500;
        line-height: 1.6;
        white-space: nowrap;
      }
      .badge::before {
        content: "";
        height: 4px;
        width: 4px;
        flex: none;
        background: currentColor;
        border-radius: 50%;
      }
      .badge.ok {
        color: var(--ok);
        background: var(--okbg);
      }
      .badge.bad {
        color: var(--bad);
        background: var(--badbg);
      }
      .badge.warn {
        color: var(--warn);
        background: var(--warnbg);
      }
      .badge.na {
        color: var(--na);
        background: var(--nabg);
      }
      .badge.run {
        color: var(--accent);
        background: var(--accentbg);
      }
      .badge.run::before {
        animation: pulse 1.5s ease infinite;
      }
      .run-heading > .badge {
        padding: 5px 11px;
        font-size: 12px;
      }
      .conclusion {
        display: flex;
        align-items: center;
        gap: 12px;
        justify-content: space-between;
        border-radius: 8px;
        background: #f3f5f0;
        padding: 12px 15px;
        margin-bottom: 20px;
      }
      .conclusion strong {
        display: block;
        font-size: 13px;
        font-weight: 600;
      }
      .conclusion p {
        font-size: 11px;
        color: var(--muted);
        margin-top: 3px;
        line-height: 1.7;
      }
      .conclusion.ok {
        background: var(--okbg);
      }
      .conclusion.ok strong {
        color: var(--ok);
      }
      .conclusion.bad {
        background: var(--badbg);
      }
      .conclusion.bad strong {
        color: var(--bad);
      }
      .conclusion.warn {
        background: var(--warnbg);
      }
      .conclusion.warn strong {
        color: var(--warn);
      }
      .gate-score {
        white-space: nowrap;
        font-size: 22px;
        font-weight: 600;
        font-variant-numeric: tabular-nums;
        letter-spacing: -1px;
        color: var(--accent);
      }
      .gate-score small {
        font-size: 12px;
        font-weight: 400;
        color: var(--muted);
        letter-spacing: 0;
        margin-left: 3px;
      }
      .conclusion.bad .gate-score {
        color: var(--bad);
      }
      .highlights {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        margin: 0 0 24px;
        gap: 20px;
      }
      .highlight + .highlight {
        border-left: 1px solid var(--line);
        padding-left: 20px;
      }
      .highlight .k {
        font-size: 11px;
        color: var(--muted);
        margin-bottom: 7px;
      }
      .highlight .v {
        font-size: 27px;
        font-weight: 600;
        line-height: 1.2;
        letter-spacing: -0.8px;
        font-variant-numeric: tabular-nums;
        white-space: nowrap;
      }
      .highlight .u {
        font-size: 12px;
        font-weight: 400;
        letter-spacing: 0;
        margin-left: 3px;
        color: var(--muted);
      }
      .highlight .caption {
        font-size: 10px;
        color: #949b93;
        margin-top: 6px;
      }
      .tabs {
        display: flex;
        gap: 27px;
        border-bottom: 1px solid var(--line);
        margin: 0;
      }
      .tabs button {
        position: relative;
        border: 0;
        border-radius: 0;
        padding: 12px 0;
        background: transparent;
        color: #899087;
        font-size: 12px;
        font-weight: 500;
      }
      .tabs button.active {
        color: var(--accent);
      }
      .tabs button.active::after {
        content: "";
        position: absolute;
        bottom: -1px;
        height: 2px;
        background: var(--accent);
        left: 0;
        right: 0;
      }
      .tabs button:hover:not(:disabled) {
        background: transparent;
        color: var(--ink);
      }
      .pane {
        display: none;
        padding: 24px 0;
      }
      .pane.active {
        display: block;
      }
      .block-heading {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        margin-bottom: 14px;
      }
      .block-heading h2 {
        font-size: 14px;
        font-weight: 600;
      }
      .block-heading p {
        font-size: 11px;
        color: var(--muted);
        margin-top: 3px;
      }
      .filter-button {
        font-size: 10px;
        padding: 5px 9px;
        color: var(--muted);
        white-space: nowrap;
      }
      .filter-button[aria-pressed="true"] {
        color: var(--accent);
        background: var(--accentbg);
        border-color: #c5dacb;
      }
      .result-block + .result-block {
        margin-top: 24px;
      }
      #m-answers:not(:empty) {
        margin-top: 26px;
      }
      .tbl-wrap {
        overflow-x: auto;
        overscroll-behavior-x: contain;
      }
      table {
        width: 100%;
        border-collapse: collapse;
        font-size: 12px;
      }
      th,
      td {
        padding: 12px 10px;
        border-bottom: 1px solid #edf0ea;
        text-align: left;
        vertical-align: middle;
      }
      th {
        font-weight: 500;
        font-size: 10px;
        color: #939b91;
        background: #f7f8f5;
        white-space: nowrap;
      }
      th:first-child,
      td:first-child {
        padding-left: 13px;
      }
      td.num,
      th.num {
        text-align: right;
        font-variant-numeric: tabular-nums;
        white-space: nowrap;
      }
      td.c,
      th.c {
        text-align: center;
      }
      tbody tr:last-child td {
        border-bottom: 0;
      }
      tbody tr:hover {
        background: #fcfdfb;
      }
      .gate-name {
        font-weight: 500;
        white-space: nowrap;
      }
      .gate-note {
        color: #747e70;
        font-size: 11px;
        max-width: 230px;
        line-height: 1.7;
      }
      .gate-value {
        font-weight: 600;
      }
      .gate-requirement {
        color: #747e70;
        font-size: 11px;
      }
      .gate-table {
        min-width: 620px;
      }
      .details-block {
        margin-top: 22px;
        border-top: 1px solid var(--line);
        padding-top: 17px;
      }
      .details-block > summary {
        cursor: pointer;
        font-size: 12px;
        color: #65715f;
        list-style-position: inside;
      }
      .details-block > summary::marker {
        color: #a5aea0;
        font-size: 9px;
      }
      .details-block > summary span {
        font-size: 10px;
        font-weight: 400;
        color: #99a092;
        margin-left: 8px;
      }
      .details-block[open] > summary {
        margin-bottom: 16px;
      }
      .cards {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 18px 12px;
        margin: 6px 0 4px;
      }
      .card .k {
        font-size: 10px;
        color: var(--muted);
        margin-bottom: 5px;
      }
      .card .v {
        font-size: 18px;
        font-weight: 600;
        font-variant-numeric: tabular-nums;
        letter-spacing: -0.3px;
      }
      .card .u {
        font-size: 10px;
        color: var(--muted);
        margin-left: 2px;
        font-weight: 400;
      }
      .doc-table {
        min-width: 640px;
      }
      .file-name {
        font-size: 10px;
        color: #8e978b;
        max-width: 220px;
        overflow-wrap: anywhere;
        margin-top: 2px;
      }
      .answer-table {
        min-width: 550px;
      }
      .answer-table .diagnosis {
        font-size: 11px;
        line-height: 1.8;
        color: #7e8878;
        min-width: 230px;
      }
      .answer-case {
        font-weight: 600;
      }
      .callout {
        padding: 12px 14px;
        border-radius: 7px;
        margin: 16px 0;
        font-size: 11px;
        line-height: 1.8;
        overflow-wrap: anywhere;
      }
      .callout.bad {
        background: var(--badbg);
        color: var(--bad);
      }
      .callout.warn {
        background: var(--warnbg);
        color: var(--warn);
      }
      .callout > summary {
        cursor: pointer;
        font-weight: 500;
      }
      .callout ul {
        padding-left: 16px;
        margin: 8px 0 0;
      }
      .callout li + li {
        margin-top: 5px;
      }
      .table-empty {
        color: var(--muted);
        text-align: center;
        padding: 26px;
        font-size: 12px;
      }
      .log-toolbar {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 10px;
        font-size: 10px;
        color: var(--muted);
      }
      .log-toolbar button {
        font-size: 10px;
        padding: 4px 8px;
      }
      pre.log {
        background: #202923;
        color: #c8d7c4;
        border-radius: 8px;
        padding: 20px;
        height: 450px;
        overflow: auto;
        font:
          11px/1.9 ui-monospace,
          SFMono-Regular,
          Menlo,
          Consolas,
          monospace;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        margin: 0;
        tab-size: 2;
      }
      pre.log:empty::before {
        content: "等待运行日志…";
        color: #7b9279;
      }
      iframe.report {
        width: 100%;
        height: 760px;
        border: 1px solid var(--line);
        border-radius: 8px;
        background: #fff;
        display: block;
      }
      .history-panel {
        overflow: hidden;
      }
      .history-heading {
        padding: 22px 28px 16px;
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 18px;
      }
      .history-heading h2 {
        font-size: 14px;
        font-weight: 600;
      }
      .history-heading h2 span {
        font-size: 10px;
        color: #929b8e;
        font-weight: 400;
        margin-left: 8px;
      }
      .history-heading input {
        font-size: 11px;
        padding: 7px 10px;
        max-width: 190px;
        background: #fafbf8;
      }
      .history-panel .tbl-wrap {
        padding: 0 15px 10px;
      }
      .history-table {
        min-width: 590px;
      }
      .history-table th {
        background: #fff;
      }
      .history-table td {
        font-size: 11px;
        padding-top: 13px;
        padding-bottom: 13px;
      }
      .history-table .selected {
        background: #f0f6ee;
      }
      .history-table .clickable {
        cursor: pointer;
      }
      .history-table .clickable:hover {
        background: #f4f7f0;
      }
      .history-table .history-open {
        font: inherit;
        border: 0;
        background: none;
        padding: 0;
        color: var(--ink);
        text-align: left;
        white-space: nowrap;
      }
      .history-table .history-open:hover {
        background: none;
        color: var(--accent);
        text-decoration: underline;
      }
      .history-table .mono {
        font-size: 11px;
      }
      .history-table .selected td:first-child {
        box-shadow: inset 2px 0 var(--accent);
      }
      .history-time {
        white-space: nowrap;
        font-size: 10px;
        color: #8f998a;
      }
      .mode-label {
        color: #899381;
        font-size: 10px;
        white-space: nowrap;
      }
      .empty-panel {
        padding: 55px 40px 40px;
      }
      .empty-panel .eyebrow {
        color: #91a38b;
      }
      .empty-panel h2 {
        font-size: 26px;
        font-weight: 550;
        letter-spacing: -0.6px;
        margin: 15px 0 12px;
        line-height: 1.5;
      }
      .empty-panel > p {
        font-size: 13px;
        line-height: 1.9;
        color: #8b9485;
        max-width: 480px;
      }
      .empty-panel .text-button {
        margin-top: 20px;
      }
      .empty-steps {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        list-style: none;
        margin: 44px 0 0;
        padding: 25px 0 0;
        border-top: 1px solid var(--line);
        gap: 25px;
      }
      .empty-steps .step-number {
        color: #a4b09b;
        font:
          12px ui-monospace,
          monospace;
        margin-bottom: 12px;
      }
      .empty-steps h3 {
        font-size: 12px;
        font-weight: 600;
        margin-bottom: 5px;
      }
      .empty-steps p {
        font-size: 11px;
        color: #939b8e;
        line-height: 1.9;
      }
      .page-footer {
        text-align: center;
        padding-top: 28px;
        color: #a2aa9b;
        font-size: 10px;
        letter-spacing: 0.2px;
      }
      #toast {
        position: fixed;
        bottom: 24px;
        left: 50%;
        transform: translate(-50%, 8px);
        padding: 12px 20px;
        background: #2b3a2e;
        color: #fff;
        border-radius: 8px;
        font-size: 12px;
        box-shadow: 0 4px 20px #2233221a;
        max-width: min(480px, calc(100vw - 32px));
        opacity: 0;
        pointer-events: none;
        transition:
          opacity 0.2s,
          transform 0.2s;
        z-index: 50;
      }
      #toast.show {
        opacity: 1;
        transform: translate(-50%, 0);
      }
      #toast.err {
        background: #8e493f;
      }
      @keyframes pulse {
        50% {
          opacity: 0.3;
        }
      }
      @media (prefers-reduced-motion: reduce) {
        html {
          scroll-behavior: auto;
        }
        *,
        *::before,
        *::after {
          animation: none !important;
          transition: none !important;
        }
      }
      @media (min-width: 1600px) {
        .workspace-grid {
          grid-template-columns: 320px minmax(0, 1fr);
        }
      }
      @media (max-width: 1100px) {
        .topbar-inner {
          padding: 0 24px;
        }
        .workspace {
          padding: 28px 24px 40px;
        }
        .workspace-grid {
          grid-template-columns: 280px minmax(0, 1fr);
          gap: 18px;
        }
        .panel-heading {
          padding: 20px 18px 16px;
        }
        .config-body {
          padding: 0 18px 16px;
        }
        .config-footer {
          padding: 15px 18px;
        }
        .run-panel {
          padding: 22px 20px 0;
        }
        .history-heading {
          padding: 20px 20px 14px;
        }
        .highlights {
          gap: 12px;
        }
        .highlight + .highlight {
          padding-left: 12px;
        }
        .highlight .v {
          font-size: 23px;
        }
        .gate-note {
          max-width: 160px;
        }
        .run-date {
          font-size: 11px;
        }
        .run-toolbar {
          align-items: flex-start;
          flex-direction: column;
        }
      }
      @media (max-width: 820px) {
        .topbar {
          height: 62px;
        }
        .topbar-inner {
          padding: 0 20px;
          gap: 12px;
        }
        .wordmark {
          font-size: 21px;
        }
        .brand-tag {
          font-size: 10px;
          padding-left: 12px;
        }
        .topbar-right {
          gap: 12px;
        }
        .service-status {
          font-size: 10px;
        }
        .topbar .new-test {
          display: none;
        }
        .workspace {
          padding: 25px 20px 32px;
        }
        .page-intro {
          margin-bottom: 23px;
          gap: 12px;
        }
        .page-intro h1 {
          font-size: 26px;
        }
        .page-intro p {
          font-size: 12px;
          max-width: 290px;
        }
        .intro-note {
          display: none;
        }
        .workspace-grid {
          grid-template-columns: 1fr;
          gap: 18px;
        }
        .config-panel {
          position: static;
        }
        .config-toggle {
          display: block;
          font-size: 11px;
          color: var(--accent);
          padding: 5px 9px;
          background: #f5f8f2;
        }
        .config-panel .panel-heading {
          padding: 18px 20px;
        }
        .config-panel .eyebrow {
          display: none;
        }
        .panel-heading h2 {
          margin: 0;
          font-size: 15px;
        }
        .config-body {
          padding: 0 20px 16px;
        }
        .config-footer {
          padding: 16px 20px;
        }
        .run-panel {
          padding: 22px 20px 0;
        }
        .run-toolbar {
          flex-direction: row;
          align-items: center;
          flex-wrap: wrap;
        }
        .run-heading h2 {
          font-size: 18px;
        }
        .run-meta {
          font-size: 10px;
        }
        .highlights {
          gap: 14px;
        }
        .highlight .v {
          font-size: 25px;
        }
        .highlight + .highlight {
          padding-left: 14px;
        }
        .highlight .caption {
          font-size: 9px;
        }
        .history-heading {
          padding: 20px 20px 14px;
        }
        .empty-panel {
          padding: 32px 25px;
        }
        .empty-panel h2 {
          font-size: 23px;
        }
        .empty-steps {
          gap: 18px;
          margin-top: 30px;
        }
        .pane {
          padding-top: 20px;
        }
        .page-footer {
          font-size: 9px;
        }
      }
      @media (max-width: 480px) {
        .topbar-inner {
          padding: 0 16px;
        }
        .brand {
          gap: 10px;
        }
        .brand-tag {
          padding-left: 10px;
          font-size: 9px;
        }
        .service-status {
          font-size: 9px;
          gap: 5px;
        }
        .workspace {
          padding: 22px 14px 28px;
        }
        .page-intro {
          padding: 0 4px;
        }
        .breadcrumb {
          font-size: 10px;
        }
        .page-intro h1 {
          font-size: 25px;
        }
        .page-intro p {
          font-size: 11px;
        }
        .run-panel {
          padding: 21px 17px 0;
        }
        .run-heading h2 {
          font-size: 16px;
        }
        .run-heading > .badge {
          font-size: 10px;
          padding: 4px 8px;
        }
        .run-date {
          font-size: 10px;
        }
        .run-toolbar {
          margin: 14px 0 16px;
        }
        .actions {
          gap: 5px;
        }
        .actions button {
          font-size: 10px;
          padding: 5px 8px;
        }
        .conclusion {
          padding: 11px 12px;
          gap: 10px;
          margin-bottom: 20px;
        }
        .conclusion strong {
          font-size: 12px;
        }
        .conclusion p {
          font-size: 10px;
        }
        .gate-score {
          font-size: 20px;
        }
        .highlights {
          grid-template-columns: 1fr 1fr;
          gap: 18px 20px;
          margin-bottom: 20px;
        }
        .highlight + .highlight {
          border: 0;
          padding-left: 0;
        }
        .highlight:nth-child(even) {
          border-left: 1px solid var(--line);
          padding-left: 20px;
        }
        .highlight .k {
          font-size: 10px;
          margin-bottom: 5px;
        }
        .highlight .v {
          font-size: 25px;
        }
        .highlight .caption {
          font-size: 9px;
        }
        .tabs {
          gap: 25px;
        }
        .tabs button {
          font-size: 11px;
          padding: 11px 0;
        }
        .cards {
          grid-template-columns: 1fr 1fr;
        }
        .history-heading {
          gap: 10px;
          padding: 18px 17px 12px;
        }
        .history-heading input {
          max-width: 140px;
          font-size: 10px;
        }
        .history-heading h2 {
          font-size: 12px;
        }
        .history-heading h2 span {
          font-size: 9px;
          margin-left: 5px;
        }
        .history-panel .tbl-wrap {
          padding: 0 10px 8px;
        }
        .block-heading h2 {
          font-size: 13px;
        }
        .block-heading p {
          font-size: 10px;
        }
        .filter-button {
          font-size: 9px;
          padding: 5px 7px;
        }
        .empty-panel {
          padding: 30px 23px;
        }
        .empty-panel h2 {
          font-size: 22px;
        }
        .empty-steps {
          gap: 16px;
          grid-template-columns: 1fr;
          margin-top: 28px;
          padding-top: 22px;
        }
        .empty-steps li {
          display: grid;
          grid-template-columns: 25px 1fr;
          column-gap: 14px;
        }
        .empty-steps .step-number {
          grid-row: 1/3;
          margin: 0;
          padding-top: 1px;
        }
        .empty-steps h3 {
          margin-bottom: 3px;
        }
        .empty-steps p {
          grid-column: 2;
        }
        .pane {
          padding-bottom: 20px;
        }
        pre.log {
          height: 380px;
          padding: 14px;
          font-size: 10px;
        }
        iframe.report {
          height: 640px;
        }
        .row {
          gap: 12px;
        }
        input[type="text"],
        input[type="url"],
        input[type="number"],
        input[type="search"],
        select {
          font-size: 16px;
        }
        input[type="search"] {
          font-size: 12px;
        }
        .history-heading input {
          font-size: 12px;
        }
      }
    </style>
  </head>
  <body>
    <header class="topbar">
      <div class="topbar-inner">
        <a class="brand" href="/" aria-label="HTKnow 评测工作台首页"
          ><span class="wordmark">HTKnow</span
          ><span class="brand-tag">EVALUATION</span></a
        >
        <div class="topbar-right">
          <span class="service-status" id="service-status"
            ><span class="service-dot" aria-hidden="true"></span
            ><span id="info-concurrency">连接中</span></span
          ><span id="info-script" hidden>检索与对话评估</span
          ><button class="new-test" id="btn-new">新建评测</button>
        </div>
      </div>
    </header>
    <main class="workspace">
      <div class="page-intro">
        <div>
          <div class="breadcrumb">工作空间<span>/</span>质量评测</div>
          <h1>评测工作台</h1>
          <p>从检索命中到答案可信度，让质量变化清晰可见。</p>
        </div>
        <div class="intro-note">
          <div><strong id="overview-count">—</strong>次评测已记录</div>
          <div id="overview-latest">建立你的质量基线</div>
        </div>
      </div>
      <div class="workspace-grid">
        <aside
          class="panel config-panel"
          id="config-panel"
          aria-label="新建评测配置"
        >
          <div class="panel-heading">
            <div>
              <p class="eyebrow">CONFIGURATION</p>
              <h2>新建评测</h2>
            </div>
            <button
              class="config-toggle"
              id="btn-config"
              aria-expanded="true"
              aria-controls="form-content"
            >
              收起配置
            </button>
          </div>
          <div id="form-content">
            <div class="config-body">
              <div class="field">
                <span class="field-label" id="mode-label">评测模式</span>
                <div
                  class="mode-switch"
                  role="group"
                  aria-labelledby="mode-label"
                >
                  <button
                    type="button"
                    data-mode="retrieval"
                    aria-pressed="true"
                  >
                    仅检索</button
                  ><button type="button" data-mode="chat" aria-pressed="false">
                    对话答案</button
                  ><button type="button" data-mode="both" aria-pressed="false">
                    完整链路
                  </button>
                </div>
                <label class="sr-only" for="f-mode">评测模式</label
                ><select
                  id="f-mode"
                  class="sr-only"
                  tabindex="-1"
                  aria-hidden="true"
                >
                  <option value="retrieval">仅检索</option>
                  <option value="chat">对话答案</option>
                  <option value="both">完整链路：检索 + 对话答案</option>
                </select>
                <p class="mode-description" id="mode-description">
                  验证文档解析、检索命中与排序质量。
                </p>
              </div>
              <div class="field">
                <label for="f-target"
                  >目标服务地址<span class="req">*</span></label
                ><input
                  type="text"
                  id="f-target"
                  placeholder="192.168.0.46:11000"
                  autocomplete="off"
                  spellcheck="false"
                  required
                  aria-describedby="target-help"
                />
                <p class="field-help" id="target-help">
                  输入 HOST:PORT 或完整的 HTTP(S) 地址
                </p>
              </div>
              <div class="row" id="retrieval-options">
                <div class="field">
                  <label for="f-repeats">问题重复次数</label
                  ><input
                    type="number"
                    id="f-repeats"
                    min="1"
                    max="20"
                    value="3"
                  />
                </div>
                <div class="field">
                  <label for="f-top_k">检索 Top-K</label
                  ><input
                    type="number"
                    id="f-top_k"
                    min="1"
                    max="50"
                    value="10"
                  />
                </div>
              </div>
              <div
                id="answer-options"
                class="answer-options"
                style="display: none"
              >
                <div class="field">
                  <label for="f-judge_mode">答案评审方式</label
                  ><select id="f-judge_mode">
                    <option value="llm">独立模型语义评审</option>
                    <option value="rules">仅规则检查（语义未评估）</option>
                  </select>
                  <div class="judge-status" id="judge-status">
                    正在读取评审配置…
                  </div>
                </div>
                <div class="row">
                  <div class="field">
                    <label for="f-answer_limit">用例上限</label
                    ><input
                      type="number"
                      id="f-answer_limit"
                      min="0"
                      max="500"
                      value="0"
                    />
                    <p class="field-help">0 表示全部 80 个用例，共 100 轮</p>
                  </div>
                  <div class="field">
                    <label for="f-answer_repeats">每用例重复次数</label
                    ><input
                      type="number"
                      id="f-answer_repeats"
                      min="1"
                      max="20"
                      value="1"
                    />
                  </div>
                </div>
                <details class="adv">
                  <summary>自定义评审与用例</summary>
                  <div class="field">
                    <label for="f-answer_categories">用例分类</label
                    ><input
                      type="text"
                      id="f-answer_categories"
                      placeholder="facts,multi_turn"
                    />
                    <p class="field-help">
                      留空评测全部分类；多个分类用逗号分隔。复杂题可填
                      complex_reasoning,partial_answer,advanced_multi_turn
                    </p>
                  </div>
                  <div class="field">
                    <label for="f-judge_url">独立评审接口</label
                    ><input
                      type="url"
                      id="f-judge_url"
                      placeholder="https://provider.example/v1"
                      autocomplete="off"
                    />
                    <p class="field-help">留空使用服务端已配置的接口</p>
                  </div>
                  <div class="field">
                    <label for="f-judge_model">评审模型</label
                    ><input
                      type="text"
                      id="f-judge_model"
                      placeholder="使用服务端配置"
                      autocomplete="off"
                    />
                  </div>
                  <div class="field">
                    <label for="f-answer_seed">合成事实种子</label
                    ><input
                      type="text"
                      id="f-answer_seed"
                      placeholder="留空每次生成新事实"
                      autocomplete="off"
                    />
                  </div>
                  <p class="field-help">仅规则检查会保留“语义未评估”状态。</p>
                </details>
              </div>
              <details class="adv">
                <summary>运行设置</summary>
                <div class="row">
                  <div>
                    <label for="f-slice_type">切片方式</label
                    ><select id="f-slice_type">
                      <option value="smart">智能切片</option>
                      <option value="fixed">固定长度</option>
                      <option value="paragraph">按段落</option>
                    </select>
                  </div>
                  <div>
                    <label for="f-upload_mode">上传模式</label
                    ><select id="f-upload_mode">
                      <option value="sequential">逐个等待</option>
                      <option value="batch">批量上传</option>
                    </select>
                  </div>
                </div>
                <div class="row">
                  <div>
                    <label for="f-parse_timeout">解析超时 / 秒</label
                    ><input
                      type="number"
                      id="f-parse_timeout"
                      min="10"
                      value="1800"
                    />
                  </div>
                  <div>
                    <label for="f-cleanup">测试后知识库</label
                    ><select id="f-cleanup">
                      <option value="delete-on-pass">通过后删除</option>
                      <option value="keep">保留</option>
                      <option value="delete">始终删除</option>
                    </select>
                  </div>
                </div>
                <label class="check"
                  ><input type="checkbox" id="f-offline" /><span
                    >离线模式，仅使用本地语料缓存</span
                  ></label
                >
              </details>
              <details class="adv">
                <summary>语料与权限</summary>
                <div class="field">
                  <label for="f-docs">指定评测文档</label
                  ><input
                    type="text"
                    id="f-docs"
                    placeholder="留空使用全部文档"
                  />
                  <p class="field-help">
                    点击选择文档，或输入以逗号分隔的文档 ID
                  </p>
                  <div class="chips" id="doc-chips"></div>
                </div>
                <div class="field">
                  <label for="f-exclude">排除文档</label
                  ><input
                    type="text"
                    id="f-exclude"
                    placeholder="calibre_docx_demo"
                  />
                </div>
                <div class="row">
                  <div>
                    <label for="f-user_id">用户 ID</label
                    ><input type="text" id="f-user_id" value="1" />
                  </div>
                  <div>
                    <label for="f-role">角色</label
                    ><input type="text" id="f-role" value="admin" />
                  </div>
                </div>
                <label class="check"
                  ><input type="checkbox" id="f-public" /><span
                    >创建公开知识库</span
                  ></label
                ><label class="check"
                  ><input type="checkbox" id="f-strict_integrity" /><span
                    >语料校验不一致即失败</span
                  ></label
                >
              </details>
              <details class="adv">
                <summary>检索验收门禁</summary>
                <div class="row">
                  <div>
                    <label for="f-min_hit_rate">Hit@K 下限 / %</label
                    ><input
                      type="number"
                      id="f-min_hit_rate"
                      value="80"
                      min="0"
                      max="100"
                      step="1"
                    />
                  </div>
                  <div>
                    <label for="f-min_mrr">MRR 下限</label
                    ><input
                      type="number"
                      id="f-min_mrr"
                      value="0.6"
                      min="0"
                      max="1"
                      step="0.05"
                    />
                  </div>
                </div>
                <div class="row">
                  <div>
                    <label for="f-min_ndcg">nDCG 下限</label
                    ><input
                      type="number"
                      id="f-min_ndcg"
                      value="0.4"
                      min="0"
                      max="1"
                      step="0.05"
                    />
                  </div>
                  <div>
                    <label for="f-min_answer_in_context">上下文命中 / %</label
                    ><input
                      type="number"
                      id="f-min_answer_in_context"
                      value="70"
                      min="0"
                      max="100"
                      step="1"
                    />
                  </div>
                </div>
                <div class="row">
                  <div>
                    <label for="f-min_parse_success">解析成功率 / %</label
                    ><input
                      type="number"
                      id="f-min_parse_success"
                      value="100"
                      min="0"
                      max="100"
                      step="1"
                    />
                  </div>
                  <div>
                    <label for="f-max_p95_ms">检索 P95 / ms</label
                    ><input
                      type="number"
                      id="f-max_p95_ms"
                      value="5000"
                      min="0"
                      step="100"
                    />
                  </div>
                </div>
                <label class="check"
                  ><input type="checkbox" id="f-allow_negative_failure" /><span
                    >负样本泄漏仅告警</span
                  ></label
                >
              </details>
            </div>
            <div class="config-footer">
              <button class="primary" id="btn-start">开始评测</button>
              <p>自动执行测试，生成可追溯的评测报告</p>
            </div>
          </div>
        </aside>
        <div class="content-column">
          <section
            class="panel run-panel"
            id="run-section"
            style="display: none"
            aria-label="评测结果"
            tabindex="-1"
          >
            <div class="run-heading">
              <div>
                <p class="eyebrow" id="r-kind">评测结果</p>
                <h2 id="r-target">—</h2>
              </div>
              <span class="badge na" id="r-status" role="status">—</span>
            </div>
            <p class="run-meta mono" id="r-meta">—</p>
            <div class="run-toolbar">
              <span class="run-date" id="r-date">—</span>
              <div class="actions">
                <button id="btn-cancel" class="danger">取消运行</button
                ><button id="btn-json" disabled>下载 JSON</button
                ><button id="btn-report" disabled>打开完整报告</button>
              </div>
            </div>
            <div id="r-summary" aria-live="polite"></div>
            <div class="highlights" id="r-highlights"></div>
            <div id="r-warn"></div>
            <div class="tabs" role="tablist" aria-label="运行详情">
              <button
                id="tab-metrics"
                role="tab"
                data-tab="metrics"
                aria-controls="pane-metrics"
                aria-selected="false"
                tabindex="-1"
              >
                指标与门禁</button
              ><button
                id="tab-log"
                role="tab"
                data-tab="log"
                class="active"
                aria-controls="pane-log"
                aria-selected="true"
              >
                运行日志</button
              ><button
                id="tab-report"
                role="tab"
                data-tab="report"
                aria-controls="pane-report"
                aria-selected="false"
                tabindex="-1"
              >
                完整报告
              </button>
            </div>
            <div
              class="pane active"
              id="pane-log"
              role="tabpanel"
              aria-labelledby="tab-log"
            >
              <div class="log-toolbar">
                <span id="log-status">实时输出 · 自动滚动至最新</span
                ><button id="btn-log-bottom">回到最新</button>
              </div>
              <pre
                class="log"
                id="log"
                tabindex="0"
                aria-label="运行日志"
              ></pre>
            </div>
            <div
              class="pane"
              id="pane-metrics"
              role="tabpanel"
              aria-labelledby="tab-metrics"
            >
              <div id="m-gates"></div>
              <div id="m-answers"></div>
              <details class="details-block" id="all-metrics">
                <summary>
                  全部质量与性能指标<span>展开查看详细数值</span>
                </summary>
                <div class="cards" id="m-cards"></div>
              </details>
              <div id="m-docs"></div>
              <div id="m-hints"></div>
            </div>
            <div
              class="pane"
              id="pane-report"
              role="tabpanel"
              aria-labelledby="tab-report"
            >
              <iframe
                class="report"
                id="report-frame"
                title="完整评测报告"
                src="about:blank"
              ></iframe>
            </div>
          </section>
          <section class="panel empty-panel" id="empty-section">
            <p class="eyebrow">YOUR QUALITY BASELINE</p>
            <h2>从一次评测，<br />了解知识库的回答质量。</h2>
            <p>
              选择目标服务和评测模式，自动验证检索质量与答案可信度。每一次运行都有据可查。
            </p>
            <button class="text-button" id="btn-empty-start">
              填写目标地址，开始评测
            </button>
            <ol class="empty-steps">
              <li>
                <div class="step-number">01</div>
                <h3>配置目标</h3>
                <p>选择服务地址与评测范围</p>
              </li>
              <li>
                <div class="step-number">02</div>
                <h3>自动执行</h3>
                <p>建库、解析、检索与答案评审</p>
              </li>
              <li>
                <div class="step-number">03</div>
                <h3>定位问题</h3>
                <p>查看指标、证据与逐题诊断</p>
              </li>
            </ol>
          </section>
          <section class="panel history-panel" aria-label="历史运行">
            <div class="history-heading">
              <h2>运行记录<span id="hist-count">—</span></h2>
              <label for="hist-search" class="sr-only">搜索运行记录</label
              ><input
                id="hist-search"
                type="search"
                placeholder="搜索目标或运行 ID"
                autocomplete="off"
              />
            </div>
            <div class="tbl-wrap">
              <table class="history-table" id="hist-table">
                <thead>
                  <tr>
                    <th>目标 / 开始时间</th>
                    <th>评测模式</th>
                    <th>状态</th>
                    <th class="num">质量</th>
                    <th class="num">P95 耗时</th>
                  </tr>
                </thead>
                <tbody id="hist-body">
                  <tr>
                    <td colspan="5" class="table-empty">正在读取运行记录…</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>
        </div>
      </div>
      <footer class="page-footer">
        HTKnow Evaluation · 每一次评测，都是质量的下一步
      </footer>
    </main>
    <div id="toast" role="status" aria-live="polite"></div>
    <script>
      "use strict";
      var $ = function (s) {
        return document.querySelector(s);
      };
      var currentRun = null,
        currentRunData = null,
        logOffset = 0,
        logTimer = null,
        listTimer = null,
        lastDone = null;
      var runs = [],
        initialList = true,
        gateOnlyFailed = false,
        answerOnlyFailed = false,
        pollBusy = false,
        selectionVersion = 0;
      var modeNames = {
        retrieval: "仅检索",
        chat: "对话答案",
        both: "完整链路",
      };
      function toast(msg, isErr) {
        var el = $("#toast");
        el.textContent = msg;
        el.className = "show" + (isErr ? " err" : "");
        clearTimeout(el._t);
        el._t = setTimeout(function () {
          el.className = "";
        }, 4200);
      }
      function api(path, opts) {
        return fetch(path, opts).then(function (r) {
          return r.json().then(function (j) {
            if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
            return j;
          });
        });
      }
      function esc(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
          return {
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#39;",
          }[c];
        });
      }
      function fmtMs(v) {
        if (v == null) return "—";
        if (v < 1000) return Math.round(v) + " ms";
        if (v < 60000) return (v / 1000).toFixed(2) + " s";
        return (
          Math.floor(v / 60000) + "m " + Math.round((v % 60000) / 1000) + "s"
        );
      }
      function fmtNum(v, d) {
        return v == null ? "—" : Number(v).toFixed(d == null ? 1 : d);
      }
      function dateLabel(s) {
        return s ? String(s).replace("T", " ").slice(0, 16) : "—";
      }
      function badge(cls, label) {
        return '<span class="badge ' + cls + '">' + esc(label) + "</span>";
      }
      function statusBadge(s) {
        return badge(
          {
            running: "run",
            pass: "ok",
            fail: "bad",
            error: "bad",
            canceled: "warn",
            interrupted: "warn",
          }[s] || "na",
          {
            running: "运行中",
            pass: "通过",
            fail: "未达标",
            error: "异常",
            canceled: "已取消",
            interrupted: "已中断",
          }[s] || s,
        );
      }
      function setConfigExpanded(expanded) {
        $("#form-content").hidden = !expanded;
        $("#btn-config").setAttribute("aria-expanded", String(expanded));
        $("#btn-config").textContent = expanded ? "收起配置" : "展开配置";
      }
      function focusConfig() {
        setConfigExpanded(true);
        $("#config-panel").scrollIntoView({
          behavior: "smooth",
          block: "start",
        });
        $("#f-target").focus({ preventScroll: true });
      }
      function updateMode() {
        var mode = $("#f-mode").value;
        document.querySelectorAll("[data-mode]").forEach(function (b) {
          b.setAttribute("aria-pressed", String(b.dataset.mode === mode));
        });
        $("#answer-options").style.display = mode === "retrieval" ? "none" : "";
        $("#retrieval-options").hidden = mode === "chat";
        $("#mode-description").textContent = {
          retrieval: "验证文档解析、检索命中与排序质量。",
          chat: "用独立模型评审答案、证据与引用。",
          both: "同时验证检索与对话，覆盖完整问答链路。",
        }[mode];
      }
      function collectParams() {
        var p = { target: $("#f-target").value.trim() };
        [
          "slice_type",
          "upload_mode",
          "cleanup",
          "user_id",
          "role",
          "docs",
          "exclude",
          "mode",
          "judge_mode",
          "judge_url",
          "judge_model",
          "answer_categories",
          "answer_seed",
        ].forEach(function (k) {
          var v = $("#f-" + k).value.trim();
          if (v) p[k] = v;
        });
        [
          "repeats",
          "top_k",
          "parse_timeout",
          "min_hit_rate",
          "min_mrr",
          "min_ndcg",
          "min_answer_in_context",
          "min_parse_success",
          "max_p95_ms",
          "answer_limit",
          "answer_repeats",
        ].forEach(function (k) {
          var v = $("#f-" + k).value;
          if (v !== "") p[k] = Number(v);
        });
        [
          "offline",
          "public",
          "strict_integrity",
          "allow_negative_failure",
        ].forEach(function (k) {
          p[k] = $("#f-" + k).checked;
        });
        return p;
      }
      function validateInputs() {
        var invalid = Array.from(
          document.querySelectorAll("#form-content input"),
        ).find(function (el) {
          if (
            $("#f-mode").value === "retrieval" &&
            el.closest("#answer-options")
          )
            return false;
          if ($("#f-mode").value === "chat" && el.closest("#retrieval-options"))
            return false;
          return !el.checkValidity();
        });
        if (!invalid) return true;
        setConfigExpanded(true);
        var parent = invalid.parentElement;
        while (parent && parent.id !== "form-content") {
          if (parent.tagName === "DETAILS") parent.open = true;
          parent = parent.parentElement;
        }
        invalid.reportValidity();
        invalid.focus();
        return false;
      }
      function startRun() {
        if ($("#btn-start").disabled) return;
        if (!validateInputs()) return;
        var p = collectParams();
        if (!p.target) {
          toast("请先填写目标服务地址", true);
          focusConfig();
          return;
        }
        var btn = $("#btn-start");
        btn.disabled = true;
        btn.textContent = "正在创建评测…";
        api("/api/runs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(p),
        })
          .then(function (run) {
            toast("评测已启动，可以查看实时日志");
            selectRun(run.id, "log");
            if (window.innerWidth <= 820) {
              setConfigExpanded(false);
              $("#run-section").scrollIntoView({
                behavior: "smooth",
                block: "start",
              });
            }
            refreshList();
          })
          .catch(function (e) {
            toast(e.message, true);
          })
          .then(function () {
            btn.disabled = false;
            btn.textContent = "开始评测";
          });
      }
      function cancelRun() {
        if (!currentRun) return;
        $("#btn-cancel").disabled = true;
        api("/api/runs/" + encodeURIComponent(currentRun) + "/cancel", {
          method: "POST",
        })
          .then(function () {
            toast("评测已取消");
            pollOnce(true);
            refreshList();
          })
          .catch(function (e) {
            toast(e.message, true);
            pollOnce(true);
          });
      }
      function selectRun(id, preferredTab) {
        selectionVersion++;
        currentRun = id;
        currentRunData = null;
        logOffset = 0;
        lastDone = null;
        pollBusy = false;
        gateOnlyFailed = false;
        answerOnlyFailed = false;
        $("#log").textContent = "";
        $("#run-section").style.display = "";
        $("#empty-section").style.display = "none";
        $("#report-frame").src = "about:blank";
        $("#r-target").textContent = "正在读取评测…";
        $("#r-meta").textContent = id;
        $("#r-summary").innerHTML = "";
        $("#r-highlights").innerHTML = "";
        $("#r-warn").innerHTML = "";
        $("#m-gates").innerHTML = '<p class="muted">正在读取评测结果…</p>';
        ["#m-answers", "#m-docs", "#m-cards", "#m-hints"].forEach(function (s) {
          $(s).innerHTML = "";
        });
        $("#r-status").outerHTML =
          '<span class="badge na" id="r-status" role="status">读取中</span>';
        ["#btn-cancel", "#btn-report", "#btn-json"].forEach(function (s) {
          $(s).disabled = true;
        });
        var known = runs.find(function (r) {
          return r.id === id;
        });
        switchTab(
          preferredTab ||
            (known && known.status !== "running" ? "metrics" : "log"),
        );
        if (logTimer) clearInterval(logTimer);
        logTimer = setInterval(function () {
          pollOnce(false);
        }, 1000);
        pollOnce(true);
        renderHistory();
        var query = new URLSearchParams(location.search);
        query.set("run", id);
        query.set("tab", $(".tabs button.active").dataset.tab);
        history.replaceState(null, "", "?" + query.toString());
      }
      function pollOnce(force) {
        if (!currentRun || pollBusy) return;
        pollBusy = true;
        var id = currentRun,
          version = selectionVersion,
          receivedLogChunk = false;
        return api(
          "/api/runs/" + encodeURIComponent(id) + "/log?offset=" + logOffset,
        )
          .then(function (d) {
            if (version !== selectionVersion) return null;
            logOffset = d.offset;
            if (d.chunk) {
              receivedLogChunk = true;
              var el = $("#log"),
                near = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
              el.textContent += d.chunk;
              if (near) el.scrollTop = el.scrollHeight;
            }
            return api("/api/runs/" + encodeURIComponent(id));
          })
          .then(function (run) {
            if (!run || version !== selectionVersion) return;
            currentRunData = run;
            renderRun(run);
            if (run.status !== "running") {
              // Drain all log pages and allow one final read after completion.
              if (lastDone === run.id && !receivedLogChunk) {
                clearInterval(logTimer);
                logTimer = null;
              }
              if (lastDone !== run.id) {
                lastDone = run.id;
                renderMetrics(run);
                refreshList();
                if (!force)
                  toast(
                    run.status === "pass"
                      ? "评测完成，关键门禁全部达标"
                      : run.status === "fail"
                        ? "评测完成，请查看未达标门禁"
                        : "评测已结束，请查看运行详情",
                    run.status !== "pass",
                  );
              }
            }
            if (force) renderMetrics(run);
          })
          .catch(function (e) {
            if (force && version === selectionVersion)
              toast("读取运行失败：" + e.message, true);
          })
          .then(function () {
            if (version === selectionVersion) pollBusy = false;
          });
      }
      function renderRun(run) {
        var mode = (run.params || {}).mode || "retrieval";
        $("#r-status").outerHTML = statusBadge(run.status).replace(
          'class="badge',
          'id="r-status" role="status" class="badge',
        );
        $("#r-kind").textContent = modeNames[mode] + " / 评测结果";
        $("#r-target").textContent = run.target;
        $("#r-meta").textContent = run.id;
        $("#r-meta").title = run.command ? "等效命令行：\n" + run.command : "";
        $("#r-date").textContent =
          dateLabel(run.started_at) +
          " · " +
          (run.status === "running" ? "已运行 " : "用时 ") +
          fmtMs(run.elapsed_s * 1000);
        $("#btn-cancel").hidden = run.status !== "running";
        $("#btn-cancel").disabled = run.status !== "running";
        $("#btn-report").disabled = !run.has_report;
        $("#btn-json").disabled = !run.has_json;
        $("#log-status").textContent =
          run.status === "running"
            ? "实时输出 · 自动滚动至最新"
            : "运行已结束 · 日志已完整保留";
        var warnings = (run.metrics && run.metrics.warnings) || [];
        $("#r-warn").innerHTML = run.error
          ? '<div class="callout bad">' + esc(run.error) + "</div>"
          : warnings.length
            ? '<details class="callout warn"><summary>运行提醒 · ' +
              warnings.length +
              " 项</summary><ul>" +
              warnings
                .map(function (w) {
                  return "<li>" + esc(w) + "</li>";
                })
                .join("") +
              "</ul></details>"
            : "";
        renderSummary(run);
      }
      function renderSummary(run) {
        var m = run.metrics || {},
          gates = (m.gates || []).filter(function (g) {
            return g.threshold != null;
          }),
          passed = gates.filter(function (g) {
            return g.passed;
          }).length,
          failed = gates.length - passed;
        var titles = {
          running: "评测正在进行",
          pass: "关键门禁全部达标",
          fail: "部分验收门禁未达标",
          error: "本次评测异常结束",
          canceled: "本次评测已取消",
          interrupted: "本次评测已中断",
        };
        var desc =
          run.status === "running"
            ? "正在执行测试，结果将在运行结束后汇总。"
            : gates.length
              ? "已检查 " +
                gates.length +
                " 项门禁，" +
                passed +
                " 项通过" +
                (failed ? "，" + failed + " 项未通过。" : "。")
              : "请查看日志，了解本次运行的详细情况。";
        if (m.answers && m.answers.semantic_status !== "complete")
          desc += " 答案语义评审尚未完整完成。";
        var cls =
          {
            pass: "ok",
            fail: "bad",
            error: "bad",
            canceled: "warn",
            interrupted: "warn",
          }[run.status] || "";
        $("#r-summary").innerHTML =
          '<div class="conclusion ' +
          cls +
          '"><div><strong>' +
          esc(titles[run.status] || "评测结果") +
          "</strong><p>" +
          esc(desc) +
          "</p></div>" +
          (gates.length
            ? '<div class="gate-score">' +
              passed +
              "<small>/ " +
              gates.length +
              "</small></div>"
            : "") +
          "</div>";
        var am = m.answers && m.answers.metrics;
        var cards = am
          ? [
              [
                "答案正确率",
                fmtNum(am.correctness),
                "%",
                "参考事实与回答一致性",
              ],
              [
                "证据忠实度",
                fmtNum(am.faithfulness),
                "%",
                "陈述是否有证据支持",
              ],
              [
                "引用准确率",
                fmtNum(am.citation_precision),
                "%",
                "引用是否支撑对应陈述",
              ],
              [
                "语义评审覆盖率",
                fmtNum(am.judge_coverage),
                "%",
                "完成独立模型评审的比例",
              ],
            ]
          : [
              [
                "检索命中率",
                fmtNum(m.hit_rate),
                "%",
                "Hit@" + (m.eval_k || "K") + " · 目标文档命中",
              ],
              ["排序质量", fmtNum(m.mrr, 3), "", "MRR · 首个正确结果排名"],
              [
                "上下文命中率",
                fmtNum(m.answer_in_context),
                "%",
                "答案是否出现在上下文",
              ],
              ["检索 P95", fmtMs(m.search_p95_ms), "", "95% 的检索请求耗时"],
            ];
        $("#r-highlights").innerHTML = run.metrics
          ? cards
              .map(function (c) {
                return (
                  '<div class="highlight"><div class="k">' +
                  esc(c[0]) +
                  '</div><div class="v">' +
                  esc(c[1]) +
                  (c[1] === "—"
                    ? ""
                    : '<span class="u">' + esc(c[2]) + "</span>") +
                  '</div><div class="caption">' +
                  esc(c[3]) +
                  "</div></div>"
                );
              })
              .join("")
          : "";
        $("#r-highlights").hidden = !run.metrics;
      }
      function metricValue(v, unit) {
        return v == null
          ? "—"
          : unit === "ms"
            ? fmtMs(v)
            : unit === "%"
              ? fmtNum(v) + "%"
              : fmtNum(v, 3);
      }
      function renderGates(m) {
        var all = m.gates || [],
          active = all.filter(function (g) {
            return g.threshold != null;
          }),
          failed = active.filter(function (g) {
            return !g.passed;
          });
        var filtered = gateOnlyFailed ? failed : all;
        $("#m-gates").innerHTML =
          '<div class="result-block"><div class="block-heading"><div><h2>验收门禁</h2><p>' +
          active.length +
          " 项已启用 · " +
          failed.length +
          ' 项未通过</p></div><button id="gate-filter" class="filter-button" aria-pressed="' +
          gateOnlyFailed +
          '">仅看未通过</button></div><div class="tbl-wrap"><table class="gate-table"><thead><tr><th>验收项</th><th class="num">实测值</th><th class="num">验收要求</th><th class="c">结果</th><th>说明</th></tr></thead><tbody>' +
          filtered
            .map(function (g) {
              return (
                '<tr><td class="gate-name" title="' +
                esc(g.metric) +
                '">' +
                esc(g.name) +
                '</td><td class="num gate-value">' +
                esc(metricValue(g.actual, g.unit)) +
                '</td><td class="num gate-requirement">' +
                (g.threshold == null
                  ? "未启用"
                  : esc(
                      g.comparator + " " + metricValue(g.threshold, g.unit),
                    )) +
                '</td><td class="c">' +
                (g.threshold == null
                  ? badge("na", "未启用")
                  : g.passed
                    ? badge("ok", "通过")
                    : badge("bad", "未通过")) +
                '</td><td class="gate-note">' +
                esc(g.note || "—") +
                "</td></tr>"
              );
            })
            .join("") +
          (filtered.length
            ? ""
            : '<tr><td colspan="5" class="table-empty">' +
              (gateOnlyFailed ? "没有未通过的验收门禁" : "暂无验收门禁") +
              "</td></tr>") +
          "</tbody></table></div></div>";
        $("#gate-filter").onclick = function () {
          gateOnlyFailed = !gateOnlyFailed;
          renderGates(m);
          $("#gate-filter").focus({ preventScroll: true });
        };
      }
      function renderAnswers(a) {
        if (!a) {
          $("#m-answers").innerHTML = "";
          return;
        }
        var rows = a.rows || [],
          failed = rows.filter(function (r) {
            return !r.passed;
          }),
          filtered = answerOnlyFailed ? failed : rows;
        var semantic =
          {
            complete: "语义评审已完成",
            incomplete: "语义评审未完整完成",
            not_evaluated: "语义未评估",
          }[a.semantic_status] || a.semantic_status;
        $("#m-answers").innerHTML =
          '<div class="result-block"><div class="block-heading"><div><h2>逐题答案评估</h2><p>' +
          rows.length +
          " 条结果 · " +
          esc(semantic) +
          " · " +
          failed.length +
          ' 条未通过</p></div><button id="answer-filter" class="filter-button" aria-pressed="' +
          answerOnlyFailed +
          '">仅看未通过</button></div><div class="tbl-wrap"><table class="answer-table"><thead><tr><th>用例</th><th>次 / 轮</th><th>结果</th><th>诊断</th></tr></thead><tbody>' +
          filtered
            .map(function (r) {
              return (
                '<tr><td class="mono answer-case">' +
                esc(r.case_id) +
                '</td><td class="mono">' +
                esc(r.repeat) +
                " / " +
                esc(r.turn) +
                "</td><td>" +
                (r.passed ? badge("ok", "通过") : badge("bad", "未通过")) +
                '</td><td class="diagnosis">' +
                esc(r.diagnosis) +
                "</td></tr>"
              );
            })
            .join("") +
          (filtered.length
            ? ""
            : '<tr><td colspan="4" class="table-empty">' +
              (answerOnlyFailed ? "没有未通过的用例" : "暂无逐题评估结果") +
              "</td></tr>") +
          '</tbody></table></div><p class="field-help">完整答案、原文证据与评分依据可在完整报告中查看。</p></div>';
        $("#answer-filter").onclick = function () {
          answerOnlyFailed = !answerOnlyFailed;
          renderAnswers(a);
          $("#answer-filter").focus({ preventScroll: true });
        };
      }
      function renderMetrics(run) {
        var m = run.metrics;
        if (!m) {
          $("#m-gates").innerHTML =
            '<p class="muted">' +
            (run.status === "running"
              ? "评测进行中，指标将在结束后汇总。可先查看运行日志。"
              : "本次运行未生成指标，请查看日志与运行提醒。") +
            "</p>";
          ["#m-cards", "#m-docs", "#m-answers", "#m-hints"].forEach(
            function (s) {
              $(s).innerHTML = "";
            },
          );
          $("#all-metrics").hidden = true;
          return;
        }
        $("#all-metrics").hidden = false;
        var cards = [
          ["解析成功率", fmtNum(m.parse_success_rate), "%"],
          ["解析 P95", fmtMs(m.parse_p95_ms), ""],
          ["切片总数", m.total_slices == null ? "—" : m.total_slices, ""],
        ];
        if (m.mode !== "chat") {
          cards = cards.concat([
            ["Hit@" + (m.eval_k || "K"), fmtNum(m.hit_rate), "%"],
            ["MRR", fmtNum(m.mrr, 3), ""],
            ["nDCG", fmtNum(m.ndcg, 3), ""],
            ["上下文命中率", fmtNum(m.answer_in_context), "%"],
            ["负样本通过率", fmtNum(m.negative_pass_rate), "%"],
            ["检索 P95", fmtMs(m.search_p95_ms), ""],
          ]);
        }
        if (m.answers) {
          var am = m.answers.metrics;
          cards = cards.concat([
            ["答案正确率", fmtNum(am.correctness), "%"],
            ["事实覆盖率", fmtNum(am.fact_completeness), "%"],
            ["证据忠实度", fmtNum(am.faithfulness), "%"],
            ["引用准确率", fmtNum(am.citation_precision), "%"],
            ["引用覆盖率", fmtNum(am.citation_coverage), "%"],
            ["正确处理无答案题", fmtNum(am.abstention_rate), "%"],
            ["语义评审覆盖率", fmtNum(am.judge_coverage), "%"],
            ["对话总耗时 P95", fmtMs((am.latency || {}).p95), ""],
            ["首段文本 P95", fmtMs((am.first_text_latency || {}).p95), ""],
          ]);
        }
        $("#m-cards").innerHTML = cards
          .map(function (c) {
            return (
              '<div class="card"><div class="k">' +
              esc(c[0]) +
              '</div><div class="v">' +
              esc(c[1]) +
              (c[1] === "—" ? "" : '<span class="u">' + esc(c[2]) + "</span>") +
              "</div></div>"
            );
          })
          .join("");
        renderGates(m);
        renderAnswers(m.answers);
        var docs = m.documents || [],
          stages = {
            completed: ["ok", "解析完成"],
            skipped: ["warn", "跳过"],
            parse_failed: ["bad", "解析失败"],
            parse_timeout: ["bad", "解析超时"],
            upload_failed: ["bad", "上传失败"],
            download_failed: ["bad", "语料获取失败"],
          };
        $("#m-docs").innerHTML = docs.length
          ? '<details class="details-block"><summary>文档解析明细<span>' +
            docs.length +
            " 份文档 · " +
            (m.total_slices == null ? "—" : esc(m.total_slices)) +
            ' 个切片</span></summary><div class="tbl-wrap"><table class="doc-table"><thead><tr><th>文档</th><th>状态</th><th class="num">切片</th><th class="num">上传耗时</th><th class="num">解析耗时</th><th>说明</th></tr></thead><tbody>' +
            docs
              .map(function (d) {
                var st = stages[d.stage] || ["na", d.stage];
                return (
                  '<tr><td><div class="mono">' +
                  esc(d.doc_id) +
                  '</div><div class="file-name">' +
                  esc(d.filename) +
                  "</div></td><td>" +
                  badge(st[0], st[1]) +
                  '</td><td class="num">' +
                  (d.slices == null ? "—" : esc(d.slices)) +
                  '</td><td class="num">' +
                  fmtMs(d.upload_ms) +
                  '</td><td class="num">' +
                  fmtMs(d.parse_ms) +
                  '</td><td class="gate-note">' +
                  esc(d.error || "—") +
                  "</td></tr>"
                );
              })
              .join("") +
            "</tbody></table></div></details>"
          : "";
        var hints = m.hints || [];
        $("#m-hints").innerHTML = hints.length
          ? '<details class="callout warn"><summary>排查建议 · ' +
            hints.length +
            " 项</summary><ul>" +
            hints
              .map(function (h) {
                return "<li>" + esc(h) + "</li>";
              })
              .join("") +
            "</ul></details>"
          : "";
      }
      function switchTab(name) {
        if (["log", "metrics", "report"].indexOf(name) < 0) return;
        document.querySelectorAll(".tabs button").forEach(function (b) {
          var active = b.dataset.tab === name;
          b.classList.toggle("active", active);
          b.setAttribute("aria-selected", String(active));
          b.tabIndex = active ? 0 : -1;
        });
        document.querySelectorAll(".pane").forEach(function (p) {
          p.classList.toggle("active", p.id === "pane-" + name);
        });
        if (name === "report" && currentRun) {
          var url = "/api/runs/" + encodeURIComponent(currentRun) + "/report";
          if ($("#report-frame").getAttribute("src") !== url)
            $("#report-frame").src = url;
        }
        if (currentRun) {
          var query = new URLSearchParams(location.search);
          query.set("run", currentRun);
          query.set("tab", name);
          history.replaceState(null, "", "?" + query.toString());
        }
      }
      function docIds(value) {
        return String(value || "")
          .split(",")
          .map(function (s) {
            return s.trim();
          })
          .filter(Boolean);
      }
      function syncDocChips() {
        var cur = docIds($("#f-docs").value);
        document.querySelectorAll("#doc-chips .chip-doc").forEach(function (b) {
          var on = cur.indexOf(b.dataset.doc) >= 0;
          b.classList.toggle("on", on);
          b.setAttribute("aria-pressed", String(on));
        });
      }
      function renderCorpus(data) {
        if (!data || !data.available || !data.documents.length) return;
        $("#doc-chips").innerHTML = data.documents
          .map(function (d) {
            return (
              '<button type="button" class="chip-doc" data-doc="' +
              esc(d.id) +
              '" aria-pressed="false" title="' +
              esc((d.description || "") + " · " + (d.filename || "")) +
              '">' +
              esc(d.id) +
              (d.optional ? " · 可选" : "") +
              "</button>"
            );
          })
          .join("");
        document.querySelectorAll("#doc-chips .chip-doc").forEach(function (b) {
          b.onclick = function () {
            var cur = docIds($("#f-docs").value),
              i = cur.indexOf(b.dataset.doc);
            if (i >= 0) cur.splice(i, 1);
            else cur.push(b.dataset.doc);
            $("#f-docs").value = cur.join(",");
            syncDocChips();
          };
        });
        syncDocChips();
      }
      function renderHistory() {
        var filter = $("#hist-search").value.trim().toLowerCase(),
          filtered = runs.filter(function (r) {
            return (
              !filter ||
              [r.id, r.target, modeNames[(r.params || {}).mode || "retrieval"]]
                .join(" ")
                .toLowerCase()
                .indexOf(filter) >= 0
            );
          });
        $("#hist-count").textContent = filter
          ? filtered.length + " / " + runs.length
          : runs.length + " 次";
        $("#hist-body").innerHTML =
          filtered
            .map(function (r) {
              var m = r.metrics || {},
                mode = (r.params || {}).mode || "retrieval",
                am = m.answers && m.answers.metrics,
                quality =
                  mode !== "retrieval" && am ? am.correctness : m.hit_rate,
                p95 =
                  mode !== "retrieval" && am
                    ? (am.latency || {}).p95
                    : m.search_p95_ms;
              return (
                '<tr class="clickable' +
                (r.id === currentRun ? " selected" : "") +
                '" data-id="' +
                esc(r.id) +
                '"' +
                (r.id === currentRun ? ' aria-current="true"' : "") +
                '><td><button class="history-open" data-run="' +
                esc(r.id) +
                '" aria-label="查看 ' +
                esc(r.target + " " + dateLabel(r.started_at) + " 的评测结果") +
                '">' +
                esc(r.target) +
                '</button><div class="history-time mono">' +
                esc(dateLabel(r.started_at)) +
                '</div></td><td class="mode-label">' +
                esc(modeNames[mode]) +
                "</td><td>" +
                statusBadge(r.status) +
                '</td><td class="num" title="' +
                (mode !== "retrieval" && am ? "答案正确率" : "检索命中率") +
                '">' +
                fmtNum(quality) +
                (quality == null ? "" : '<span class="muted">%</span>') +
                '</td><td class="num mono">' +
                fmtMs(p95) +
                "</td></tr>"
              );
            })
            .join("") ||
          '<tr><td colspan="5" class="table-empty">' +
            (filter
              ? "没有匹配的运行记录"
              : "暂无运行记录，从左侧开始第一次评测") +
            "</td></tr>";
        document
          .querySelectorAll("#hist-body tr.clickable")
          .forEach(function (tr) {
            tr.onclick = function () {
              selectRun(tr.dataset.id);
            };
          });
      }
      function refreshList() {
        return api("/api/runs")
          .then(function (d) {
            runs = d.runs;
            $("#info-concurrency").textContent = d.running
              ? "运行中 " + d.running + " / " + d.max_concurrent
              : "暂无运行任务";
            $("#service-status").classList.add("ready");
            $("#service-status").title =
              "最多同时运行 " + d.max_concurrent + " 个评测";
            $("#overview-count").textContent = runs.length;
            $("#overview-latest").textContent = runs.length
              ? "最近运行 " + dateLabel(runs[0].started_at)
              : "建立你的质量基线";
            renderHistory();
            if (initialList) {
              initialList = false;
              var qs = new URLSearchParams(location.search),
                id = qs.get("run"),
                tab = qs.get("tab");
              if (id) selectRun(id, tab);
              else if (runs.length) selectRun(runs[0].id);
              if (runs.length && window.innerWidth <= 820)
                setConfigExpanded(false);
            }
          })
          .catch(function (e) {
            $("#service-status").classList.remove("ready");
            $("#info-concurrency").textContent = "连接暂时中断";
            if (initialList) {
              $("#hist-body").innerHTML =
                '<tr><td colspan="5" class="table-empty">无法读取记录，将自动重试</td></tr>';
              toast("读取运行记录失败：" + e.message, true);
            }
          });
      }
      function init() {
        api("/api/info")
          .then(function (info) {
            $("#judge-status").textContent = info.judge_configured
              ? "已配置独立评审模型 · " + info.judge_model
              : "尚未配置评审模型，请展开下方选项填写。";
          })
          .catch(function () {
            $("#judge-status").textContent = "暂时无法读取评审配置";
          });
        api("/api/corpus")
          .then(renderCorpus)
          .catch(function () {});
        $("#btn-start").onclick = startRun;
        $("#btn-cancel").onclick = cancelRun;
        $("#btn-new").onclick = focusConfig;
        $("#btn-empty-start").onclick = focusConfig;
        $("#btn-config").onclick = function () {
          setConfigExpanded($("#form-content").hidden);
        };
        $("#btn-report").onclick = function () {
          window.open(
            "/api/runs/" + encodeURIComponent(currentRun) + "/report",
            "_blank",
            "noopener",
          );
        };
        $("#btn-json").onclick = function () {
          window.open(
            "/api/runs/" + encodeURIComponent(currentRun) + "/json",
            "_blank",
            "noopener",
          );
        };
        $("#btn-log-bottom").onclick = function () {
          var el = $("#log");
          el.scrollTop = el.scrollHeight;
        };
        document.querySelectorAll("[data-mode]").forEach(function (b) {
          b.onclick = function () {
            $("#f-mode").value = b.dataset.mode;
            updateMode();
          };
        });
        document.querySelectorAll(".tabs button").forEach(function (b) {
          b.onclick = function () {
            switchTab(b.dataset.tab);
          };
          b.onkeydown = function (e) {
            var tabs = Array.from(document.querySelectorAll(".tabs button")),
              i = tabs.indexOf(b),
              next =
                e.key === "ArrowRight"
                  ? (i + 1) % tabs.length
                  : e.key === "ArrowLeft"
                    ? (i + tabs.length - 1) % tabs.length
                    : e.key === "Home"
                      ? 0
                      : e.key === "End"
                        ? tabs.length - 1
                        : null;
            if (next != null) {
              e.preventDefault();
              switchTab(tabs[next].dataset.tab);
              tabs[next].focus();
            }
          };
        });
        $("#f-target").addEventListener("keydown", function (e) {
          if (e.key === "Enter") startRun();
        });
        $("#f-mode").onchange = updateMode;
        $("#f-docs").addEventListener("input", syncDocChips);
        $("#hist-search").addEventListener("input", renderHistory);
        updateMode();
        refreshList();
        listTimer = setInterval(refreshList, 5000);
      }
      init();
    </script>
  </body>
</html>
"""


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def listen_urls(bind: str, port: int) -> List[str]:
    """给出可以在浏览器里打开的地址；绑定 0.0.0.0 时额外猜测局域网地址。"""
    if bind not in ("", "0.0.0.0", "::", "*"):
        host = f"[{bind}]" if ":" in bind else bind
        return [f"http://{host}:{port}"]
    urls = [f"http://127.0.0.1:{port}"]
    probe_ip: Optional[str] = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            probe_ip = probe.getsockname()[0]
    except OSError:
        probe_ip = None
    if probe_ip is None:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                probe_ip = info[4][0]
                break
        except OSError:
            probe_ip = None
    if probe_ip:
        urls.append(f"http://{probe_ip}:{port}")
    return urls


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="e2e_web.py",
        description="HTKnow 端到端检索测试 Web 控制台：浏览器里输入 HOST:PORT 即可发起全链路测试。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--bind", default="127.0.0.1",
                        help="监听地址；填 0.0.0.0 允许同网段其他机器访问（控制台无鉴权，请自行评估）")
    parser.add_argument("--port", type=int, default=8099, help="监听端口")
    parser.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR),
                        help="运行产物目录，保存每次测试的日志与 HTML/JSON 报告")
    parser.add_argument("--max-concurrent", type=int, default=2, help="同时运行的测试数量上限")
    parser.add_argument("--max-history", type=int, default=50,
                        help="启动时最多保留的历史运行条数，超出部分连同产物目录一起删除；0 表示不清理")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    global MANAGER

    args = parse_args(argv)
    from e2e_judge import JudgeError, judge_settings
    try:
        settings = judge_settings()
    except JudgeError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    if not TEST_SCRIPT.exists():
        print(f"[错误] 找不到测试脚本：{TEST_SCRIPT}", file=sys.stderr)
        print("       请确认 e2e_web.py 与 e2e_retrieval_test.py 位于同一目录。", file=sys.stderr)
        return 2

    runs_dir = Path(args.runs_dir).expanduser().resolve()
    try:
        MANAGER = RunManager(runs_dir, max(1, args.max_concurrent))
    except OSError as exc:
        print(f"[错误] 无法创建产物目录 {runs_dir}: {exc}", file=sys.stderr)
        return 2
    pruned = MANAGER.prune(args.max_history)

    SERVER_INFO.update({
        "service": "htknow-e2e-web",
        "version": "1.0",
        "test_script": str(TEST_SCRIPT),
        "script_dir": str(SCRIPT_DIR),
        "runs_dir": str(runs_dir),
        "max_concurrent": MANAGER.max_concurrent,
        "max_history": args.max_history,
        "python": sys.version.split()[0],
        "history": len(MANAGER.runs),
        "judge_configured": bool(settings["E2E_JUDGE_URL"] and settings["E2E_JUDGE_MODEL"]),
        "judge_model": settings["E2E_JUDGE_MODEL"] or None,
        "defaults": DEFAULT_PARAMS,
    })

    try:
        httpd = ThreadingHTTPServer((args.bind, args.port), Handler)
    except OSError as exc:
        print(f"[错误] 监听 {args.bind}:{args.port} 失败：{exc}", file=sys.stderr)
        if getattr(exc, "errno", None) == 98:
            print("       端口可能已被占用，换个端口试试：--port 8100", file=sys.stderr)
        return 2
    httpd.daemon_threads = True

    def _on_term(_signum: int, _frame: Any) -> None:
        # 让 kill/systemd stop 也走优雅退出：终止子进程并落盘状态
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_term)

    urls = listen_urls(args.bind, args.port)
    print("=" * 72)
    print("HTKnow 端到端检索测试 Web 控制台")
    print("=" * 72)
    print(f"  访问地址 : {urls[0]}" + (f"   （局域网：{urls[1]}）" if len(urls) > 1 else ""))
    print(f"  测试脚本 : {TEST_SCRIPT}")
    print(f"  产物目录 : {runs_dir}")
    print(f"  并发上限 : {MANAGER.max_concurrent}    历史运行 : {len(MANAGER.runs)} 条"
          + (f"（已清理 {pruned} 条）" if pruned else ""))
    if args.bind in ("0.0.0.0", "::", "*"):
        print("  ⚠ 正在监听所有网卡：控制台没有鉴权，任何能访问该端口的人都能发起测试。")
    print("  使用方式 : 打开页面 → 输入目标 HOST:PORT（如 192.168.0.46:11000）→ 开始测试")
    print("  停止服务 : Ctrl-C（会同时终止正在运行的测试子进程）")
    print("=" * 72, flush=True)

    try:
        httpd.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n收到停止信号，正在关闭…")
    finally:
        stopped = MANAGER.shutdown()
        httpd.server_close()
        if stopped:
            print(f"已终止 {stopped} 个运行中的测试。")
        print("服务已退出。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
