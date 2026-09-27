# Q1 Hidden State 上云方案安全评估

## 1 结论

题目方案不能保证 prompt 和生成 token 对云服务商不可见。

对于 Embedding 层输出，攻击本质上就是反向查表：

```text
正向推理：
文本 → tokenizer → token ID → 查询 Embedding 表 → hidden state → 上传云端

反向攻击：
hidden state → 在 Embedding 表中查找最近向量 → token ID → tokenizer 解码 → 文本
```

正向过程中，token `t_i` 的 hidden state 就是 Embedding 表 `E` 的第 `t_i` 行：

```text
h_i = E[t_i]
```

因此，已知 `h_i` 和 Embedding 表的攻击者可以反向找到最相似的一行：

```text
t_hat_i = argmax_j cosine(h_i, E[j])
```

在未经扰动的 Embedding 切分点，`h_i` 原本就是表中的一行，所以这不是根据语义猜测，而是从 hidden state 反查 token ID，再由 tokenizer 还原文本。

如果企业侧只运行 Embedding，上传的每个向量就是词嵌入矩阵中的一行。云端知道开源模型权重时，只需做一次最近邻查询即可恢复 token。使用 MiniMind-3 真实权重的 PoC 对 5 条中英文敏感 prompt、共 309 个 token 的恢复率为 **100%**。

因此，hidden state 是派生数据，不是密文。该方案可以隐藏自然语言的直接表示，但不能作为强保密机制。

## 2 被评估的方案

```text
企业：Embedding 或前层
   → hidden state
云端：Transformer 中间层
   → hidden state
企业：尾层和 Unembedding
   → token
```

分析假设云端是“诚实但好奇”的攻击者：它按协议完成推理，同时保存上传的 hidden state，并知道开源模型、tokenizer 和 embedding 权重。网络已经使用 TLS，但 TLS 不阻止云端读取自己收到的数据。

## 3 Embedding 反演攻击

设词嵌入矩阵为 `E ∈ R^(V×d)`，输入 token 为 `t_i`。Embedding 输出为：

```text
h_i = E[t_i]
```

攻击者枚举词表并寻找与 `h_i` 最相似的行：

```text
t_hat_i = argmax_j cosine(h_i, E[j])
```

由于 `h_i` 本来就是 `E` 中的一行，在没有扰动时该攻击不是复杂的语义猜测，而是反向查表。计算复杂度为 `O(sequence_length × vocab_size × hidden_size)`，可用矩阵乘法高效完成。

对自回归 decode 也存在同样问题：企业每生成一个 token，下一轮会再次把该 token 的 embedding 发往云端，因此云端可以持续恢复生成序列。

更深层的 hidden state 不再等于 embedding 表中的一行，但仍保留词法和语义信息。ICML 2025 的研究已展示从多种开源 LLM hidden state 近乎完整恢复 prompt 的攻击。[Hidden No More](https://proceedings.mlr.press/v267/thomas25b.html)

## 4 PoC 实测

PoC 使用：

- 模型：`jingyaogong/minimind-3`；
- 真实 embedding：`6400 × 768`，FP16；
- 攻击：余弦最近邻；
- 输入：5 条中英文敏感 prompt；
- token：使用 MiniMind tokenizer 的可逆 ByteLevel 基础 token，共 309 个。

| 实验 | Token 恢复率 | 表示余弦相似度 | 结论 |
|---|---:|---:|---|
| 原始 Embedding | 100.00% | 1.000 | 原文完整恢复 |
| 加噪 `σ = 4 × embedding RMS` | 99.03% | 0.225 | 表示已严重失真，攻击仍基本成功 |
| 加噪 `σ = 8 × embedding RMS` | 31.39% | 0.116 | 恢复率下降，但模型可用信息也大幅损失 |
| 加噪 `σ = 16 × embedding RMS` | 3.24% | 0.057 | 几乎破坏原表示 |
| 两方秘密共享，仅观察一份 | 0.00% | 不适用 | 单份 share 与输入统计独立 |
| 两方 share 合并 | 100.00% | 近似无损 | 能恢复量化后的表示 |

这组加噪实验只用余弦相似度作为表示失真的代理指标，并未把噪声送入完整模型测下游精度。因此它能证明“降低恢复率需要很强扰动”，但不能替代后续 GSM8K 或生成质量测试。

## 5 其他安全问题

| 风险 | 说明 |
|---|---|
| 输入机密性 | Embedding 可直接反查；更深 hidden state 仍可能被训练式或优化式反演 |
| 输出机密性 | decode 时新 token 的 embedding 再次上传，可逐步恢复生成内容 |
| 元数据泄漏 | 序列长度、请求时间、输出长度和并发模式仍然可见 |
| 完整性 | 云端可以篡改 hidden state 或不按指定模型执行，原方案没有验证机制 |
| 重放和关联 | 云端可保存表示、比较相同或相似请求并建立用户画像 |
| 日志和缓存 | activation、KV cache、调试数据和崩溃转储都可能形成长期副本 |

## 6 高斯噪声的安全—效用结论

将同一噪声定义实际注入 MiniMind 的每次 prefill/decode 上行 hidden state 后，得到：σ=0.5 时最近邻攻击恢复率仍为 100%，但输出 token 一致率已降至 0.94%，首 token 和完整序列一致率均为 0%；直到 σ=8 时攻击恢复率才降至 31.39%，此时输出 token 一致率为 0%。

因此普通高斯噪声没有可用的折中区间：它在提供实质隐私收益之前就破坏了推理效用。图和逐 prompt 原始数据见 `q1-poc/results/noise-utility-tradeoff.svg` 与 `noise-utility-tradeoff.json`。

## 7 缓解方案评价

### 6.1 推荐方案：TEE 与远程证明

让云端中间层运行在 CPU/GPU TEE 内。企业验证硬件、固件和推理镜像的远程证明后，再建立加密会话并释放短期密钥。普通云管理员只能看到密文，TEE 内部才能解密 hidden state。

- 优点：保留普通 Transformer 算子，性能最接近原生推理；
- 代价：需要支持机密计算的 GPU、证明服务和密钥释放系统；数据搬运及启动存在额外开销；
- 边界：TEE 内恶意代码、侧信道、日志和 DoS 仍需单独处理。

### 6.2 防御性措施：更深切分、加噪、量化或降维

这些方法可能降低特定反演攻击效果，但不能提供密码学保证。切分更深会增加企业端算力；强噪声和压缩会降低模型精度；攻击者还可能针对防御重新训练反演器。

PoC 显示，MiniMind-3 Embedding 在表示余弦相似度下降到约 0.12 后，简单最近邻恢复率才明显下降。因此“轻微加噪即可安全”并不成立。

### 6.3 强密码学方案：MPC 或秘密共享

PoC 中，两方加法秘密共享使任一单方的恢复率降到 0%，两份合并后仍能恢复表示。但普通 Transformer 不能直接在单份 share 上运行；矩阵运算、非线性函数和采样都必须改成 MPC 协议，通信与延迟成本很高。

### 6.4 不应依赖的措施

- TLS：只防链路窃听，云端仍收到 hidden state；
- 私有模型或隐藏 embedding 权重：属于安全性依赖保密实现，权重可能泄漏，也可能通过已知输入或辅助数据学习映射；
- 随机旋转或维度置换：若云端要运行后续层，相关变换通常也体现在云端权重中，缺少可证明安全性；
- 只保留首尾层：控制了 token 的直接接口位置，但没有消除中间表示的信息。

## 8 最终判断

| 问题 | 判断 |
|---|---|
| 云端能否看到原始 prompt？ | 能。Embedding 切分点可实现 100% token 恢复。 |
| 云端能否看到生成 token？ | 能。每轮 decode 再次上传新 token 的 embedding。 |
| 增加本地层数是否足够？ | 只能降低部分攻击效果，不能自动形成安全保证。 |
| 最现实的增强是什么？ | GPU TEE、远程证明、企业持钥和条件密钥释放。 |
| 不信任 TEE 时怎么办？ | 使用 HE/MPC，但要接受显著性能和工程成本。 |

结论是：**保留 Embedding 和 Unembedding 在企业侧，不等于输入和输出对云端不可见。若仍传输明文 hidden state，该方案至多是混淆，不是保密。**

## 9 复现

从 `interview-challenge` 目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\q1-poc\run.ps1
```

实现说明见 [`q1-poc/README.md`](q1-poc/README.md)，机器可读结果生成在 `q1-poc/results/results.json`。
