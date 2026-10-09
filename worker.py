import base64
import io
import json
import math
import os
import secrets
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image


LISTEN_HOST = os.getenv("IMAGE_OPTIMIZER_HOST", "127.0.0.1")
LISTEN_PORT = int(os.getenv("IMAGE_OPTIMIZER_PORT", "8789"))
API_KEY = os.getenv("IMAGE_OPTIMIZER_API_KEY", "").strip()
REALESRGAN_EXE = Path(
    os.getenv(
        "REALESRGAN_EXE",
        r"C:\Users\22621\image2-resize-test\realesrgan\realesrgan-ncnn-vulkan.exe",
    )
)
MAX_BODY_BYTES = int(os.getenv("IMAGE_OPTIMIZER_MAX_BODY_BYTES", str(48 * 1024 * 1024)))
MIN_PIXELS = 655_360
MAX_PIXELS = 8_294_400
MAX_EDGE = 3_840


def env_int(name, default, minimum, maximum):
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def env_float(name, default, minimum, maximum):
    try:
        value = float(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


# CPU/downscale HTTP work and two GPU upscale processes can run in parallel by default.
GPU_CONCURRENCY = env_int("IMAGE_OPTIMIZER_GPU_CONCURRENCY", 2, 1, 8)
GPU_SLOTS = threading.BoundedSemaphore(GPU_CONCURRENCY)
GPU_PROCESS_TIMEOUT_SECONDS = env_float("IMAGE_OPTIMIZER_GPU_TIMEOUT_SECONDS", 150.0, 0.0, 3600.0)
HTTP_IO_TIMEOUT_SECONDS = env_float("IMAGE_OPTIMIZER_HTTP_IO_TIMEOUT_SECONDS", 60.0, 5.0, 600.0)


def error_payload(message, code, param=""):
    return {"error": {"message": message, "type": "invalid_request_error", "param": param, "code": code}}


def parse_image(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("image is required")
    encoded = value.split(",", 1)[1] if value.startswith("data:") and "," in value else value
    raw = base64.b64decode(encoded, validate=True)
    image = Image.open(io.BytesIO(raw))
    image.load()
    return image.convert("RGB")


def validate_target(width, height):
    if width <= 0 or height <= 0:
        return error_payload("图片宽高必须大于 0", "image_size_invalid", "size")
    pixels = width * height
    if pixels < MIN_PIXELS:
        return error_payload("图片总像素不能小于 655360", "image_pixel_count_too_small", "size")
    if pixels > MAX_PIXELS:
        return error_payload("图片总像素不能大于 8294400", "image_pixel_count_too_large", "size")
    if max(width, height) > MAX_EDGE:
        return error_payload("图片最大边不能超过 3840", "image_edge_too_large", "size")
    if width > height * 3 or height > width * 3:
        return error_payload("图片比例必须在 1:3 到 3:1 之间", "image_aspect_ratio_out_of_range", "size")
    return None


def run_realesrgan(image):
    if not REALESRGAN_EXE.is_file():
        raise RuntimeError(f"Real-ESRGAN executable not found: {REALESRGAN_EXE}")
    with tempfile.TemporaryDirectory(prefix="terln-image-") as temp_dir:
        input_path = Path(temp_dir) / "input.png"
        output_path = Path(temp_dir) / "output.png"
        image.save(input_path, "PNG")
        command = [
            str(REALESRGAN_EXE),
            "-i",
            str(input_path),
            "-o",
            str(output_path),
            "-n",
            "realesrgan-x4plus",
            "-s",
            "4",
            "-f",
            "png",
        ]
        with GPU_SLOTS:
            run_options = {
                "cwd": str(REALESRGAN_EXE.parent),
                "capture_output": True,
                "text": True,
            }
            if GPU_PROCESS_TIMEOUT_SECONDS > 0:
                run_options["timeout"] = GPU_PROCESS_TIMEOUT_SECONDS
            completed = subprocess.run(command, **run_options)
        if completed.returncode != 0 or not output_path.is_file():
            detail = (completed.stderr or completed.stdout or "unknown error").strip()
            raise RuntimeError(f"Real-ESRGAN failed: {detail[:500]}")
        output = Image.open(output_path)
        output.load()
        return output.convert("RGB")


def cover_resize_and_crop(image, target_width, target_height):
    source_width, source_height = image.size
    scale = max(target_width / source_width, target_height / source_height)
    resized_width = max(target_width, math.ceil(source_width * scale))
    resized_height = max(target_height, math.ceil(source_height * scale))
    if (resized_width, resized_height) != image.size:
        image = image.resize((resized_width, resized_height), Image.Resampling.LANCZOS)
    left = max(0, (image.width - target_width) // 2)
    top = max(0, (image.height - target_height) // 2)
    return image.crop((left, top, left + target_width, top + target_height))


def optimize(payload):
    target_width = int(payload.get("target_width", 0))
    target_height = int(payload.get("target_height", 0))
    if validation := validate_target(target_width, target_height):
        return 400, validation
    image = parse_image(payload.get("image"))
    source_width, source_height = image.size
    source_pixels = source_width * source_height
    target_pixels = target_width * target_height
    operation = "center_crop"
    started = time.monotonic()
    if target_pixels > source_pixels:
        image = run_realesrgan(image)
        operation = "realesrgan_x4_then_center_crop"
    elif target_pixels < source_pixels:
        operation = "lanczos_downscale_then_center_crop"
    image = cover_resize_and_crop(image, target_width, target_height)
    output = io.BytesIO()
    image.save(output, "PNG", optimize=True)
    return 200, {
        "width": image.width,
        "height": image.height,
        "source_width": source_width,
        "source_height": source_height,
        "operation": operation,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "output_format": "png",
        "image": "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii"),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "terln-image-optimizer/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(HTTP_IO_TIMEOUT_SECONDS)

    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} {fmt % args}", flush=True)

    def write_json(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass

    def authorized(self):
        if not API_KEY:
            return False
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return secrets.compare_digest(supplied, API_KEY)

    def do_GET(self):
        if self.path != "/health":
            self.write_json(404, error_payload("not found", "not_found"))
            return
        self.write_json(
            200,
            {
                "status": "ok",
                "realesrgan_ready": REALESRGAN_EXE.is_file(),
                "gpu_concurrency": GPU_CONCURRENCY,
                "gpu_timeout_seconds": GPU_PROCESS_TIMEOUT_SECONDS,
                "http_io_timeout_seconds": HTTP_IO_TIMEOUT_SECONDS,
            },
        )

    def do_POST(self):
        if self.path != "/v1/images/optimize":
            self.write_json(404, error_payload("not found", "not_found"))
            return
        if not self.authorized():
            self.write_json(401, error_payload("unauthorized", "unauthorized"))
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                self.write_json(413, error_payload("request body is too large", "request_too_large"))
                return
            payload = json.loads(self.rfile.read(length))
            status, result = optimize(payload)
            self.write_json(status, result)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.write_json(400, error_payload(str(exc), "invalid_request"))
        except subprocess.TimeoutExpired:
            self.write_json(504, error_payload("image optimization timed out", "optimizer_timeout"))
        except Exception as exc:
            self.write_json(500, error_payload(str(exc), "optimizer_failed"))


if __name__ == "__main__":
    if not API_KEY:
        raise SystemExit("IMAGE_OPTIMIZER_API_KEY is required")
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()
