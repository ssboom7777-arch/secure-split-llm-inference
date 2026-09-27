# Agent 协作全程记录

交付文件为 [`codex-project-transcript.md`](codex-project-transcript.md)。它从本地 Codex rollout 会话中自动提取，按时间保留与任务相关的用户与 Agent 可见消息。系统提示、内部推理、工具原始日志、无关问答、误输入、重复输入和 Transcript 生成自述未纳入交付文件；实验失败和人工纠偏仍完整保留。公开的 [`curation.json`](curation.json) 记录了全部整理规则。

项目中的命令、代码改动、实验结果和原始数据分别保存在 Q1～Q4 目录，Transcript 用于说明人机协作与决策过程，两者共同形成可审计记录。

## 重新生成

```powershell
python .\docs\transcript\export_transcript.py `
  <rollout-1.jsonl> <rollout-2.jsonl> `
  --curation .\docs\transcript\curation.json `
  -o .\docs\transcript\codex-project-transcript.md
```

不同机器上的 Codex 会话路径可能不同，需要显式传入实际 rollout 文件。不传入 `--curation` 时，脚本会生成未整理的可见消息版，并在文件头记录原始会话文件名和 SHA-256。
