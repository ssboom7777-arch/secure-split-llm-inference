# Agent 协作全程记录

交付文件为 [`codex-project-transcript.md`](codex-project-transcript.md)。它从本地 Codex rollout 会话中自动提取，按时间保留用户与 Agent 的可见消息原文；系统提示、内部推理和工具原始日志不属于对话正文，未纳入交付文件。

项目中的命令、代码改动、实验结果和原始数据分别保存在 Q1～Q4 目录，Transcript 用于说明人机协作与决策过程，两者共同形成可审计记录。

## 重新生成

```powershell
python .\docs\transcript\export_transcript.py `
  <rollout-1.jsonl> <rollout-2.jsonl> `
  -o .\docs\transcript\codex-project-transcript.md
```

生成文件头部会记录原始会话文件名和 SHA-256，便于核对来源。不同机器上的 Codex 会话路径可能不同，需要显式传入实际 rollout 文件。
