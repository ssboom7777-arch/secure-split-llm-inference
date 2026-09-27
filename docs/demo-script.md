# 1分钟Demo脚本

## 0～10秒：问题与仓库

- 展示根README和Q1～Q4目录；
- 说明目标：验证Hidden State是否安全，实现分割推理，优化并发，并预测目标硬件容量。

## 10～22秒：Q1安全PoC

- 运行或展示`q1-poc/run.ps1`结果；
- 展示原始Embedding可被余弦最近邻100%还原；
- 展示噪声—还原率曲线，指出可用性先于还原率崩溃。

## 22～36秒：Q2分割推理

- 执行`q2-split-inference/run.ps1`；
- 用PowerShell或curl向企业侧`8100`发起问答；
- 展示企业侧—Hidden State—云侧Transformer—企业侧LM Head路径；
- 展示MiniMind单体/分割100%对齐及Qwen3分割GSM8K 27/50。

## 36～49秒：Q3优化

- 展示串行与流水线Timeline；
- 展示MiniMind before-after矩阵：吞吐3.35倍、利用率34.56%→99.24%、相对单位成本下降70.16%；
- 强调无限并发恶化TPOT，最终使用Decode优先和并发上限3。

## 49～60秒：Q4容量模型

- 展示输入Profile与`estimate.py`；
- 展示Qwen3-32B + 8×H20、4K/256、TTFT≤3s、TPOT≤100ms时约0.97 QPS；
- 说明该结果经过本地小模型校准，但目标GPU仍是外推，需实机Benchmark替换硬件Profile。
