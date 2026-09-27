# Split-Inference Security and Performance Challenge

本项目完成任务书中的四项工作：Hidden State安全评估、企业—云分割推理、并发流水线优化，以及面向指定模型/GPU/SLO的容量估算。

建议先阅读[项目理解与复盘入口](readme_first.md)，再按Q1～Q4顺序查看各子目录。

## 结果概览

| 任务 | 核心交付 | 主要结论 | 文档与入口 |
|---|---|---|---|
| Q1 安全 | 行业方案调研、Hidden State反演与加噪PoC | Embedding输出可通过余弦最近邻反向查表；有效降还原率前，生成可用性已经明显下降 | [调研](q1-industry-security-solutions.md) · [评估](q1-hidden-state-security-evaluation.md) · [PoC](q1-poc/README.md) |
| Q2 架构 | 独立企业/云HTTP服务、KV Cache、GSM8K对齐 | MiniMind单体/分割逐Token一致；Qwen3-1.7B分割模型GSM8K前50题为27/50 | [README](q2-split-inference/README.md) |
| Q3 优化 | 串行/流水线、Decode优先、并发控制、500 km/10 Gbps链路 | MiniMind主实验吞吐提高3.35倍、相对单位成本下降70.16%；并发3满足TPOT SLO | [README](q3-scheduler-poc/README.md) |
| Q4 建模 | 输入模型/GPU/工作负载/SLO，输出TTFT、TPOT和最大QPS | 当前假设下Qwen3-32B + 8×H20约0.97 QPS；结果是外推而非H20实测 | [README](q4-capacity-model/README.md) |

## 环境

- Windows PowerShell；
- Python 3.11；
- 16 GB内存可运行MiniMind全部PoC及Qwen3-1.7B短实验；
- CPU即可验证机制，目标GPU结果需在真实硬件上重新校准；
- 首次运行需要网络下载Python依赖和模型权重。

模型权重、虚拟环境、Wheel缓存和运行日志不提交Git。脚本会在首次运行时下载所需内容。

## Git未包含的大文件与下载位置

为避免超过GitHub单文件100 MB限制，以下内容不会上传。`git clone`后无需手工寻找文件，使用对应命令即可下载到脚本预期位置：

| 未上传内容 | 本地目标位置 | 约占用空间 | 下载来源 | 准备命令 |
|---|---|---:|---|---|
| MiniMind-3权重 | `q2-split-inference/assets/model.safetensors` | 122 MiB | `jingyaogong/minimind-3`固定文件 | `q2-split-inference/download-model.ps1` |
| Qwen3-1.7B BF16权重及Tokenizer | `assets/qwen3-1.7b/` | 约3.8 GiB | `Qwen/Qwen3-1.7B`固定Revision | `download-qwen3.ps1` |
| Python虚拟环境 | `q2-split-inference/.venv/` | 约1～2 GiB | PyTorch/PyPI镜像 | `q2-split-inference/setup.ps1` |
| Wheel下载缓存 | `q2-split-inference/wheels/` | 约400 MiB | PyTorch/PyPI镜像 | 由`setup.ps1`自动下载 |
| 运行日志和PID | 各目录`runtime/` | 随实验变化 | 本地运行生成 | 无需下载 |

MiniMind权重和Tokenizer使用SHA-256校验；Qwen3下载固定在Revision `70d244cc86ccca08cf5af4e1e306ecf908b1ad5e`，并按照仓库中的`assets/qwen3-1.7b.manifest.json`逐文件校验大小和SHA-256。

直接准备全部模型：

```powershell
powershell -ExecutionPolicy Bypass -File .\q2-split-inference\download-model.ps1
powershell -ExecutionPolicy Bypass -File .\download-qwen3.ps1
```

Q1、Q2、Q3主实验和Q4的MiniMind校准共享同一份MiniMind权重，不需要重复下载。Qwen3只用于补充验证；不运行Qwen3实验时，可以不下载约3.8 GiB权重。

## 一条命令复现

所有命令均从仓库根目录执行。

### Q1：Hidden State反演与安全—可用性

```powershell
powershell -ExecutionPolicy Bypass -File .\q1-poc\run.ps1
powershell -ExecutionPolicy Bypass -File .\q1-poc\run-tradeoff.ps1
```

### Q2：启动分割推理并完成对齐

```powershell
powershell -ExecutionPolicy Bypass -File .\q2-split-inference\verify-all.ps1
```

服务启动后，可直接请求：

```powershell
$body = @{prompt="中国的首都是哪里？"; max_new_tokens=16; stream=$false} | ConvertTo-Json
Invoke-RestMethod -Uri http://127.0.0.1:8100/generate -Method Post `
  -ContentType "application/json; charset=utf-8" `
  -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

MiniMind GSM8K：

```powershell
powershell -ExecutionPolicy Bypass -File .\q2-split-inference\gsm8k.ps1
```

Qwen3-1.7B补充实验需要先下载约4GB BF16权重：

```powershell
powershell -ExecutionPolicy Bypass -File .\download-qwen3.ps1
powershell -ExecutionPolicy Bypass -File .\qwen3-validation\run.ps1
powershell -ExecutionPolicy Bypass -File .\qwen3-validation\run-gsm8k.ps1
```

### Q3：优化前后实测

```powershell
powershell -ExecutionPolicy Bypass -File .\q3-scheduler-poc\single-slot-bd.ps1
powershell -ExecutionPolicy Bypass -File .\q3-scheduler-poc\single-slot-decode-sweep.ps1
```

### Q4：容量模型

直接执行完整校准与预测：

```powershell
powershell -ExecutionPolicy Bypass -File .\q4-capacity-model\run.ps1
```

只执行指定Profile的估算：

```powershell
.\q2-split-inference\.venv\Scripts\python.exe .\q4-capacity-model\estimate.py `
  --model .\q4-capacity-model\profiles\models\qwen3-32b.json `
  --hardware .\q4-capacity-model\profiles\hardware\h20x8.json `
  --workload .\q4-capacity-model\profiles\workloads\4k-slo.json
```

## 仓库结构

```text
.
├─ README.md                         # 项目总入口
├─ readme_first.md                   # 个人理解与复盘入口
├─ interview-challenge-ZH aug.md     # 原始任务书
├─ q1-industry-security-solutions.md # Q1行业调研
├─ q1-hidden-state-security-evaluation.md
├─ q1-poc/                           # Q1安全PoC
├─ q2-split-inference/               # Q2分割推理服务与GSM8K
├─ q3-scheduler-poc/                 # Q3调度优化与实测
├─ q4-capacity-model/                # Q4容量模型与校准
├─ qwen3-validation/                 # Qwen3补充实验
└─ docs/                             # 交付清单、Demo与Agent复盘
```

## 结果与证据口径

- `results/*.json`为原始或汇总实验数据，保留在仓库中；
- SVG图表直接引用对应JSON结果；
- MiniMind实验是真实CPU模型执行；
- 500 km / 10 Gbps通过传播时延、序列化与共享链路排队模拟；
- Qwen3-32B + 8×H20为假设硬件Profile下的容量外推，不是目标GPU实测；
- Qwen3原始模型Q4量化结果与BF16分割模型结果使用不同后端，不能把差值直接归因于分割。

## GitHub交付

提交前请查看[交付清单](docs/DELIVERY_CHECKLIST.md)。仓库需要分享给GitHub用户`fxlin`。Agent完整对话记录和1分钟Demo视频需要在最终提交前由作者导出/录制并放入指定目录。
