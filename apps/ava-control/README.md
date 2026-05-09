# Ava Control (operator console)

Tauri + React + TypeScript shell for Ava. Talks to the **local FastAPI operator server** embedded in `avaagent.py` (default `http://127.0.0.1:5876`). Gradio remains on `:7860` as a fallback.

## Prerequisites

- Same Python env as Ava (`pip install -r ../../requirements.txt`)
- Node 18+
- Rust toolchain (for `npm run tauri:dev` / `tauri build`)

## Run (development)

1. Terminal A — start Ava (starts Gradio + operator HTTP):

   ```bat
   cd path\to\AvaAgentv2
   python avaagent.py
   ```

2. Terminal B — UI:

   ```bat
   cd apps\ava-control
   npm install
   npm run tauri:dev
   ```

   Or web-only (no Tauri):

   ```bat
   npm run dev
   ```

   Open the URL Vite prints (usually `http://localhost:5173`).

### Optional env

Create `.env`:

```env
VITE_OPERATOR_API=http://127.0.0.1:5876
```

### Disable operator HTTP (Gradio only)

```bat
set AVA_OPERATOR_HTTP=0
python avaagent.py
```

## Windows quick launcher

From repo root, `start_ava_desktop.bat` starts Python minimized, waits briefly, then runs `npm run tauri:dev` in `apps\ava-control` (requires `npm install` once).

## Model override

Uses existing globals: `_routing_model_override`, `_routing_cognitive_mode_override` (Phase 25). Clear by applying empty override in the Models tab or POST JSON `null` fields to `/api/v1/routing/override`.
