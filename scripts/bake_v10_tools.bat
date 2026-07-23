@echo off
REM v10 TOOL-FLUENT bake (2026-07-22, Zeke greenlit the rules+tools build).
REM Warm-starts from v9.1 (adapter_v9_1_bak — best identity+grounding+rules) on
REM the dataset that now adds: the RULES as principles (three-tries-then-ask,
REM wall-clock-never-guess, honest-agent ladder, write-down-your-limits) + the
REM multi-turn TOOL DIALOGUES teaching her to EMIT [[tool:...]] calls and answer
REM from [[result:...]] (x10 for format learning).
REM SEQ=320 (not 256): tool dialogues run up to 306 tokens — at 256, 41 samples
REM truncate and cut off the tool answers. 320 fits all (max 306) and is the
REM documented-resident value on the 7B. paged_adamw_8bit avoids the wedge.
set IRIS_LB_BASE=unsloth/Qwen2.5-7B-Instruct
set IRIS_LB_ATTN_ONLY=1
set IRIS_LB_WARMSTART=D:\Wren-Companion\state\little_brain\adapter_v9_1_bak
set IRIS_LB_OPTIM=paged_adamw_8bit
set IRIS_LB_SAVE=steps
set IRIS_LB_SAVE_STEPS=25
set IRIS_LB_SEQ=320
set IRIS_LB_EPOCHS=2
cd /d D:\Wren-Companion
"D:\Wren-Companion\.venv-train\Scripts\python.exe" -u "D:\Wren-Companion\scripts\little_brain_finetune.py" >> "D:\Wren-Companion\state\little_brain\train_v10.log" 2>&1
