# Q3 PoC：分割推理的并发流水线优化

## 1. 交付结论

本PoC以 **MiniMind真实分割推理**为主实验，对比严格串行与请求间流水线，并使用Decode优先和并发上限约束TPOT。

在相同MiniMind模型、32个请求、每请求最多输出64 Token，以及相同500 km / 10 Gbps共享链路下：

| 指标 | 严格串行 | 请求间流水线 | 变化 |
|---|---:|---:|---:|
| 请求吞吐 | 0.40 req/s | 1.34 req/s | **3.35×** |
| Token吞吐 | 10.66 token/s | 35.74 token/s | **3.35×** |
| 云执行槽利用率 | 34.56% | 99.24% | **+64.68个百分点** |
| 相对单位成本 | 93.77 slot-s/千Token | 27.98 slot-s/千Token | **-70.16%** |
| 输出一致性 | 基准 | 与基准逐项一致 | 通过 |

无约束的32请求流水线会使P95 TPOT恶化到1024.55 ms。因此最终策略是：

```text
请求间流水线 + Decode优先 + 并发上限
```

本机实测中，并发度3是满足`P95 TPOT <= 100 ms`的最高并发度，此时Token吞吐为17.52 token/s，P95 TPOT为96.74 ms。

## 2. 问题与优化原理

一次分割推理包含：

```text
企业侧 Embedding
  → hidden state上行
  → 云侧Transformer
  → hidden state下行
  → 企业侧Norm + LM Head + 采样
  → 下一轮Decode
```

严格串行时，当前请求进行网络传输和企业侧计算期间，云端没有其他工作可执行，但云资源仍在计费。流水线用其他已经就绪的请求填补空档：

```text
时间片                1       2       3       4       5       6       7       8       9

严格串行              A·P     等待    A·D     B·P     等待    B·D     C·P     等待    C·D
云侧                 █████   ░░░░░   █████   █████   ░░░░░   █████   █████   ░░░░░   █████

请求间流水线          A·P     B·P     C·P     A·D     B·D     C·D
云侧                 █████   █████   █████   █████   █████   █████

P = Prefill，D = Decode，░ = 云端等待但资源仍在计费
```

流水线不减少单请求的模型计算量，收益来自隐藏网络和企业侧处理等待。高并发虽然能提高利用率，但多个请求轮流Decode会增大TPOT。因此调度器采用：

1. 允许不同请求交错执行；
2. Ready Queue优先调度Decode；
3. 设置最大并发度保护TPOT；
4. 保留Prefill防饥饿机制，避免新请求长期拿不到首Token。

## 3. MiniMind主实验设计

### 3.1 实现边界

PoC直接复用Q2的分割模型：

- 企业侧：Tokenizer、Embedding、Norm、LM Head和采样；
- 云侧：MiniMind Transformer与逐请求独立KV Cache；
- 云执行槽：1个，用于明确测量等待与流水线收益；
- 链路：全双工共享链路，上、下行分别为10 Gbps；
- 距离：500 km，按光纤传播下限约`5 μs/km`，单向传播时延2.5 ms；
- 传输量：按照Hidden Tensor的真实FP32字节数计算；
- 模型计算：真实执行MiniMind，不使用`sleep`代替推理。

`shared_link_server.py`实现共享链路，上、下行分别排队，因此并发请求会竞争同一方向的带宽。

### 3.2 B/D主对比

| 条件 | B：严格串行 | D：请求间流水线 |
|---|---|---|
| 请求数 | 32 | 32 |
| 每请求最大输出 | 64 Token | 64 Token |
| 云执行槽 | 1 | 1 |
| 动态Batch | 关闭 | 关闭 |
| 云侧调度 | FIFO | FIFO |
| 请求并发 | 严格串行 | 32 |
| 链路 | 500 km / 10 Gbps共享全双工 | 相同 |

实验只改变请求能否交错执行，用于隔离流水线隐藏等待的收益。

### 3.3 Decode优先消融

保持模型、请求、链路和单云执行槽不变，启用Decode优先，分别测试并发度2、3和4，验收目标为`P95 TPOT <= 100 ms`。

## 4. MiniMind实测结果

### 4.1 严格串行与流水线

| 指标 | B：严格串行 | D：流水线 | 变化 |
|---|---:|---:|---:|
| 总墙钟时间 | 80.08 s | 23.90 s | -70.16% |
| 请求吞吐 | 0.40 req/s | 1.34 req/s | 3.35× |
| Token吞吐 | 10.66 token/s | 35.74 token/s | 3.35× |
| 云执行槽利用率 | 34.56% | 99.24% | +64.68个百分点 |
| 非计算空档占比 | 65.44% | 0.76% | -64.68个百分点 |
| P95 TTFT | 77.20 s | 2.50 s | -96.76% |
| P95 TPOT | 95.58 ms | 1024.55 ms | 恶化 |
| 成本代理 | 93.77 slot-s/千Token | 27.98 slot-s/千Token | -70.16% |
| 输出一致性 | 基准 | 与基准逐项一致 | 通过 |

流水线显著提高吞吐、利用率并降低单位成本，但无约束的高并发破坏了单请求的生成连续性。这里的“非计算空档”包括网络往返、企业侧LM Head/采样、HTTP和调度开销，不能解释为纯网络等待。

### 4.2 Decode优先与并发上限

| 指标 | 并发2 | 并发3 | 并发4 |
|---|---:|---:|---:|
| Token吞吐 | 15.23 token/s | 17.52 token/s | 20.29 token/s |
| 云执行槽利用率 | 46.96% | 51.48% | 56.61% |
| P95 TTFT | 235 ms | 311 ms | 437 ms |
| P95 TPOT | 90.73 ms | **96.74 ms** | 109.09 ms |
| 成本代理 | 65.67 slot-s/千Token | 57.08 slot-s/千Token | 49.29 slot-s/千Token |
| 满足TPOT ≤ 100 ms | 是 | **是** | 否 |
| 输出与主实验一致 | 是 | 是 | 是 |

并发4具有更高吞吐和更低单位成本，但已经违反TPOT SLO。因此当前推荐`decode-first + max_concurrency=3`。

## 5. 指标与成本口径

| 指标 | 定义 |
|---|---|
| 云执行槽利用率 | 云侧真实模型计算时间 / 实验墙钟时间 |
| 请求吞吐 | 完成请求数 / 实验墙钟时间 |
| Token吞吐 | 生成Token数 / 实验墙钟时间 |
| TTFT | 请求发出到首个生成Token返回的时间 |
| TPOT | 首Token后，相邻输出Token的平均间隔 |
| 非计算空档 | 墙钟时间减去云侧模型计算时间 |
| 成本代理 | 云执行槽秒数 / 千生成Token |

PoC不绑定特定云厂商价格。执行槽数量和单位时间价格固定时，`slot-seconds / 1k tokens`与实际GPU单位输出成本成正比，适合比较相对成本，但不是人民币或美元账单。

本实验运行在CPU笔记本上，“云侧利用率”表示PoC云执行槽忙时占比，不等于生产GPU的SM利用率。生产环境需要使用Profiler或DCGM重新测量。

## 6. 一键复现MiniMind主实验

从`interview-challenge`目录执行。以下文件必须存在：

```text
q2-split-inference/.venv/Scripts/python.exe
q2-split-inference/assets/model.safetensors
q2-split-inference/assets/tokenizer.json
```

### 6.1 B/D主实验

```powershell
powershell -ExecutionPolicy Bypass -File .\q3-scheduler-poc\single-slot-bd.ps1
```

默认运行32个请求、每请求最多生成64 Token，也可以显式指定：

```powershell
powershell -ExecutionPolicy Bypass -File .\q3-scheduler-poc\single-slot-bd.ps1 `
  -Requests 32 -MaxNewTokens 64
```

主结果位于`results/single-slot-BD-comparison.json`，原始结果位于`results/single-slot-B-serial*.json`和`results/single-slot-D-pipeline*.json`。

### 6.2 Decode优先实验

```powershell
powershell -ExecutionPolicy Bypass -File .\q3-scheduler-poc\single-slot-decode-sweep.ps1
```

汇总结果位于`results/single-slot-decode-sweep.json`；各并发度结果位于`results/decode-priority-c2.json`、`c3.json`和`c4.json`。日志位于`runtime/single-slot-bd/`和`runtime/single-slot-decode-sweep/`。

## 7. Qwen3-1.7B补充验证

MiniMind是Q3的主要交付模型。为验证结论是否只来自小模型特性，另用Qwen3-1.7B BF16真实执行Prefill和Decode，并用实测服务时间驱动同一单云槽、500 km / 10 Gbps链路上的8请求调度回放。

| 模型路径 | 调度方式 | 吞吐（QPS） | 云执行槽利用率 | 总耗时 |
|---|---|---:|---:|---:|
| 原始模型 | 串行 | 0.708 | 99.65% | 11.307 s |
| 原始模型 | 流水线 | 0.710 | 99.96% | 11.272 s |
| 分割模型 | 串行 | 0.698 | 98.25% | 11.468 s |
| 分割模型 | 流水线 | **0.710** | **99.96%** | **11.272 s** |

分割流水线相比分割串行，吞吐提高约1.73%，云执行槽利用率从98.25%提高到99.96%，基本隐藏了链路等待。

Qwen3收益较小，是因为本机CPU执行1.7B模型的计算时间远大于约20 ms的累计网络等待，串行方案已经接近饱和。结果验证了相同优化方向，但不能把1.73%的收益比例外推到8×H20。完整数据与复现脚本位于[`../qwen3-validation`](../qwen3-validation/README.md)。

## 8. 补充探索与P/D分离

| 实验 | 脚本 | 作用 | 证据类型 |
|---|---|---|---|
| 动态Batch / Prefill分块 | `benchmark.ps1` | 比较FIFO、Decode优先、分块与动态Batch | MiniMind实测 |
| 5用户、5分钟随机负载 | `sustained-benchmark.ps1` | 观察过载、排队和调度公平性 | MiniMind实测 |
| 固定企业2卡、云端8卡 | `fixed-2e8c.ps1` | 在固定物理预算下比较方案 | 校准后模拟 |
| 10个独立模型进程 | `real-10-process-2min.ps1` | 验证可运行2个企业进程和8个云进程 | MiniMind本机实测 |
| Prefill/Decode容量建模 | `pd-capacity.ps1` | 分析P/D资源配比与瓶颈 | 校准后模拟 |

P/D分离实验把Prefill和Decode视为不同资源池，用于分析资源配比。当前笔记本没有企业2卡和云端8卡，因此固定2+8矩阵及P/D容量结果不能表述为真实多GPU实测。

## 9. 最终结论与适用边界

1. MiniMind真实执行证明，多请求流水线能够填补网络和企业侧处理造成的云侧空档；
2. 主实验吞吐提高3.35倍，单位成本下降70.16%，且生成结果保持一致；
3. 无限制并发会严重恶化TPOT，必须使用Decode优先和并发上限；
4. 当前负载下推荐并发度3，P95 TPOT为96.74 ms；
5. MiniMind Hidden Size较小，10 Gbps链路未成为带宽瓶颈，主实验验证的是隐藏端到端等待；
6. Qwen3补充实验验证了相同优化方向，但CPU计算占比过高，使绝对收益较小；
7. 本地结果用于验证调度机制和测量方法，不能直接替代目标多GPU环境的绝对性能测试。
