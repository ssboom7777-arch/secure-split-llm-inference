# GitHub交付清单

## 已完成

- [x] Q1行业方案调研与性能影响矩阵；
- [x] Q1可运行Hidden State反演、量化还原率与缓解方法；
- [x] Q2一键启动的企业侧/云侧HTTP分割推理；
- [x] Q2进程内与HTTP端到端输出对齐；
- [x] Q2 MiniMind和Qwen3-1.7B GSM8K实验；
- [x] Q3优化原理、Timeline、500 km/10 Gbps before-after实测；
- [x] Q3云侧利用率、吞吐和相对成本结果及原始数据；
- [x] Q4可运行测算脚本；
- [x] Q4本地predict-vs-measure校准及目标模型/GPU外推；
- [x] 根README、各题README、结果数据与图表；
- [x] 模型权重、虚拟环境、日志和缓存已排除在Git提交之外；
- [x] Agent协作全程记录及可重复导出脚本；
- [x] 一页Agent协作复盘。

## 提交前需要人工完成

- [ ] 按`docs/demo-script.md`录制约1分钟Demo，放入`docs/demo/`或上传后在README中添加链接；
- [ ] 在干净目录克隆仓库，至少执行Q1、Q2 `verify-all.ps1`、Q3主实验和Q4估算冒烟测试；
- [ ] 检查Git提交中不存在`.safetensors`、`.gguf`、`.venv`、`wheels`和`runtime`；
- [ ] 推送GitHub并将仓库共享给用户`fxlin`。

## 建议的提交前检查

```powershell
git status --short
git ls-files | Select-String -Pattern '\.safetensors$|\.gguf$|\.venv|/wheels/|/runtime/'
git grep -n -i -E 'api[_-]?key|password|private.key|sk-[A-Za-z0-9]'
```
