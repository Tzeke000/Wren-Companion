@echo off
REM v12 bake (2026-07-22, Zeke greenlit escalation reflex). Warm from v11
REM (adapter_v11_bak) on the dataset adding the ESCALATION close: hard task ->
REM memory_recall limit -> [[tool:ask_big_iris|...]] -> "handed to big Iris"
REM (v11 recalls its limit but won't reach the 6th tool from prompt alone).
REM SEQ=448: the multi-hop escalation dialogues run to ~444 tok (TOOL_SPEC grew
REM with the 6th tool); 448 fits all. If it WEDGES (VRAM thrash on the 7B),
REM drop IRIS_LB_SEQ to 384 (truncates only the one longest dialogue).
set IRIS_LB_BASE=unsloth/Qwen2.5-7B-Instruct
set IRIS_LB_ATTN_ONLY=1
set IRIS_LB_WARMSTART=D:\Wren-Companion\state\little_brain\adapter_v11_bak
set IRIS_LB_OPTIM=paged_adamw_8bit
set IRIS_LB_SAVE=steps
set IRIS_LB_SAVE_STEPS=25
set IRIS_LB_SEQ=448
set IRIS_LB_EPOCHS=2
cd /d D:\Wren-Companion
"D:\Wren-Companion\.venv-train\Scripts\python.exe" -u "D:\Wren-Companion\scripts\little_brain_finetune.py" >> "D:\Wren-Companion\state\little_brain\train_v12.log" 2>&1
