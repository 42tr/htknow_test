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
}
TEXT_OPTIONS = {"user_id", "role", "user_name", "docs", "exclude", "slice_type",
                "cleanup", "upload_mode", "corpus_cache"}
BOOL_OPTIONS = {"offline", "strict_integrity", "public", "refresh_corpus",
                "allow_negative_failure", "unique_marker"}
CHOICES = {
    "slice_type": {"smart", "fixed", "paragraph"},
    "cleanup": {"keep", "delete", "delete-on-pass"},
    "upload_mode": {"sequential", "batch"},
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
        target = str(params.get("target") or "").strip()
        if not target:
            raise ValueError("必须填写目标地址 HOST:PORT")
        if not TARGET_RE.match(target):
            raise ValueError("目标地址只能包含字母、数字、. - : [ ]")
        with self.lock:
            if self.running_count() >= self.max_concurrent:
                raise RuntimeError(f"已有 {self.max_concurrent} 个测试在运行，请等待完成或取消后再试")
            run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
            run_dir = self.runs_dir / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            argv = build_argv(target, params, run_dir)
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


def build_argv(target: str, params: Dict[str, Any], run_dir: Path) -> List[str]:
    argv: List[str] = [str(TEST_SCRIPT), target,
                       "--report", str(run_dir / "report.html"),
                       "--json-report", str(run_dir / "report.json")]
    for key in ("user_id", "role", "user_name", "docs", "exclude", "corpus_cache"):
        value = params.get(key)
        if value:
            argv += [f"--{key.replace('_', '-')}", str(value)]
    for key in ("slice_type", "cleanup", "upload_mode"):
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
                    self._send(200, target.read_bytes(), ctype)
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
                params[key] = max(lo, min(hi, cast(float(raw[key]))))
            except (TypeError, ValueError):
                raise ValueError(f"{key} 不是合法数字") from None
    for key in BOOL_OPTIONS:
        if key in raw:
            params[key] = bool(raw[key])
    return params


UI_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HTKnow 端到端检索测试控制台</title>
<style>
:root{--bg:#f4f6f9;--card:#fff;--ink:#0f172a;--muted:#64748b;--line:#e2e8f0;
--ok:#15803d;--okbg:#dcfce7;--bad:#b91c1c;--badbg:#fee2e2;--warn:#b45309;--warnbg:#fef3c7;
--na:#475569;--nabg:#e2e8f0;--accent:#2563eb;--accentbg:#dbeafe}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Hiragino Sans GB','Microsoft YaHei',Helvetica,Arial,sans-serif}
header{background:linear-gradient(135deg,#0f172a,#1e3a8a);color:#fff;padding:16px 24px;
display:flex;align-items:center;gap:16px;flex-wrap:wrap;box-shadow:0 4px 18px rgba(15,23,42,.18)}
header h1{margin:0;font-size:18px;letter-spacing:.3px}
header .sub{opacity:.8;font-size:12.5px}
header .right{margin-left:auto;display:flex;gap:8px;align-items:center;font-size:12.5px}
header .chip{background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.22);
padding:3px 10px;border-radius:999px}
main{max-width:1400px;margin:0 auto;padding:20px;display:grid;
grid-template-columns:400px 1fr;gap:18px;align-items:start}
@media (max-width:980px){main{grid-template-columns:1fr}}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:16px 18px;box-shadow:0 1px 2px rgba(15,23,42,.05)}
section+section{margin-top:16px}
h2{margin:0 0 10px;font-size:15px}
h2 .hint{font-weight:400;color:var(--muted);font-size:12px;margin-left:6px}
label{display:block;font-size:12.5px;color:#334155;margin:10px 0 4px}
label .req{color:var(--bad)}
input[type=text],input[type=number],select{width:100%;padding:7px 9px;border:1px solid var(--line);
border-radius:8px;font:inherit;background:#fcfdff;color:var(--ink)}
input:focus,select:focus{outline:2px solid var(--accentbg);border-color:var(--accent)}
.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.check{display:flex;gap:8px;align-items:center;margin:10px 0 2px;font-size:13px;color:#334155}
.check input{width:auto;margin:0}
button{font:inherit;cursor:pointer;border-radius:8px;border:1px solid var(--line);
background:#fff;padding:7px 14px;color:#334155}
button:hover{background:#f8fafc}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
button.primary:hover{background:#1d4ed8}
button.danger{background:#fff;border-color:#fecaca;color:var(--bad)}
button.danger:hover{background:var(--badbg)}
button:disabled{opacity:.5;cursor:not-allowed}
details.adv{margin-top:12px;border:1px solid var(--line);border-radius:9px;padding:4px 12px 10px}
details.adv>summary{cursor:pointer;font-size:12.5px;color:var(--muted);padding:6px 0;list-style:none}
details.adv>summary::-webkit-details-marker{display:none}
details.adv>summary::before{content:'▸ ';}
details.adv[open]>summary::before{content:'▾ ';}
.badge{display:inline-block;padding:1px 9px;border-radius:999px;font-size:11.5px;font-weight:600;
border:1px solid transparent;white-space:nowrap}
.badge.ok{color:var(--ok);background:var(--okbg);border-color:#bbf7d0}
.badge.bad{color:var(--bad);background:var(--badbg);border-color:#fecaca}
.badge.warn{color:var(--warn);background:var(--warnbg);border-color:#fde68a}
.badge.na{color:var(--na);background:var(--nabg);border-color:#cbd5e1}
.badge.run{color:#1d4ed8;background:var(--accentbg);border-color:#bfdbfe}
.runhead{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.runhead .target{font-family:ui-monospace,Menlo,Consolas,monospace;font-weight:600}
.runhead .meta{color:var(--muted);font-size:12px}
.runhead .actions{margin-left:auto;display:flex;gap:8px}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin:10px 0 0}
.tabs button{border:none;border-radius:8px 8px 0 0;background:transparent;padding:8px 14px;
color:var(--muted);font-weight:600}
.tabs button.active{color:var(--accent);background:var(--accentbg)}
.pane{display:none;padding-top:12px}
.pane.active{display:block}
pre.log{background:#0b1220;color:#d7e3f4;border-radius:10px;padding:12px 14px;height:420px;
overflow:auto;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
white-space:pre-wrap;word-break:break-word;margin:0}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:12px}
.card{border:1px solid var(--line);border-radius:10px;padding:10px 12px;background:#fcfdff}
.card .k{color:var(--muted);font-size:11.5px;margin-bottom:3px}
.card .v{font-size:19px;font-weight:700}
.card .u{font-size:11px;color:var(--muted);font-weight:400;margin-left:2px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{background:#f8fafc;color:#475569;font-weight:600;font-size:11.5px}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.c,th.c{text-align:center}
tbody tr:nth-child(even){background:#fbfcfe}
tbody tr.clickable{cursor:pointer}
tbody tr.clickable:hover{background:var(--accentbg)}
tbody tr.selected{background:var(--accentbg)}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11.5px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:7px}
.tbl-wrap{overflow-x:auto}
#hist-table{font-size:11.5px;table-layout:fixed;width:100%}
#hist-table th,#hist-table td{padding:5px 6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#hist-table th:nth-child(1),#hist-table td:nth-child(1){width:88px}
#hist-table th:nth-child(2),#hist-table td:nth-child(2){width:108px}
#hist-table th:nth-child(3),#hist-table td:nth-child(3){width:50px}
#hist-table th:nth-child(4),#hist-table td:nth-child(4){width:52px}
#hist-table th:nth-child(5),#hist-table td:nth-child(5){width:58px}
.chip-doc{border:1px solid var(--line);background:#fcfdff;border-radius:999px;padding:1px 10px;
font:inherit;font-size:11.5px;color:#475569;cursor:pointer;line-height:1.8}
.chip-doc:hover{border-color:#bfdbfe;color:#1d4ed8;background:#f8fbff}
.chip-doc.on{background:var(--accentbg);border-color:var(--accent);color:#1d4ed8;font-weight:600}
.muted{color:var(--muted);font-size:12px}
.callout{border-radius:9px;padding:9px 12px;margin:10px 0;font-size:12.5px;border:1px solid}
.callout.bad{background:var(--badbg);border-color:#fecaca;color:#7f1d1d}
.callout.warn{background:var(--warnbg);border-color:#fde68a;color:#78350f}
.callout.ok{background:var(--okbg);border-color:#bbf7d0;color:#14532d}
ul.tight{margin:6px 0 0;padding-left:18px}ul.tight li{margin:3px 0}
iframe.report{width:100%;height:720px;border:1px solid var(--line);border-radius:10px;background:#fff}
#toast{position:fixed;right:18px;bottom:18px;background:#0f172a;color:#fff;padding:10px 16px;
border-radius:10px;font-size:13px;opacity:0;transform:translateY(8px);transition:.25s;
pointer-events:none;max-width:420px;box-shadow:0 8px 24px rgba(15,23,42,.3);z-index:50}
#toast.show{opacity:1;transform:none}
#toast.err{background:#b91c1c}
.spin{display:inline-block;width:10px;height:10px;border:2px solid #93c5fd;border-top-color:transparent;
border-radius:50%;animation:sp 0.8s linear infinite;margin-right:6px;vertical-align:-1px}
@keyframes sp{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<header>
  <div>
    <h1>HTKnow 端到端检索测试控制台</h1>
    <div class="sub">建库 → 上传 → 解析 → 检索 → 指标评估 → 报告，全流程可视化</div>
  </div>
  <div class="right">
    <span class="chip" id="info-concurrency">-</span>
    <span class="chip" id="info-script">-</span>
  </div>
</header>
<main>
  <div>
    <section>
      <h2>发起测试</h2>
      <label>目标地址 <span class="req">*</span></label>
      <input type="text" id="f-target" placeholder="192.168.0.46:11000" autocomplete="off" spellcheck="false">
      <div class="muted" style="margin-top:4px">HOST:PORT 形式；也接受 http://host:port</div>
      <div class="row">
        <div><label>切片方式</label>
          <select id="f-slice_type"><option value="smart">smart 智能切片</option>
          <option value="fixed">fixed 固定长度</option>
          <option value="paragraph">paragraph 按段落</option></select></div>
        <div><label>上传模式</label>
          <select id="f-upload_mode"><option value="sequential">sequential 逐个等待</option>
          <option value="batch">batch 先全传再等</option></select></div>
      </div>
      <div class="row">
        <div><label>问题重复次数</label><input type="number" id="f-repeats" min="1" max="20" value="3"></div>
        <div><label>评测 Top-K</label><input type="number" id="f-top_k" min="1" max="50" value="10"></div>
      </div>
      <div class="row">
        <div><label>解析超时(秒)</label><input type="number" id="f-parse_timeout" min="10" value="1800"></div>
        <div><label>知识库处置</label>
          <select id="f-cleanup"><option value="delete-on-pass">通过即删</option>
          <option value="keep">保留</option><option value="delete">总是删除</option></select></div>
      </div>
      <label class="check"><input type="checkbox" id="f-offline"> 离线模式（只用本地语料缓存，不联网下载）</label>
      <details class="adv">
        <summary>高级选项（认证 / 语料筛选 / 门禁阈值）</summary>
        <div class="row">
          <div><label>x-user-id</label><input type="text" id="f-user_id" value="1"></div>
          <div><label>x-role</label><input type="text" id="f-role" value="admin"></div>
        </div>
        <label>只测文档（逗号分隔，留空=全部）</label>
        <input type="text" id="f-docs" placeholder="art_of_war,needle_synthetic">
        <div class="chips" id="doc-chips"></div>
        <label>排除文档（逗号分隔）</label>
        <input type="text" id="f-exclude" placeholder="calibre_docx_demo">
        <div class="row">
          <div><label>Hit@K 下限 %</label><input type="number" id="f-min_hit_rate" value="80" step="1"></div>
          <div><label>MRR 下限</label><input type="number" id="f-min_mrr" value="0.6" step="0.05"></div>
        </div>
        <div class="row">
          <div><label>nDCG 下限</label><input type="number" id="f-min_ndcg" value="0.4" step="0.05"></div>
          <div><label>答案在上下文 %</label><input type="number" id="f-min_answer_in_context" value="70" step="1"></div>
        </div>
        <div class="row">
          <div><label>解析成功率 %</label><input type="number" id="f-min_parse_success" value="100" step="1"></div>
          <div><label>检索 P95 上限 ms</label><input type="number" id="f-max_p95_ms" value="5000" step="100"></div>
        </div>
        <label class="check"><input type="checkbox" id="f-public"> 创建公开知识库</label>
        <label class="check"><input type="checkbox" id="f-strict_integrity"> SHA-256 不匹配即失败</label>
        <label class="check"><input type="checkbox" id="f-allow_negative_failure"> 负样本泄漏仅告警</label>
      </details>
      <div style="margin-top:14px;display:flex;gap:10px">
        <button class="primary" id="btn-start" style="flex:1">开始测试</button>
      </div>
    </section>
    <section>
      <h2>历史运行 <span class="hint" id="hist-count"></span></h2>
      <div class="tbl-wrap"><table id="hist-table"><thead><tr><th>开始时间</th><th>目标</th><th class="c">状态</th>
      <th class="num">Hit@K</th><th class="num">P95</th></tr></thead>
      <tbody id="hist-body"></tbody></table></div>
    </section>
  </div>
  <div>
    <section id="run-section" style="display:none">
      <div class="runhead">
        <span class="badge na" id="r-status">-</span>
        <span class="target" id="r-target">-</span>
        <span class="meta" id="r-meta">-</span>
        <span class="actions">
          <button id="btn-cancel" class="danger">取消</button>
          <button id="btn-report" disabled>打开报告</button>
          <button id="btn-json" disabled>下载 JSON</button>
        </span>
      </div>
      <div id="r-warn"></div>
      <div class="tabs">
        <button data-tab="log" class="active">实时日志</button>
        <button data-tab="metrics">门禁与指标</button>
        <button data-tab="report">报告</button>
      </div>
      <div class="pane active" id="pane-log"><pre class="log" id="log"></pre></div>
      <div class="pane" id="pane-metrics">
        <div class="cards" id="m-cards"></div>
        <div id="m-gates"></div>
        <div id="m-docs"></div>
        <div id="m-hints"></div>
      </div>
      <div class="pane" id="pane-report"><iframe class="report" id="report-frame" src="about:blank"></iframe></div>
    </section>
    <section id="empty-section">
      <h2>还没有运行</h2>
      <p class="muted">在左侧填写目标地址（例如 <span class="mono">192.168.0.46:11000</span>）后点击「开始测试」。
      测试会在目标部署上创建一次性知识库、上传公开评测语料、等待解析并执行带 ground truth 的检索评估，
      结束后生成自包含 HTML 报告。历史运行会保留在下方列表中。</p>
    </section>
  </div>
</main>
<div id="toast"></div>
<script>
'use strict';
var $ = function(s){ return document.querySelector(s); };
var currentRun = null, logOffset = 0, logTimer = null, listTimer = null, lastDone = null;

function toast(msg, isErr){
  var el = $('#toast');
  el.textContent = msg;
  el.className = 'show' + (isErr ? ' err' : '');
  clearTimeout(el._t);
  el._t = setTimeout(function(){ el.className = ''; }, 4200);
}
function api(path, opts){
  return fetch(path, opts).then(function(r){
    return r.json().then(function(j){
      if(!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
      return j;
    });
  });
}
function esc(s){
  return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}
function fmtMs(v){
  if(v == null) return '-';
  if(v < 1000) return Math.round(v) + ' ms';
  if(v < 60000) return (v/1000).toFixed(2) + ' s';
  return Math.floor(v/60000) + 'm' + Math.round((v%60000)/1000) + 's';
}
function fmtNum(v, d){ return v == null ? '-' : Number(v).toFixed(d == null ? 1 : d); }
function statusBadge(s){
  var map = {running:'run', pass:'ok', fail:'bad', error:'bad', canceled:'warn', interrupted:'warn'};
  var label = {running:'运行中', pass:'通过', fail:'未达标', error:'异常', canceled:'已取消', interrupted:'已中断'};
  return '<span class="badge ' + (map[s]||'na') + '">' + (label[s]||s) + '</span>';
}

function collectParams(){
  var p = { target: $('#f-target').value.trim() };
  ['slice_type','upload_mode','cleanup','user_id','role','docs','exclude'].forEach(function(k){
    var v = $('#f-' + k).value.trim();
    if(v) p[k] = v;
  });
  ['repeats','top_k','parse_timeout','min_hit_rate','min_mrr','min_ndcg',
   'min_answer_in_context','min_parse_success','max_p95_ms'].forEach(function(k){
    var v = $('#f-' + k).value;
    if(v !== '') p[k] = Number(v);
  });
  ['offline','public','strict_integrity','allow_negative_failure'].forEach(function(k){
    p[k] = $('#f-' + k).checked;
  });
  return p;
}

function startRun(){
  var p = collectParams();
  if(!p.target){ toast('请先填写目标地址', true); $('#f-target').focus(); return; }
  $('#btn-start').disabled = true;
  api('/api/runs', {method:'POST', headers:{'Content-Type':'application/json'},
                    body: JSON.stringify(p)})
    .then(function(run){
      toast('已启动测试 ' + run.id);
      selectRun(run.id);
      refreshList();
    })
    .catch(function(e){ toast(e.message, true); })
    .then(function(){ $('#btn-start').disabled = false; });
}

function cancelRun(){
  if(!currentRun) return;
  api('/api/runs/' + currentRun + '/cancel', {method:'POST'})
    .then(function(){ toast('已取消'); pollOnce(true); refreshList(); })
    .catch(function(e){ toast(e.message, true); });
}

function selectRun(id){
  currentRun = id; logOffset = 0; lastDone = null;
  $('#log').textContent = '';
  $('#run-section').style.display = '';
  $('#empty-section').style.display = 'none';
  $('#report-frame').src = 'about:blank';
  switchTab('log');
  pollOnce(true);
  if(logTimer) clearInterval(logTimer);
  logTimer = setInterval(function(){ pollOnce(false); }, 900);
  refreshList();
}

function pollOnce(force){
  if(!currentRun) return;
  api('/api/runs/' + currentRun + '/log?offset=' + logOffset).then(function(d){
    logOffset = d.offset;
    if(d.chunk){
      var el = $('#log');
      var near = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
      el.textContent += d.chunk;
      if(near) el.scrollTop = el.scrollHeight;
    }
    return api('/api/runs/' + currentRun);
  }).then(function(run){
    renderRun(run);
    if(run.status !== 'running' && lastDone !== run.id){
      lastDone = run.id;
      renderMetrics(run);
      refreshList();
      if(run.status === 'pass') toast('测试完成：全部关键门禁达标');
      else if(run.status === 'fail') toast('测试完成：存在未达标门禁', true);
      else if(run.status === 'error') toast('测试异常结束：' + (run.error||''), true);
    }
    if(force) renderMetrics(run);
  }).catch(function(e){ /* 网络抖动时静默重试 */ });
}

function renderRun(run){
  $('#r-status').outerHTML = statusBadge(run.status).replace('class="badge', 'id="r-status" class="badge');
  $('#r-target').textContent = run.target;
  var bits = [run.id, '已用时 ' + run.elapsed_s + 's'];
  if(run.pid) bits.push('pid ' + run.pid);
  var metaEl = $('#r-meta');
  metaEl.textContent = bits.join(' · ');
  metaEl.title = run.command ? '等效命令行（可直接复制到终端重跑）：\\n' + run.command : '';
  $('#btn-cancel').disabled = run.status !== 'running';
  $('#btn-report').disabled = !run.has_report;
  $('#btn-json').disabled = !run.has_json;
  var warn = '';
  if(run.error) warn = '<div class="callout bad">' + esc(run.error) + '</div>';
  else if(run.metrics && run.metrics.warnings && run.metrics.warnings.length){
    warn = '<div class="callout warn"><strong>运行告警</strong><ul class="tight">' +
      run.metrics.warnings.map(function(w){ return '<li>' + esc(w) + '</li>'; }).join('') + '</ul></div>';
  }
  $('#r-warn').innerHTML = warn;
}

function renderMetrics(run){
  var m = run.metrics;
  if(!m){
    $('#m-cards').innerHTML = '';
    $('#m-gates').innerHTML = run.status === 'running'
      ? '<p class="muted">测试进行中，指标将在结束后展示。可先查看实时日志。</p>'
      : '<div class="callout bad">未生成指标（测试可能未跑到评估阶段），请查看日志与告警。</div>';
    $('#m-docs').innerHTML = ''; $('#m-hints').innerHTML = '';
    return;
  }
  var cards = [
    ['总体结论', m.overall_pass ? 'PASS' : 'FAIL', ''],
    ['Hit@' + (m.eval_k||'-'), fmtNum(m.hit_rate), '%'],
    ['MRR', fmtNum(m.mrr, 3), ''],
    ['nDCG', fmtNum(m.ndcg, 3), ''],
    ['答案在上下文', fmtNum(m.answer_in_context), '%'],
    ['负样本通过率', fmtNum(m.negative_pass_rate, 0), '%'],
    ['解析成功率', fmtNum(m.parse_success_rate), '%'],
    ['检索 P95', fmtMs(m.search_p95_ms), ''],
    ['解析 P95', fmtMs(m.parse_p95_ms), ''],
    ['切片总数', m.total_slices == null ? '-' : m.total_slices, '']
  ];
  $('#m-cards').innerHTML = cards.map(function(c){
    return '<div class="card"><div class="k">' + esc(c[0]) + '</div><div class="v">' +
      esc(c[1]) + '<span class="u">' + esc(c[2]) + '</span></div></div>';
  }).join('');
  var gates = (m.gates || []).map(function(g){
    var unit = g.unit === 'ms' ? fmtMs(g.actual) : (g.unit === '%' ? fmtNum(g.actual) + '%' : fmtNum(g.actual, 3));
    var th = g.threshold == null ? '未启用' : (g.unit === 'ms' ? fmtMs(g.threshold)
      : (g.unit === '%' ? fmtNum(g.threshold) + '%' : fmtNum(g.threshold, 3)));
    var st = g.threshold == null || g.actual == null ? '<span class="badge na">未启用</span>'
      : (g.passed ? '<span class="badge ok">通过</span>' : '<span class="badge bad">未通过</span>');
    return '<tr><td>' + esc(g.name) + '</td><td class="mono">' + esc(g.metric) + '</td>' +
      '<td class="num">' + esc(unit) + '</td><td class="c mono">' + esc(g.comparator) + '</td>' +
      '<td class="num">' + esc(th) + '</td><td class="c">' + st + '</td>' +
      '<td class="muted">' + esc(g.note || '') + '</td></tr>';
  }).join('');
  $('#m-gates').innerHTML = '<h2 style="margin-top:6px">验收门禁</h2><table><thead><tr>' +
    '<th>门禁项</th><th>指标</th><th class="num">实测</th><th class="c">要求</th>' +
    '<th class="num">阈值</th><th class="c">结果</th><th>备注</th></tr></thead><tbody>' +
    gates + '</tbody></table>';
  var docs = (m.documents || []).map(function(d){
    var stageMap = {completed:['ok','解析完成'], skipped:['warn','跳过'], parse_failed:['bad','解析失败'],
      parse_timeout:['bad','超时'], upload_failed:['bad','上传失败'], download_failed:['bad','语料获取失败']};
    var st = stageMap[d.stage] || ['na', d.stage];
    return '<tr><td class="mono">' + esc(d.doc_id) + '</td><td>' + esc(d.filename) + '</td>' +
      '<td class="c"><span class="badge ' + st[0] + '">' + st[1] + '</span></td>' +
      '<td class="num">' + (d.slices == null ? '-' : d.slices) + '</td>' +
      '<td class="num">' + fmtMs(d.upload_ms) + '</td><td class="num">' + fmtMs(d.parse_ms) + '</td>' +
      '<td class="muted">' + esc(d.error || '') + '</td></tr>';
  }).join('');
  $('#m-docs').innerHTML = docs ? '<h2 style="margin-top:14px">文档解析明细</h2>' +
    '<table><thead><tr><th>文档</th><th>文件</th><th class="c">状态</th><th class="num">切片</th>' +
    '<th class="num">上传</th><th class="num">解析</th><th>错误</th></tr></thead><tbody>' +
    docs + '</tbody></table>' : '';
  var hints = (m.hints || []).map(function(h){ return '<li>' + esc(h) + '</li>'; }).join('');
  $('#m-hints').innerHTML = hints ? '<div class="callout warn"><strong>排查建议</strong>' +
    '<ul class="tight">' + hints + '</ul></div>' : '';
}

function switchTab(name){
  document.querySelectorAll('.tabs button').forEach(function(b){
    b.classList.toggle('active', b.dataset.tab === name);
  });
  document.querySelectorAll('.pane').forEach(function(p){
    p.classList.toggle('active', p.id === 'pane-' + name);
  });
  if(name === 'report' && currentRun){
    $('#report-frame').src = '/api/runs/' + currentRun + '/report';
  }
}

function docIds(value){
  return String(value || '').split(',').map(function(s){ return s.trim(); }).filter(Boolean);
}

function syncDocChips(){
  var cur = docIds($('#f-docs').value);
  document.querySelectorAll('#doc-chips .chip-doc').forEach(function(b){
    b.classList.toggle('on', cur.indexOf(b.dataset.doc) >= 0);
  });
}

function toggleDoc(id){
  var cur = docIds($('#f-docs').value);
  var idx = cur.indexOf(id);
  if(idx >= 0){ cur.splice(idx, 1); } else { cur.push(id); }
  $('#f-docs').value = cur.join(',');
  syncDocChips();
}

function renderCorpus(data){
  var box = $('#doc-chips');
  if(!box || !data || !data.available || !data.documents.length){ return; }
  box.innerHTML = data.documents.map(function(d){
    var tag = d.generated ? ' · 生成' : (d.optional ? ' · 可选' : '');
    var title = (d.description || '') + '\\n文件：' + (d.filename || '-') +
      '｜格式：' + (d.format || '-') +
      '｜黄金问题：' + (d.generated ? '运行时生成' : d.queries + ' 条');
    return '<button type="button" class="chip-doc" data-doc="' + esc(d.id) + '" title="' +
      esc(title) + '">' + esc(d.id) + esc(tag) + '</button>';
  }).join('');
  box.querySelectorAll('.chip-doc').forEach(function(b){
    b.onclick = function(){ toggleDoc(b.dataset.doc); };
  });
  syncDocChips();
}

function refreshList(){
  api('/api/runs').then(function(d){
    $('#info-concurrency').textContent = '运行中 ' + d.running + ' / 上限 ' + d.max_concurrent;
    $('#hist-count').textContent = '共 ' + d.runs.length + ' 次';
    $('#hist-body').innerHTML = d.runs.map(function(r){
      var m = r.metrics || {};
      return '<tr class="clickable' + (r.id === currentRun ? ' selected' : '') + '" data-id="' + esc(r.id) + '">' +
        '<td class="mono">' + esc(r.started_at.replace('T',' ').slice(5,16)) + '</td>' +
        '<td class="mono" title="' + esc(r.target) + '">' + esc(r.target) + '</td>' +
        '<td class="c">' + statusBadge(r.status) + '</td>' +
        '<td class="num">' + fmtNum(m.hit_rate) + '</td>' +
        '<td class="num">' + fmtMs(m.search_p95_ms) + '</td></tr>';
    }).join('') || '<tr><td colspan="5" class="muted">暂无运行记录</td></tr>';
    document.querySelectorAll('#hist-body tr.clickable').forEach(function(tr){
      tr.onclick = function(){ selectRun(tr.dataset.id); };
    });
  }).catch(function(e){ toast('刷新列表失败: ' + e.message, true); });
}

function init(){
  api('/api/info').then(function(info){
    $('#info-script').textContent = info.test_script;
  }).catch(function(){});
  api('/api/corpus').then(renderCorpus).catch(function(){});
  $('#btn-start').onclick = startRun;
  $('#btn-cancel').onclick = cancelRun;
  $('#btn-report').onclick = function(){ window.open('/api/runs/' + currentRun + '/report', '_blank'); };
  $('#btn-json').onclick = function(){ window.open('/api/runs/' + currentRun + '/json', '_blank'); };
  document.querySelectorAll('.tabs button').forEach(function(b){
    b.onclick = function(){ switchTab(b.dataset.tab); };
  });
  $('#f-target').addEventListener('keydown', function(e){ if(e.key === 'Enter') startRun(); });
  $('#f-docs').addEventListener('input', syncDocChips);
  listTimer = setInterval(refreshList, 5000);
  refreshList();
  var qs = new URLSearchParams(location.search);
  var deepLink = qs.get('run');
  if(deepLink){
    selectRun(deepLink);
    var deepTab = qs.get('tab');
    if(deepTab){ switchTab(deepTab); }
  }
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
