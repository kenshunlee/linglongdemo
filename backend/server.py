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
import subprocess
import sys
import tempfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from multipart import parse_form

# ------------------- 配置区 -------------------
ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY", "")
ZHIPU_BASE_URL = os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
ZHIPU_ASR_MODEL = os.getenv("ZHIPU_ASR_MODEL", "glm-asr-2512")
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "phi3")
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output"
OUTPUT_DIR = Path(os.getenv("ASR_OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR)))
LISTEN_HOST = os.getenv("ASR_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("PORT", os.getenv("ASR_PORT", "8765")))
# ---------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def generate_filename() -> str:
    return "asr" + datetime.now().strftime("%Y%m%d%H%M%S")


def transcribe_with_zhipu_asr(audio_path: str) -> str:
    if not ZHIPU_API_KEY:
        raise ValueError("未配置 ZHIPU_API_KEY")

    endpoint = f"{ZHIPU_BASE_URL.rstrip('/')}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {ZHIPU_API_KEY}"}

    with open(audio_path, "rb") as f:
        files = {
            "file": (Path(audio_path).name, f, "application/octet-stream")
        }
        data = {
            "model": ZHIPU_ASR_MODEL,
        }

        with httpx.Client(timeout=120) as client:
            resp = client.post(endpoint, headers=headers, data=data, files=files)
        resp.raise_for_status()
        data = resp.json()

    text = (
        data.get("text")
        or data.get("result")
        or data.get("response")
        or data.get("data", {}).get("text")
    )
    if not text:
        raise ValueError(f"GLM-ASR 响应中未找到转写文本: {data}")
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
    return {
        "status": "ok",
        "service": "ASR Bridge",
        "asr_provider": "zhipu",
        "asr_model": ZHIPU_ASR_MODEL,
        "zhipu_configured": bool(ZHIPU_API_KEY),
        "output_dir": str(OUTPUT_DIR),
        "output_dir_exists": OUTPUT_DIR.exists(),
    }


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

    def do_OPTIONS(self):
        self.send_response(204)
        self._set_cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
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
    log.info("ASR Bridge Service 启动中...")
    log.info(f"ASR 提供方: zhipu ({ZHIPU_ASR_MODEL})")
    log.info(f"ZHIPU_BASE_URL: {ZHIPU_BASE_URL}")
    log.info(f"输出目录: {OUTPUT_DIR}")
    log.info(f"监听: http://{LISTEN_HOST}:{LISTEN_PORT}")

    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ASRHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("收到停止信号，服务退出")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
