# Q1 企业数据不出域的云端推理方案

> 范围：Q1 第一部分，调研日期 2026-09-21。

## 1 问题定义

企业希望使用云端模型和 GPU，但不希望云服务商、其他租户或攻击者读到 prompt、RAG 文档、会话、KV cache、输出、日志及身份元数据。

TLS 只保护传输，磁盘加密只保护存储；普通云进程执行推理时仍能看到明文。真正的问题是：**谁能看到使用中的数据，企业能否验证并约束它？** [NIST IR 8320](https://tsapps.nist.gov/publication/get_pdf.cfm?pub_id=934427)

## 2 什么叫数据不出企业

| 等级 | 含义 | 可满足的方案 |
|---|---|---|
| L0 物理不出域 | 原文、派生数据和计算均留在企业控制的设备与网络 | 本地部署、企业独占私有云 |
| L1 控制不出域 | 数据可进入托管环境，但地域、密钥和管理员由企业控制 | 托管私有云、专属硬件 |
| L2 明文不出可信边界 | 密文可以上云，普通云软件和管理员看不到明文 | TEE、HE、MPC |
| L3 标识符不出域 | 本地删除或替换已识别的敏感字段 | 脱敏、Tokenization |

数据驻留、数据主权、数据机密性和“不留存、不训练”不是一回事。报告必须先声明目标等级，不能笼统地说“数据不出企业”。

## 3 威胁模型

主要攻击者包括：

- 网络窃听者和中间人；
- 外部入侵者；
- 云端管理员或失陷的云控制面；
- 其他云租户和共享硬件侧信道攻击者；
- 恶意推理程序、模型或供应链组件；
- 企业内部人员和失陷终端；
- 能要求服务商交付数据的管辖权主体。

评价方案时至少看五点：

1. 云端能否恢复原文、语义或身份；
2. 能否检测代码、模型和结果被篡改；
3. 企业能否验证实际运行环境；
4. 数据是否进入日志、缓存、快照或训练集；
5. 性能、成熟度和关键信任假设。

## 4 保密方案与性能影响

| 保密方案名 | 方案 | 对推理性能的影响 | 引用 |
|---|---|---|---|
| 本地部署 | 模型、向量库、密钥和推理服务全部部署在企业控制边界内，原始数据和派生数据均不上云。 | 无公网传输延迟；性能由本地 GPU 决定。主要代价是硬件投入、峰值容量和较低利用率，而非单次推理额外开销。 | [NVIDIA NIM 部署模式](https://www.nvidia.com/en-us/ai-data-science/products/nim-microservices/) |
| 本地脱敏与 Tokenization | 上传前识别并删除、遮盖或替换姓名、证件号等字段；响应返回后在本地按需恢复。只保护被正确识别的字段。 | 增加一次本地检测和替换，通常开销较低；漏检会泄密，过度脱敏会降低摘要、问答等任务质量。 | [Google 转换方法](https://docs.cloud.google.com/sensitive-data-protection/docs/transformations-reference)；[NIST 去标识化风险](https://csrc.nist.gov/pubs/ir/8053/final) |
| 本地与云端分级路由 | 本地判断请求敏感度：高敏走本地模型，中敏脱敏后上云，需高质量且允许出域的请求进入可信云环境。 | 增加本地分类器延迟；普通请求仍接近原云服务性能。主要风险是误分类，而不是计算开销。 | [Apple Private Cloud Compute](https://security.apple.com/blog/private-cloud-compute/) |
| Split Inference | 企业运行 Embedding 或前若干层，只上传 hidden state；云端计算中间层，再把结果传回企业。 | 企业承担部分模型计算；hidden state 通常远大于 token，prefill 占带宽，decode 受逐轮 RTT 影响。hidden state 可被反演，不能视为密文。 | [Hidden No More](https://proceedings.mlr.press/v267/thomas25b.html) |
| TEE 机密计算 | 在受硬件隔离的 CPU/GPU TEE 中运行模型；企业验证远程证明后才释放短期解密密钥。 | 通常最接近原生云推理；证明和启动有固定开销，CPU-GPU 加密传输可能成为瓶颈。具体损失依赖硬件和数据搬运，不能用统一百分比表示。 | [AWS 远程证明](https://docs.aws.amazon.com/enclaves/latest/user/set-up-attestation.html)；[Azure 机密 GPU](https://learn.microsoft.com/en-us/azure/confidential-computing/gpu-options)；[NVIDIA H100](https://developer.nvidia.com/blog/?p=68661) |
| 同态加密 HE 或 FHE | 客户端加密输入，云端直接对密文执行计算，客户端解密结果；云端不持有明文密钥。 | 开销很高。矩阵运算密文膨胀，Softmax、GELU、LayerNorm 和采样通常需要近似、量化或混合协议，可能同时增加延迟并影响精度。 | [NIST FHE](https://csrc.nist.gov/Projects/pec/fhe)；[THE-X](https://aclanthology.org/2022.findings-acl.277.pdf) |
| MPC 与秘密共享 | 将输入拆成多个秘密份额，由不串谋的计算方联合执行推理；任一方单独看不到完整输入。 | 跨参与方通信轮次多，非线性算子和逐 token 解码尤其昂贵；通用在线 LLM 推理通常比明文推理慢多个数量级。 | [NIST MPC](https://csrc.nist.gov/Projects/pec/threshold)；[PUMA](https://arxiv.org/abs/2307.12533) |
| 基础云安全控制 | 使用 TLS、专线、VPC、IAM、客户管理密钥、零留存、禁用训练、数据地域和审计。它们保护链路、存储和生命周期，但普通云进程仍可看到运行时明文。 | TLS 和访问控制开销通常较低；专线和独占资源影响成本与部署弹性。不能替代 TEE、HE 或 MPC 的使用中数据保护。 | [NIST Zero Trust Architecture](https://www.nist.gov/publications/zero-trust-architecture) |

## 5 结论与建议

- 若要求严格 L0，采用本地部署。
- 若要求云管理员不可见且需要在线性能，优先采用 **GPU TEE + 远程证明 + 企业持钥和条件密钥释放**。
- 只有少量结构化敏感字段时，可使用本地 Tokenization，但它只是风险降低措施。
- 敏感度差异较大时，采用本地分级路由：本地推理、脱敏普通云、TEE 三条路径。
- 对云硬件也不信任且能接受高成本时，再考虑 HE/MPC。
- Split Inference 必须通过反演 PoC 量化，不能因为上传内容不可读就认定安全。
- TLS、VPC、IAM、零留存、禁用训练、地域和审计均应保留，但它们不能替代使用中数据保护。

推荐架构：

```text
本地分类与最小化
  → 敏感度路由
      ├─ 严格敏感：本地推理
      ├─ 可去标识：脱敏后普通云
      └─ 需要云能力：经远程证明的 GPU TEE
  → 企业持钥、最小日志、独立审计和持续攻击测试
```

准确的安全承诺应是：**未经批准的云软件、管理员和其他租户无法获得推理明文**，而不是含义不清的“数据绝不出企业”。
