# app/claude_runner.py
"""
Claude CLI 실행.
- shell 미사용 (인자 배열)
- 프롬프트는 stdin으로 전달 (인자 길이 제한/프로세스 목록 노출 회피)
- 작업 디렉터리는 격리된 빈 폴더로 고정
- 환경변수 최소화 (화이트리스트)
- 도구 전체 비활성화 (--tools "")
- 타임아웃 강제
"""
import os
import json
import shutil
import threading
import subprocess

from app.config import (
    CLAUDE_TIMEOUT_SEC, ENV_WHITELIST, CLAUDE_WORKDIR, CLAUDE_BIN,
    CLAUDE_MODEL, CLAUDE_EFFORT,
)
from app.logging_conf import log_internal_error

_resolved_bin: str | None = None


def _claude_bin() -> str:
    """
    claude 실행 파일을 절대경로로 1회 해석해 캐시한다.
    부모 프로세스의 PATH/PATHEXT로 해석하므로 Windows의 claude.cmd 셰임도 찾는다.
    찾은 절대경로를 자식에 넘기되 shell은 사용하지 않는다.
    """
    global _resolved_bin
    if _resolved_bin is not None:
        return _resolved_bin
    cand = CLAUDE_BIN or shutil.which("claude") or "claude"
    _resolved_bin = cand
    return cand


def _terminate_tree(proc) -> None:
    """
    프로세스를 자식까지 통째로 종료한다.
    Windows에서 claude는 .CMD→cmd.exe→node 트리라 proc.kill()로는
    손자(node)가 남으므로 taskkill /T 로 트리 전체를 종료한다.
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, shell=False,
            )
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _minimal_env() -> dict:
    """
    환경변수 전체 상속 금지. 화이트리스트만 전달.
    Windows의 환경변수 키는 대문자(SYSTEMROOT 등)로 저장되는데
    화이트리스트는 'SystemRoot' 표기이므로 대소문자 무시로 매칭한다.
    (이 매칭이 정확하지 않으면 node가 SystemRoot 없이 떠서 크래시한다.)
    """
    wl_lower = {k.lower() for k in ENV_WHITELIST}
    return {k: v for k, v in os.environ.items() if k.lower() in wl_lower}


def _log_internal_error(detail: str) -> None:
    log_internal_error(detail)


def _absorb_usage(sink: dict | None, usage: dict | None, model: str | None = None) -> None:
    """
    stream-json usage 조각을 sink에 누적 반영한다(A-5 계측).
    여러 이벤트(message_start→message_delta→result)에 걸쳐 값이 갱신되므로
    '있는 값만' 덮어쓴다(None으로 기존 값을 지우지 않는다). result가 마지막이라 최종 승.
    """
    if sink is None:
        return
    if model:
        sink["model"] = model
    if not isinstance(usage, dict):
        return
    mapping = {
        "input_tokens": "input_tokens",
        "output_tokens": "output_tokens",
        "cache_read_input_tokens": "cache_read",
        "cache_creation_input_tokens": "cache_write",
    }
    for src_key, dst_key in mapping.items():
        v = usage.get(src_key)
        if v is not None:
            sink[dst_key] = v


def run_claude(prompt: str, model: str | None = None, effort: str | None = None) -> str:
    """프롬프트를 stdin으로 넘기고 텍스트 응답을 받는다. shell 절대 미사용.
    model: 주어지면 --model 로 해당 모델(별칭/풀네임)을 강제한다(A-1 경량 모델 등)."""
    CLAUDE_WORKDIR.mkdir(parents=True, exist_ok=True)

    # CLI 2.1.x 기준 확인된 플래그:
    #   -p / --print            : 비대화형 출력
    #   --output-format text    : 평문 출력
    #   --tools ""              : 내장 도구 전체 비활성화 (Bash/Edit/Read 등 차단)
    #   --model <alias>         : 모델 지정(선택). 미지정 시 기본 모델.
    # --dangerously-skip-permissions 류는 어떤 경우에도 사용하지 않는다.
    cmd = [
        _claude_bin(),
        "-p",
        "--output-format", "text",
        "--tools", "",
    ]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]

    # Popen + communicate(timeout) 로 직접 실행한다. 타임아웃 시 subprocess.run은
    # 직계 자식만 죽이지만, Windows의 claude는 .CMD→cmd.exe→node 트리라 손자(node)가
    # 좀비로 남을 수 있다. 그래서 TimeoutExpired에서 _terminate_tree로 트리를 정리한다(B-3).
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",   # 한국어 Windows 기본(cp949)로 디코딩하지 않도록 강제
            errors="replace",
            cwd=str(CLAUDE_WORKDIR),
            env=_minimal_env(),
            shell=False,  # 명시적 금지
        )
    except FileNotFoundError:
        # claude 실행 파일을 못 찾는 경우도 원문 노출 없이 분류
        _log_internal_error("claude executable not found on PATH")
        raise RuntimeError("CLAUDE_FAILED")

    try:
        out, err = proc.communicate(input=prompt, timeout=CLAUDE_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        _terminate_tree(proc)               # 손자(node) 포함 트리 종료
        try:
            proc.communicate(timeout=5)     # 파이프를 비우고 프로세스를 회수(reap)
        except Exception:
            pass
        raise RuntimeError("CLAUDE_TIMEOUT")

    if proc.returncode != 0:
        # stderr 원문을 그대로 올리지 않는다. 내부 로그용으로만 사용.
        _log_internal_error(err)
        raise RuntimeError("CLAUDE_FAILED")

    return out


def run_claude_stream(
    prompt: str,
    on_start=None,
    usage_sink: dict | None = None,
    model: str | None = None,
    effort: str | None = None,
):
    """
    run_claude의 스트리밍 버전. 텍스트 조각(delta)을 생성기로 하나씩 yield 한다.
    - 보안 속성은 run_claude와 동일(shell 미사용/stdin 전달/격리 cwd/최소 env/도구 비활성화)
    - 출력 형식만 stream-json 으로 바꿔 부분 응답(content_block_delta)을 즉시 흘려보낸다.
    - 타임아웃은 watchdog 스레드가 프로세스 트리를 종료해 강제한다.
    - 대용량 프롬프트(최대 200KB) stdin 기록은 별도 스레드로 처리해
      stdin/stdout 동시 블로킹(파이프 교착)을 피한다.
    - on_start(proc): Popen 직후 호출. 호출부가 취소용으로 프로세스를 등록할 수 있다.
    - usage_sink: 주어지면 stream-json에서 파싱한 실토큰/모델을 이 dict에 채운다
      (input_tokens, output_tokens, cache_read, cache_write, model). A-5 계측용.
    예외: RuntimeError("CLAUDE_TIMEOUT" | "CLAUDE_FAILED")
    """
    CLAUDE_WORKDIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        _claude_bin(),
        "-p",
        "--output-format", "stream-json",
        "--include-partial-messages",   # content_block_delta(부분 텍스트) 방출
        "--verbose",                    # stream-json + -p 에 필요
        "--tools", "",
    ]
    # 본 답변은 호출부 인자 → 없으면 config 기본값 순으로 모델/추론 강도를 정한다.
    eff_model = model or CLAUDE_MODEL
    eff_effort = effort or CLAUDE_EFFORT
    if eff_model:
        cmd += ["--model", eff_model]
    if eff_effort:
        cmd += ["--effort", eff_effort]

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(CLAUDE_WORKDIR),
            env=_minimal_env(),
            shell=False,
        )
    except FileNotFoundError:
        _log_internal_error("claude executable not found on PATH")
        raise RuntimeError("CLAUDE_FAILED")

    if on_start is not None:
        try:
            on_start(proc)
        except Exception:
            pass

    timed_out = {"v": False}

    def _kill():
        timed_out["v"] = True
        _terminate_tree(proc)

    timer = threading.Timer(CLAUDE_TIMEOUT_SEC, _kill)
    timer.start()

    def _feed_stdin():
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except Exception:
            pass

    writer = threading.Thread(target=_feed_stdin, daemon=True)
    writer.start()

    # B-2: stderr를 '지속적으로' 별도 스레드에서 비운다.
    # stdout만 읽고 stderr를 루프 종료 후에 읽으면, CLI가 stderr로 많이 쓸 때
    # 파이프 버퍼가 차서 자식이 블로킹 → 타임아웃까지 멈춘다(교착). 미리 드레인한다.
    stderr_chunks: list[str] = []

    def _drain_stderr():
        try:
            if proc.stderr is not None:
                for chunk in proc.stderr:
                    stderr_chunks.append(chunk)
        except Exception:
            pass

    stderr_reader = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_reader.start()

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except ValueError:
                continue
            etype = evt.get("type")
            if etype == "stream_event":
                inner = evt.get("event", {})
                itype = inner.get("type")
                if itype == "content_block_delta":
                    delta = inner.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            yield text
                elif itype == "message_start":
                    # 모델명과 초기 usage(입력/캐시 토큰)는 여기서만 온다.
                    _absorb_usage(usage_sink, inner.get("message", {}).get("usage"),
                                  inner.get("message", {}).get("model"))
                elif itype == "message_delta":
                    # 최종 output_tokens(누적)이 여기 담긴다.
                    _absorb_usage(usage_sink, inner.get("usage"))
            elif etype == "result":
                # 최상위 result 이벤트의 usage가 가장 권위 있는 최종값(마지막에 옴).
                _absorb_usage(usage_sink, evt.get("usage"))
        proc.wait()
    finally:
        timer.cancel()
        writer.join(timeout=1)
        # 클라이언트가 중간에 끊어(GeneratorExit) 생성기가 일찍 닫히면
        # 남은 CLI 프로세스 트리를 정리한다(좀비/자원 누수 방지).
        _terminate_tree(proc)
        # 드레인 스레드가 stderr EOF까지 비우고 끝나도록 join(프로세스 종료 후).
        stderr_reader.join(timeout=2)

    if timed_out["v"]:
        raise RuntimeError("CLAUDE_TIMEOUT")
    if proc.returncode not in (0, None):
        _log_internal_error("".join(stderr_chunks))
        raise RuntimeError("CLAUDE_FAILED")
