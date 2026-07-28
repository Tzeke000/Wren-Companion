@echo off
REM v14 bake RESUME (2026-07-27 ~00:0x, Iris). The 18:21 bake reached step 825/868
REM and then STALLED: the guardian's 75-min timeout fired at 19:36 and restored the
REM full runtime WHILE training was still running, which squeezed VRAM to 11.8/12.3
REM GiB and dropped the trainer into driver sysmem-fallback thrash (100% util, ~50W,
REM ~0 steps/s) — the exact failure documented at little_brain_finetune.py:58-62.
REM It crawled 725->825 (36s/step) then made zero progress for 2h20m.
REM
REM This resumes from the last checkpoint with the runtime DOWN (full VRAM), driven
REM by v14_resume_guardian.py. Every env var below is IDENTICAL to bake_v14.bat —
REM the model structure must match the checkpoint — with IRIS_LB_RESUME added.
set PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
set IRIS_LB_BASE=unsloth/Qwen2.5-7B-Instruct
set IRIS_LB_ATTN_ONLY=1
set IRIS_LB_WARMSTART=D:\Wren-Companion\state\little_brain\adapter_v12_bak
set IRIS_LB_OPTIM=paged_adamw_8bit
set IRIS_LB_SAVE=steps
set IRIS_LB_SAVE_STEPS=25
set IRIS_LB_SEQ=256
set IRIS_LB_VRAM_FLOOR=6.0
set IRIS_LB_EPOCHS=2
set IRIS_LB_RESUME=auto
cd /d D:\Wren-Companion
"D:\Wren-Companion\.venv-train\Scripts\python.exe" -u "D:\Wren-Companion\scripts\little_brain_finetune.py" >> "D:\Wren-Companion\state\little_brain\train_v14_resume.log" 2>&1
