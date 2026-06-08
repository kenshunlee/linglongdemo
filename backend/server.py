"""
ASR 本地桥接服务（兼容版）
功能：接收微信小程序上传的音频 → 调用智谱 GLM-ASR-2512 转写 → 保存 txt 到 output/
运行：python server.py
依赖：pip install httpx python-multipart
"""

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from typing import Any
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from multipart import parse_form
from robot_control import maybe_handle_robot_request

# from zai import ZhipuAiClient

try:
    from faster_whisper import WhisperModel  # type: ignore[import-not-found]
except Exception:
    WhisperModel = None


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return

    try:
        with env_path.open("r", encoding="utf-8-sig") as fp:
            for raw_line in fp:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue

                if line.lower().startswith("export "):
                    line = line[7:].strip()

                if "=" not in line:
                    continue

                key, value = line.split("=", 1)
                key = key.strip()
                if not key or key in os.environ:
                    continue

                value = value.strip()
                if (value.startswith('"') and value.endswith('"')) or (
                    value.startswith("'") and value.endswith("'")
                ):
                    value = value[1:-1]

                os.environ[key] = value
    except Exception as exc:
        print(f"[WARN] 读取环境文件失败 {env_path}: {exc}")


load_env_file(Path(__file__).with_name("cloud.env"))
# ------------------- 配置区 -------------------
ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY", "")
ZHIPU_BASE_URL = os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
ZHIPU_ASR_MODEL = os.getenv("ZHIPU_ASR_MODEL", "glm-asr-2512")
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "phi3")
PHI3_FIRST = os.getenv("PHI3_FIRST", "0") == "1"
LOCAL_ASR_ENABLED = os.getenv("LOCAL_ASR_ENABLED", "1") == "1"
LOCAL_ASR_MODEL_SIZE = os.getenv("LOCAL_ASR_MODEL_SIZE", "small")
LOCAL_ASR_DEVICE = os.getenv("LOCAL_ASR_DEVICE", "auto")
LOCAL_ASR_LANGUAGE = os.getenv("LOCAL_ASR_LANGUAGE", "zh")
LOCAL_ASR_DOWNLOAD_DIR = os.getenv("LOCAL_ASR_DOWNLOAD_DIR", str(Path(__file__).resolve().parents[1] / "models"))
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output"
OUTPUT_DIR = Path(os.getenv("ASR_OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR)))
LISTEN_HOST = os.getenv("ASR_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("PORT", os.getenv("ASR_PORT", "8765")))
USB_DEBUG_PREFERRED = os.getenv("USB_DEBUG_PREFERRED", "1") == "1"
if USB_DEBUG_PREFERRED and LISTEN_HOST in {"127.0.0.1", "localhost", "::1"}:
    LISTEN_HOST = "0.0.0.0"
# ---------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

LOCAL_ASR_STATE: dict[str, object] = {
    "enabled": LOCAL_ASR_ENABLED,
    "ready": False,
    "provider": "faster-whisper",
    "model": LOCAL_ASR_MODEL_SIZE,
    "device": "unknown",
    "error": "",
}
LOCAL_ASR_MODEL = None


def generate_filename() -> str:
    return "asr" + datetime.now().strftime("%Y%m%d%H%M%S")


def init_local_asr() -> None:
    global LOCAL_ASR_MODEL

    if not LOCAL_ASR_ENABLED:
        LOCAL_ASR_STATE["error"] = "LOCAL_ASR_ENABLED=0"
        return

    if WhisperModel is None:
        LOCAL_ASR_STATE["error"] = "未安装 faster-whisper"
        return

    download_dir = Path(LOCAL_ASR_DOWNLOAD_DIR)
    download_dir.mkdir(parents=True, exist_ok=True)

    def _load_model(device: str, compute_type: str) -> Any:
        return WhisperModel(
            LOCAL_ASR_MODEL_SIZE,
            device=device,
            compute_type=compute_type,
            download_root=str(download_dir),
        )

    preferred_device = LOCAL_ASR_DEVICE
    if preferred_device == "auto":
        preferred_device = "cuda"

    try:
        LOCAL_ASR_MODEL = _load_model(preferred_device, "float16" if preferred_device == "cuda" else "int8")
        LOCAL_ASR_STATE.update({"ready": True, "device": preferred_device, "error": ""})
        log.info(f"本地 ASR 已就绪: faster-whisper/{LOCAL_ASR_MODEL_SIZE} on {preferred_device}")
        return
    except Exception as e:
        LOCAL_ASR_STATE["error"] = f"{preferred_device} 初始化失败: {e}"
        log.warning(f"本地 ASR 初始化失败({preferred_device}): {e}")

    if preferred_device != "cpu":
        try:
            LOCAL_ASR_MODEL = _load_model("cpu", "int8")
            LOCAL_ASR_STATE.update({"ready": True, "device": "cpu", "error": ""})
            log.info(f"本地 ASR 已降级为 CPU: faster-whisper/{LOCAL_ASR_MODEL_SIZE}")
            return
        except Exception as e:
            LOCAL_ASR_STATE["error"] = f"cpu 初始化失败: {e}"
            log.warning(f"本地 ASR CPU 初始化失败: {e}")


def transcribe_with_local_asr(audio_path: str) -> str:
    if not LOCAL_ASR_STATE.get("ready") or LOCAL_ASR_MODEL is None:
        raise RuntimeError(f"本地 ASR 不可用: {LOCAL_ASR_STATE.get('error', 'unknown')}")

    segments, _ = LOCAL_ASR_MODEL.transcribe(
        audio_path,
        beam_size=5,
        vad_filter=True,
        language=LOCAL_ASR_LANGUAGE,
    )
    text = "".join(segment.text for segment in segments).strip()
    if not text:
        raise RuntimeError("本地 ASR 未返回文本")
    return text


def transcribe_with_zhipu_asr(audio_path: str) -> str:
    if not ZHIPU_API_KEY:
        raise ValueError("未配置 ZHIPU_API_KEY")

    url = f"{ZHIPU_BASE_URL.rstrip('/')}/audio/transcriptions"
    payload = {
        "model": ZHIPU_ASR_MODEL,
        "stream": "false",
    }
    headers = {"Authorization": f"Bearer {ZHIPU_API_KEY}"}

    print(f"请求 GLM-ASR 转写: {audio_path} → {url}，模型: {ZHIPU_ASR_MODEL}")

    with open(audio_path, "rb") as fp:
        files = {
            "file": (Path(audio_path).name, fp, "application/octet-stream")
        }
        with httpx.Client(timeout=120) as client:
            response = client.post(url, data=payload, files=files, headers=headers)
        response.raise_for_status()
        data = response.json()

    text = (
        data.get("text")
        or data.get("result")
        or data.get("response")
        or data.get("data", {}).get("text")
    )
    if not text:
        raise ValueError(f"GLM-ASR 响应中未找到转写文本: {data}")
    
    print(f"GLM-ASR 原始响应: {data}")
    print(f"GLM-ASR 提取文本: {text}")

    return str(text).strip()


def transcribe_with_whisper_cpp(audio_path: str) -> str:
    whisper_bins = [
        "whisper-cli",
        "whisper",
        "main",
        r"C:\whisper.cpp\main.exe",
        r"C:\Program Files\whisper\whisper-cli.exe",
    ]

    bin_path = None
    for b in whisper_bins:
        if shutil.which(b):
            bin_path = b
            break

    if not bin_path:
        raise FileNotFoundError("未找到 whisper-cli 可执行文件")

    wav_path = audio_path + ".wav"
    ffmpeg_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        audio_path,
        "-ar",
        "16000",
        "-ac",
        "1",
        "-f",
        "wav",
        wav_path,
    ]
    subprocess.run(ffmpeg_cmd, capture_output=True, check=True)

    result = subprocess.run(
        [bin_path, "-m", "models/ggml-base.bin", "-f", wav_path, "--output-txt"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def transcribe_with_phi3_mock(audio_path: str) -> str:
    file_size = os.path.getsize(audio_path)
    return (
        f"[调试模式/{FALLBACK_MODEL}] 收到音频文件，大小 {file_size} 字节。"
        "当前环境未成功调用 GLM-ASR-2512，请检查 ZHIPU_API_KEY 或网络连通性。"
    )


def smart_transcribe(audio_path: str) -> tuple[str, str]:
    try:
        log.info("尝试 GLM-ASR-2512 转写...")
        text = transcribe_with_zhipu_asr(audio_path)
        if text:
            return text, "glm-asr-2512"
    except Exception as e:
        log.warning(f"GLM-ASR-2512 失败: {e}")

    if LOCAL_ASR_STATE.get("ready"):
        try:
            log.info("尝试本地 ASR(faster-whisper) 转写...")
            text = transcribe_with_local_asr(audio_path)
            if text:
                engine = f"faster-whisper-{LOCAL_ASR_STATE.get('device', 'unknown')}"
                return text, engine
        except Exception as e:
            log.warning(f"本地 ASR 失败: {e}")

    try:
        log.info("尝试 whisper.cpp CLI 转写...")
        text = transcribe_with_whisper_cpp(audio_path)
        if text:
            return text, "whisper-cpp"
    except Exception as e:
        log.warning(f"whisper.cpp 失败: {e}")

    try:
        log.info("降级到 phi3 占位模式...")
        text = transcribe_with_phi3_mock(audio_path)
        return text, "phi3-fallback"
    except Exception as e:
        log.error(f"phi3 也失败了: {e}")
        return f"[转写失败] 所有引擎均不可用。错误：{e}", "none"


def get_health_payload() -> dict:
    if PHI3_FIRST:
        preferred_provider = "phi3"
        preferred_model = FALLBACK_MODEL
    else:
        preferred_provider = "faster-whisper" if LOCAL_ASR_STATE.get("ready") else ("zhipu" if ZHIPU_API_KEY else "fallback")
        preferred_model = str(LOCAL_ASR_STATE.get("model")) if LOCAL_ASR_STATE.get("ready") else ZHIPU_ASR_MODEL
    return {
        "status": "ok",
        "service": "ASR Bridge",
        "asr_provider": preferred_provider,
        "asr_model": preferred_model,
        "phi3_first": PHI3_FIRST,
        "zhipu_configured": bool(ZHIPU_API_KEY),
        "local_asr_enabled": LOCAL_ASR_ENABLED,
        "local_asr_ready": bool(LOCAL_ASR_STATE.get("ready")),
        "local_asr_provider": LOCAL_ASR_STATE.get("provider"),
        "local_asr_model": LOCAL_ASR_STATE.get("model"),
        "device": LOCAL_ASR_STATE.get("device"),
        "gpu_available": LOCAL_ASR_STATE.get("device") == "cuda" and bool(LOCAL_ASR_STATE.get("ready")),
        "active_engine": (
            "phi3-priority"
            if PHI3_FIRST
            else
            f"faster-whisper-{LOCAL_ASR_STATE.get('device', 'unknown')}"
            if LOCAL_ASR_STATE.get("ready")
            else "remote-fallback"
        ),
        "local_asr_error": LOCAL_ASR_STATE.get("error"),
        "output_dir": str(OUTPUT_DIR),
        "output_dir_exists": OUTPUT_DIR.exists(),
    }


def get_ipv4_addresses() -> list[str]:
    ips = []
    try:
        hostnames = [socket.gethostname(), socket.getfqdn()]
        for host in hostnames:
            for ip in socket.gethostbyname_ex(host)[2]:
                if ip and not ip.startswith("127.") and ip not in ips:
                    ips.append(ip)
    except Exception:
        return ips
    return ips


def _sort_usb_addresses(addresses: list[str]) -> list[str]:
    def score(ip: str) -> tuple[int, str]:
        if ip.startswith("192.168.137."):
            return (0, ip)
        if ip.startswith("192.168."):
            return (1, ip)
        if ip.startswith("172."):
            return (2, ip)
        if ip.startswith("10."):
            return (3, ip)
        return (4, ip)

    return sorted(addresses, key=score)


def list_records_payload(limit: int) -> dict:
    files = sorted(OUTPUT_DIR.glob("asr*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    records = []

    for f in files[:limit]:
        stat = f.stat()
        try:
            with open(f, encoding="utf-8") as fp:
                lines = fp.readlines()

            content_lines = []
            found_sep = False
            for line in lines:
                if "─" in line:
                    found_sep = True
                    continue
                if found_sep:
                    content_lines.append(line)

            preview = "".join(content_lines)[:100].strip()
        except Exception:
            preview = ""

        records.append(
            {
                "filename": f.name,
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "preview": preview,
            }
        )

    return {"records": records, "total": len(files)}


class ASRHandler(BaseHTTPRequestHandler):
    server_version = "ASRBridge/1.0"

    def _set_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization")

    def _send_json(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._set_cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, code: int, payload: bytes, content_type: str):
        self.send_response(code)
        self._set_cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        self.send_response(204)
        self._set_cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        robot_resp = maybe_handle_robot_request("GET", self.path)
        if robot_resp is not None:
            code, payload, content_type = robot_resp
            if isinstance(payload, (bytes, bytearray)):
                self._send_bytes(code, bytes(payload), content_type)
            else:
                self._send_json(code, payload)
            return

        if parsed.path == "/health":
            self._send_json(200, get_health_payload())
            return

        if parsed.path == "/records":
            qs = parse_qs(parsed.query)
            try:
                limit = int(qs.get("limit", ["20"])[0])
            except ValueError:
                limit = 20
            self._send_json(200, list_records_payload(limit))
            return

        self._send_json(404, {"detail": "Not Found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/robot/"):
            try:
                clen = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                clen = 0
            body = self.rfile.read(clen) if clen > 0 else b""
            robot_resp = maybe_handle_robot_request("POST", self.path, body)
            if robot_resp is None:
                self._send_json(404, {"detail": "Not Found"})
                return
            code, payload, _ = robot_resp
            self._send_json(code, payload)
            return

        if parsed.path != "/transcribe":
            self._send_json(404, {"detail": "Not Found"})
            return

        try:
            content_type = self.headers.get("Content-Type", "")
            if not content_type.lower().startswith("multipart/form-data"):
                self._send_json(400, {"detail": "未收到音频文件"})
                return

            audio_meta: dict[str, object] = {}

            def on_field(_field):
                return

            def on_file(file_obj):
                audio_meta["field_name"] = file_obj.field_name.decode("utf-8", errors="ignore")
                audio_meta["file_name"] = file_obj.file_name.decode("utf-8", errors="ignore")
                try:
                    file_obj.file_object.seek(0)
                except Exception:
                    pass
                audio_meta["content"] = file_obj.file_object.read()

            headers = {
                "Content-Type": self.headers.get("Content-Type", "").encode("utf-8"),
                "Content-Length": self.headers.get("Content-Length", "0").encode("utf-8"),
            }
            try:
                parse_form(headers, self.rfile, on_field, on_file)
            except Exception:
                self._send_json(400, {"detail": "未收到音频文件"})
                return

            if (
                audio_meta.get("field_name") != "audio"
                or not audio_meta.get("file_name")
                or not audio_meta.get("content")
            ):
                self._send_json(400, {"detail": "未收到音频文件"})
                return

            audio_filename = str(audio_meta["file_name"])
            content = bytes(audio_meta["content"])
            suffix = Path(audio_filename).suffix or ".m4a"

            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp_path = tmp.name
                tmp.write(content)

            log.info(f"收到音频: {audio_filename}, 大小: {len(content)} bytes, 临时路径: {tmp_path}")

            try:
                transcribed_text, engine_used = smart_transcribe(tmp_path)
                log.info(f"转写完成 [{engine_used}]: {transcribed_text[:80]}...")

                txt_filename = f"{generate_filename()}.txt"
                txt_path = OUTPUT_DIR / txt_filename

                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(f"转写时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"转写引擎：{engine_used}\n")
                    f.write(f"原始文件：{audio_filename}\n")
                    f.write("─" * 40 + "\n")
                    f.write(transcribed_text)

                log.info(f"已保存: {txt_path}")

                self._send_json(
                    200,
                    {
                        "success": True,
                        "filename": txt_filename,
                        "filepath": str(txt_path),
                        "text": transcribed_text,
                        "engine": engine_used,
                        "timestamp": datetime.now().isoformat(),
                    },
                )
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

        except Exception as e:
            log.error(f"转写异常: {e}", exc_info=True)
            self._send_json(500, {"detail": str(e)})


def main():
    init_local_asr()

    log.info("ASR Bridge Service 启动中...")
    if LOCAL_ASR_STATE.get("ready"):
        log.info(
            "ASR 提供方: faster-whisper "
            f"({LOCAL_ASR_STATE.get('model')}, device={LOCAL_ASR_STATE.get('device')})"
        )
    else:
        log.info(f"ASR 提供方: zhipu ({ZHIPU_ASR_MODEL})")
    log.info(f"ZHIPU_BASE_URL: {ZHIPU_BASE_URL}")
    if LOCAL_ASR_STATE.get("error"):
        log.info(f"本地 ASR 状态: {LOCAL_ASR_STATE.get('error')}")
    log.info(f"输出目录: {OUTPUT_DIR}")
    log.info(f"监听: http://{LISTEN_HOST}:{LISTEN_PORT}")
    ips = _sort_usb_addresses(get_ipv4_addresses())
    for ip in ips:
        log.info(f"可用于手机调试的地址: http://{ip}:{LISTEN_PORT}")
    if ips:
        log.info(f"USB 调试优先地址: http://{ips[0]}:{LISTEN_PORT}")

    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ASRHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("收到停止信号，服务退出")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
