# Q4 PoC：分割推理容量模型与 QPS 预测

> **TTFT/TPOT 衡量单个请求的快慢与流畅度，最大请求率衡量整个系统在保证体验的前提下能接多少活。**

本 PoC 将 TTFT 和 TPOT 的目标值作为 SLO 约束，求解同时满足延迟、利用率和显存约束时可持续承载的最大 QPS，并给出该最大 QPS 下实际预测的 TTFT、TPOT 和限制条件。

> **任务边界：**优化前后的真实模型性能对比已经在 Q3 PoC 中完成，包括严格串行与请求间流水线的云侧利用率、吞吐和相对成本对比。Q4 不重复实现该 before-after 实验，而是以 Q3 验证过的优化后调度方案为建模对象，重点回答：给定模型、GPU、工作负载和 TTFT/TPOT SLO，系统能够持续承载的最大 QPS 是多少。

## 最终结果

在以下明确假设下，修正后的模型预测最大请求率约为 **0.97 QPS**：

- 模型：Qwen3-32B BF16，企业侧保留 Embedding/LM Head，云侧约 31.244B 参数；
- 云端：8 × NVIDIA H20，采用 profile 中的假设规格与效率；
- 请求：输入 4096 token、输出 256 token；
- 网络：500 km、10 Gbps，理想传播时延 2.5 ms/单程；
- SLO：TTFT ≤ 3 s、TPOT ≤ 100 ms；
- 调度：3 ms 窗口，粗粒度扫描后用完整模拟复核的配置为 prefill batch 上限 1、decode batch 上限 8。

预测点：P95 TTFT 2.889 s、P95 TPOT 87.8 ms、云端资源负载 89.9%。限制 QPS 的是 **90% 资源利用率上限**，TTFT/TPOT 和显存仍满足约束。目标 H20 未在本机实测，因此 0.97 QPS 仍是基于假设硬件 Profile 的外推结果，不是 H20 实测值。

### 原始模型与分割推理容量对比

在模型、硬件、4K 输入、256 Token 输出和 SLO 均不变的情况下，取消企业侧分割边界和 Hidden State 往返传输，将完整 Qwen3-32B 直接部署在云端。使用 1000 个请求进行相同的 Token 级离散事件模拟，结果如下：

| 方案 | 最大 QPS | P95 TTFT | P95 TPOT | 云侧利用率 |
|---|---:|---:|---:|---:|
| 分割推理 | 0.968 | 2.889 s | 87.8 ms | 89.9% |
| 原始模型直接推理 | **1.001** | 3.000 s | 86.9 ms | 89.8% |

原始模型直接推理的容量约提高 **3.4%**。提升有限，是因为当前负载的主要瓶颈是 4K 输入的 Transformer Prefill 计算，而不是 500 km / 10 Gbps 链路。取消分割只能省去企业侧边界处理和 Hidden State 往返等待，无法消除主体 Prefill 和 Decode 计算。因此，当前条件下分割推理使最大 QPS 从约 1.00 降至约 0.97，主要代价体现在端到端时延，而非数量级上的吞吐下降。

> **建模边界：**当前模型近似认为两种方案的 Transformer 主体计算量相同。如果实际切分导致部分层重复计算、KV Cache 连续性被破坏或产生额外序列化开销，分割推理的实际容量损失会更大。以上结果属于同一预测模型下的反事实对照，不是 8×H20 实机测量。

## 建模方法

PoC 使用 Roofline 估算单个云侧 Batch 的计算下限，再用 Token 级离散事件模型模拟端到端调度：

```text
prefill_time = max(prefill_FLOPs / effective_compute,
                   cloud_weight_bytes / effective_bandwidth)

decode_time  = max(decode_FLOPs / effective_compute,
                   (cloud_weight_bytes + KV_bytes) / effective_bandwidth)

TTFT = 企业侧输入边界处理（Tokenizer / Embedding）
       + prefill hidden state 上行
       + batch 等待与排队
       + 云侧 Transformer Prefill
       + prefill hidden state 下行
       + 企业侧输出边界处理（Norm / LM Head / 首 Token 采样）

TPOT = 企业侧输入边界处理（上一 Token 的 Embedding）
       + decode hidden state 上行
       + batch 等待与摊销排队
       + 云侧 Transformer Decode
       + decode hidden state 下行
       + 企业侧输出边界处理（Norm / LM Head / 采样）
```

这里的 Prefill 和 Decode 主体均指云侧 Transformer 计算；企业侧不执行 Transformer Prefill/Decode，只执行分割边界之外的 Embedding、Norm、LM Head 和采样。当前 workload profile 中的兼容字段 `enterprise_prefill_ms` 与 `enterprise_decode_ms`，分别表示上述 Prefill/Decode 路径上企业侧输入、输出边界处理耗时的合计，不表示企业侧执行完整 Prefill 或 Decode。后续版本可进一步拆成输入 Embedding 和输出 Head 两项，以分析阶段重叠。

Prefill FLOPs 包含各层 Q/K/V/O、SwiGLU 三个矩阵乘和随序列长度平方增长的 attention；decode 同时计算权重读取和 KV Cache 读取。旧版使用 M/M/1 平均排队时间近似 P95，且将排队时间均匀分摊到输出 Token，导致容量预测偏高。修正版对每个请求逐 Token 模拟：

- 独立的 Prefill 和 Decode ready queue；
- 每轮 hidden state 上下行和企业侧边界处理；
- Decode 优先，但最多连续执行 4 个 Decode batch；
- Prefill 500 ms 饥饿保护；
- 动态 Batch 上限和等待窗口；
- 固定随机种子的请求到达过程；
- 直接从逐请求时间戳计算 P95 TTFT 和 P95 TPOT。

搜索器逐步提高 QPS，同时检查：

- TTFT SLO；
- TPOT SLO；
- 最大资源利用率；
- 权重和并发 KV Cache 显存；
- 实际并发数能否填满配置的 batch。

## 本地校准和验证

`benchmark_local.py` 直接执行 Q2 的真实 MiniMind Transformer，测量多个序列长度和 batch 的 prefill/decode，不用 sleep 模拟计算。`calibrate.py` 使用短上下文样本拟合有效算力与有效带宽，将最长的 64-token 样本保留为验证集。

本机本次结果：

| 项目 | 结果 |
|---|---:|
| 拟合有效计算吞吐 | 50.1 GFLOP/s |
| 拟合有效内存带宽 | 4.0 GB/s |
| 训练集 MAPE | 18.0% |
| 64-token holdout MAPE | 14.9% |
| Q3 独立交叉检查 | 56 个逻辑任务合并为 20 个物理 batch |

CPU 拟合参数只用于验证公式是否能描述本地长度/batch 趋势，**不会外推成 H20 性能**。H20 预测使用独立、可编辑的硬件 profile。

### 端到端容量验证实验

Q2 的 microbenchmark 只能校准单次 Prefill/Decode 计算，不能验证排队模型和最大 QPS。为此另设开放到达率实验，直接复用 Q2 的真实 MiniMind 分割推理服务和 Q3 的共享链路实现：

```text
固定 MiniMind、单云执行槽、Decode 优先、关闭动态 Batch
固定 500 km / 10 Gbps 全双工共享链路
                    ↓
按泊松过程持续注入请求，而不是一次性并发突发
                    ↓
先粗扫，再围绕 SLO 边界密集扫描
                    ↓
测量 P95 TTFT、P95 TPOT、云侧利用率和队列排空时间
                    ↓
求满足 SLO 且可稳定排空的最高到达率
```

每个档位独立重启服务并使用相同随机种子。有限时长内固定请求总数为 `round(QPS × duration)`，并按照“给定请求总数条件下的泊松到达时刻”随机分布，避免实际平均 QPS 严重偏离配置档位。验收条件为：

- `P95 TTFT <= 3 s`；
- `P95 TPOT <= 100 ms`；
- 到达调度 P95 滞后不超过 100 ms；
- 流量停止后 10 秒内排空队列。

一键运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\q4-capacity-model\validate-q2-q3.ps1
```

正式120秒边界实验使用当前 PowerShell 会话传递数组：

```powershell
& .\q4-capacity-model\validate-q2-q3.ps1 `
  -Rates @(0.10,0.13,0.16,0.17) -DurationSeconds 120
```

正式汇总结果写入 `results/q2-q3-open-loop-validation-120s.json`，逐档原始结果写入 `results/open-loop-validation/`，服务日志写入 `runtime/open-loop-validation/`。最高合格档位与首个失败档位共同构成实测容量区间。由于真实生成可能提前遇到 EOS，原始结果同时保留实际生成 Token 数和 Token 吞吐。

本机120秒边界实验结果如下：

| 配置档位 | 实际到达率 | P95 TTFT | P95 TPOT | 云侧利用率 | 稳定且满足 SLO |
|---:|---:|---:|---:|---:|---|
| 0.10 QPS | 0.100 QPS | 195 ms | 98.4 ms | 22.2% | 是 |
| 0.13 QPS | 0.133 QPS | 233 ms | 99.0 ms | 27.9% | 是 |
| 0.16 QPS | 0.158 QPS | 236 ms | 98.8 ms | 32.7% | 是 |
| 0.17 QPS | 0.167 QPS | 246 ms | 116.8 ms | 45.3% | 否：TPOT 超限 |

因此当前实测容量被夹在：

```text
0.158 QPS <= 最大可持续 QPS < 0.167 QPS
```

首个失败档位仍能及时排空，失败原因不是系统吞吐崩溃，而是 P95 TPOT 先超过 100 ms，符合“由单请求体验 SLO 限制最大 QPS”的定义。与45秒初测相比，120秒实验暴露出明显的CPU热态和尾部抖动，说明短实验不适合确定P95容量。

`generate_calibration.py` 自动读取 Q2 拟合结果和 0.10/0.13 QPS 两个低负载实测点，生成 CPU 硬件 Profile、企业侧边界开销和云侧服务时间修正系数；0.158/0.167 QPS 不参与拟合，用作容量验证：

| 自动校准参数 | 本次生成值 |
|---|---:|
| 有效计算吞吐 | 50.1 GFLOP/s |
| 有效内存带宽 | 4.0 GB/s |
| 云侧服务时间修正系数 | 0.521 |
| 企业侧 Prefill 边界开销 | 120.2 ms |
| 企业侧 Decode 边界开销 | 52.9 ms |

| 项目 | 旧 M/M/1 模型 | Token 级离散事件模型 | 实测 |
|---|---:|---:|---:|
| 最大 QPS | 0.615 | **0.108** | `[0.158, 0.167)` |
| 首要限制 | TTFT | **TPOT** | **TPOT** |
| 相对已确认通过的 0.158 QPS | +288% | **-31.8%** | 基准 |

自动校准模型正确识别 TPOT 瓶颈，但预测0.108 QPS低于实测区间，属于约31.8%的保守估计。原因是离散事件模拟使用更长的随机到达序列，产生的尾部排队强于本次单随机种子实测。结果保存在 `results/minimind-open-loop-corrected.json`，校准参数及推导过程保存在 `results/calibration-parameters.json`。最终结论应同时报告预测误差，而不能只报告目标GPU点估计。

## GPU 敏感性矩阵

目标 GPU 未在本机实测，因此 `sweep.py` 自动扫描计算效率和显存带宽效率：先用小样本选择 Batch，再对保守、中性、乐观三个入选配置执行完整300请求模拟。结果不是统计置信区间，而是硬件效率假设范围。

| 场景 | 计算效率 | 带宽效率 | TP效率 | 最大QPS | P95 TTFT | P95 TPOT | 利用率 | 限制条件 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 保守 | 30% | 50% | 82% | 0.566 | 3.000s | 82.3ms | 82.2% | TTFT |
| 中性 | 42% | 65% | 82% | **0.968** | 2.889s | 87.8ms | 89.9% | 资源利用率 |
| 乐观 | 55% | 75% | 82% | 1.249 | 2.196s | 70.0ms | 89.9% | 资源利用率 |

因此，当前假设下 Qwen3-32B + 8×H20 的预测范围为 `0.566～1.249 QPS`，中性点为 `0.968 QPS`。完整结果保存在 `results/qwen3-32b-h20x8-sweep.json`。

### 量级复核与外部基准对照

Qwen3-32B 的单请求4K Prefill按当前模型结构约需261.1 TFLOPs。8张H20按每卡148 TFLOP/s计算，总峰值为1184 TFLOP/s，因此即使不考虑Decode、网络、排队和任何效率损失，纯Prefill的物理上限也只有：

```text
1184 / 261.1 = 4.53 QPS
```

当前中性有效算力为407.8 TFLOP/s，对应纯Prefill上限1.56 QPS；再加入每请求256个输出Token、90%利用率限制和SLO约束后得到0.968 QPS。即使把计算、带宽和多卡效率全部设为100%，完整模型在90%利用率约束下也只预测2.79 QPS。因此，“单个8×H20实例、BF16、每请求独立4K输入、无Prefix Cache”达到15 QPS与当前硬件算力上限不相容。

公开结果必须按工作负载和SLO区分：

- [Qwen官方速度基准](https://qwen.readthedocs.io/en/latest/getting_started/speed_benchmark.html)给出了Qwen3-32B在不同上下文和精度下的Token速度，但不是本题4K/256、P95 TTFT/TPOT约束下的在线QPS；
- [vLLM用户提交的单H100 NVL结果](https://github.com/vllm-project/vllm/issues/17788)中，Qwen3-32B BF16达到3.26 req/s，但平均输入仅约217 Token，且平均TTFT约147秒，不能与`TTFT <= 3s`的容量比较；
- [llm-d的Qwen3-32B基准](https://github.com/llm-d/llm-d/blob/main/guides/precise-prefix-cache-routing/benchmark-results/vllm-qwen3-32b-h100.md)在16张H100、8个TP=2副本和大规模共享Prefix Cache下达到更高请求率；它证明15 QPS可在多副本和缓存命中的集群条件下实现，但不是单个8卡、每请求重新Prefill的对照组。

若目标确实要求15 QPS，需要改变至少一项前提：使用大量Prefix Cache命中、显著缩短输入、改用FP8/INT4、增加GPU或多副本，或者放宽TTFT/TPOT SLO。

## 一键复现

在 `interview-challenge` 目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\q4-capacity-model\run.ps1
```

它会依次执行本地 microbenchmark、自动生成校准 Profile、本地容量预测、目标GPU预测和敏感性扫描。端到端开放流量原始数据耗时较长，由 `validate-q2-q3.ps1` 单独生成。结果写入：

- `results/local-benchmark.json`：本地原始测量；
- `results/local-validation.json`：逐样本 predict-vs-measure；
- `results/calibration-parameters.json`：自动推导的边界开销和云侧服务修正系数；
- `results/minimind-open-loop-corrected.json`：修正模型对本地 MiniMind 容量的预测；
- `results/qwen3-32b-h20x8.json`：指定配置预测；
- `results/qwen3-32b-h20x8-sweep.json`：batch/效率敏感性扫描。

单独估算其他 profile：

```powershell
.\q2-split-inference\.venv\Scripts\python.exe .\q4-capacity-model\estimate.py `
  --model .\q4-capacity-model\profiles\models\qwen3-32b.json `
  --hardware .\q4-capacity-model\profiles\hardware\h20x8.json `
  --workload .\q4-capacity-model\profiles\workloads\4k-slo.json
```

## 参数来源与边界

Qwen3-32B 的 5120 hidden size、64 层、64/8 个 Q/KV heads、25600 intermediate size 和 BF16 来自 [Qwen 官方模型配置](https://huggingface.co/Qwen/Qwen3-32B/blob/main/config.json)。计算与带宽取较慢者的原则参考 [NVIDIA 深度学习性能文档](https://docs.nvidia.com/deeplearning/performance/dl-performance-getting-started/index.html)。

公开渠道缺少稳定、完整且可直接核验的 H20 官方规格页，因此 `h20x8.json` 明确标为 **assumption profile**：每卡 96 GB、4 TB/s、BF16 148 TFLOP/s，计算/带宽/TP 效率分别假设为 42%/65%/82%。面试时应主动说明：换成实机 benchmark 数据后，需要替换这些字段并重新校准，0.97 QPS 不是 H20 实测结果。

当前模型还没有模拟 chunked prefill、prefill/decode 分离、投机解码、真实 TP 通信拓扑和请求长度分布；云侧 Batch 服务时间也仍由 Roofline 与效率 Profile 给出。这些是从 PoC 升级到生产容量规划器时最优先的改进点。
