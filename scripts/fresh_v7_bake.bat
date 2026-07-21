@echo off
REM Fresh v7 7B little-brain bake from step 0 (2026-07-21, Zeke chose option A
REM after the power-blip killed the overnight run and the bit-exact resume was
REM gated by torch<2.6). Same pinned config that made checkpoint-250:
REM Qwen2.5-7B base, attn-only LoRA (q/k/v/o), seq 256, save every 50 steps.
REM NO IRIS_LB_RESUME -> starts fresh (avoids the torch.load optimizer gate).
set IRIS_LB_BASE=unsloth/Qwen2.5-7B-Instruct
set IRIS_LB_ATTN_ONLY=1
set IRIS_LB_SAVE=steps
set IRIS_LB_SEQ=256
cd /d D:\Wren-Companion
"D:\Wren-Companion\.venv-train\Scripts\python.exe" -u "D:\Wren-Companion\scripts\little_brain_finetune.py" >> "D:\Wren-Companion\state\little_brain\train_v7_fresh.log" 2>&1
