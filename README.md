# pixfine

基于 Real-ESRGAN 的图像优化计费服务

This standalone service runs entirely on the GPU host `192.168.4.2`:

- `worker.py` on Windows port `8789`: Real-ESRGAN/Lanczos GPU execution.
- `app.py` on Windows port `8791`: independent authentication, wallet, billing, queue, payment callbacks and WebUI.
- Native billing/WebUI on port `8791`; Windows Caddy on port `80` routes both existing FRP hostnames to it.
- The original Docker Caddy remains available on host port `8082` for unrelated hostnames.
- SQLite WAL data: `data/billing.db`; successful idempotent responses: `data/responses/`.
- Production admits `16` active image transfers/jobs and a bounded queue of `48`. Most production jobs are 1K Lanczos (~1.5s); Real-ESRGAN remains capped at two GPU processes. Admission happens before a large Base64 body is read, so slow FRP uploads cannot bypass backpressure and saturate the tunnel. Queue waiting is limited to 180 seconds, total body ingress to 300 seconds, app-to-worker execution to 180 seconds, and Real-ESRGAN execution to 150 seconds; a full or expired queue is rejected before any job reservation or charge. The gateway also refuses new image generation when `/health` shows no landing headroom, and retries 502/503 after a generated image instead of dropping it immediately.

## Billing

| Tier | Target pixels | Price |
|---|---:|---:|
| 1K | `<= 1240 * 1240` | `¥0.032` |
| 2K | `> 1240 * 1240` and `<= 2480 * 2480` | `¥0.048` |
| 4K | `> 2480 * 2480` | `¥0.064` |

The request is rejected below `655360` pixels, above `8294400` pixels, above a `3840` pixel edge, or outside ratios `1:3` through `3:1`. A job reserves its price before entering the bounded optimizer queue, settles on success and refunds on failure. `X-Request-ID` is the idempotency key.

Runtime tuning variables:

- `IMAGE_OPTIMIZER_MAX_CONCURRENCY`: active ingress/optimizer requests, production `16`, maximum `32`.
- `IMAGE_OPTIMIZER_MAX_QUEUE`: waiting ingress requests before overload rejection, production `48`.
- `IMAGE_OPTIMIZER_QUEUE_TIMEOUT_SECONDS`: maximum admission wait, production `180`; `0` explicitly disables this guardrail.
- `IMAGE_OPTIMIZER_GPU_CONCURRENCY`: concurrent Real-ESRGAN processes, default `2`; lower it if the GPU runs out of VRAM.
- `IMAGE_OPTIMIZER_GPU_TIMEOUT_SECONDS`: Real-ESRGAN process timeout, default `150`; `0` explicitly disables this guardrail.
- `IMAGE_OPTIMIZER_WORKER_TIMEOUT_SECONDS`: app-to-worker request timeout, default `180`; `0` explicitly disables this guardrail.
- `IMAGE_OPTIMIZER_HTTP_IO_TIMEOUT_SECONDS`: socket read timeout and normal HTTP idle timeout, default `60`; prevents half-open FRP/Caddy connections from occupying a handler indefinitely.
- `IMAGE_OPTIMIZER_RESPONSE_WRITE_TIMEOUT_SECONDS`: finite response-write budget for multi-MiB Base64 images, production `300`; aligned with the gateway optimizer budget so a healthy but slow tunnel cannot truncate a 200 JSON response at 60 seconds.
- `IMAGE_OPTIMIZER_INGRESS_TIMEOUT_SECONDS`: hard wall-clock budget for one optimizer request body, default/production `300`; this also covers a continuously slow upload that does not trigger the socket idle timeout.

`/health` exposes `active`/`queued` for all admitted requests (including body upload), plus `admission_available` and `saturated`; an upload that is waiting for admission is therefore visible before it has a billing job.

The gateway submits multiple images from one generation/edit request through a bounded worker pool (`IMAGE_OPTIMIZER_IMAGE_CONCURRENCY`, default `2`, maximum `8`). Results keep their original order and use per-image idempotency keys; the default matches the two Real-ESRGAN GPU slots on the RTX 2060 SUPER host.

## Files required on the host

- `api-key.txt`: native worker key.
- `service-api-key.txt`: terln-api to billing service key.
- `password-hash.txt`: `pbkdf2_sha256$iterations$salt$digest` WebUI password hash.
- `payment.json`: runtime-only payment configuration. New orders use the same VMQ `createOrder`/HMAC callback flow as `tietiezhi-gateway`; legacy official merchant fields remain only so already-created direct orders can still complete their original callbacks. Never commit this file.

Install `requirements.txt`, start `start-worker.ps1` and `start-app.ps1`, then run the Windows Caddy with `Caddyfile.windows`. Public WebUI and callbacks use `https://image-optimizer.tietiezhi.xyz` through the existing Singapore FRP tunnel.
