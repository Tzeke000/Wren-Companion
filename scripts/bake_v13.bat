@echo off
REM v13 bake (2026-07-24, Zeke greenlit ACT-NOT-NARRATE round). Warm from v12
REM (adapter_v12_bak). Adds: act-not-narrate tool reflex, senses-gating, memory-
REM home fluency, deployment-confab fix, look-up-first, escalate-for-real,
REM journal-experiences, embodied-ownership, brevity. Corpus little_brain_corpus_v13.py
REM (v13_dlg x12). SEQ=448 (matches v12; drop to 384 if VRAM thrashes on the 7B).
REM Tight-VRAM allocator (2026-07-24): perception floor ~4.8GB leaves only ~7.4GB
REM free; at 448 AND 256 the first training step tipped over the 12GB ceiling into
REM Windows sysmem fallback and STALLED (util 100%%->0%%, hung at step 0). seq was
REM NOT the lever (peak barely moved). expandable_segments lets the allocator pack
REM into the tight free window instead of fragmenting over the edge.
set PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
set IRIS_LB_BASE=unsloth/Qwen2.5-7B-Instruct
set IRIS_LB_ATTN_ONLY=1
set IRIS_LB_WARMSTART=D:\Wren-Companion\state\little_brain\adapter_v12_bak
set IRIS_LB_OPTIM=paged_adamw_8bit
set IRIS_LB_SAVE=steps
set IRIS_LB_SAVE_STEPS=25
REM SEQ dropped 448->256 (2026-07-24): at 448 the 7B spilled to sysmem fallback
REM (12.06/12.29GiB, 100%% util, hung at step 0 = thrash, exactly the failure the
REM finetune docstring warns about). 256 covers the speech-sized corpus (p99 well
REM under it) and keeps peak ~6.5GB resident in the ~7.3GB free window.
set IRIS_LB_SEQ=256
REM Perception floor grew to ~4.7GB (nervous-system/senses), leaving ~7.3GB free,
REM below the 8.0 default guard; allow the lower floor deliberately for this run.
set IRIS_LB_VRAM_FLOOR=6.0
set IRIS_LB_EPOCHS=2
cd /d D:\Wren-Companion
"D:\Wren-Companion\.venv-train\Scripts\python.exe" -u "D:\Wren-Companion\scripts\little_brain_finetune.py" >> "D:\Wren-Companion\state\little_brain\train_v13.log" 2>&1
