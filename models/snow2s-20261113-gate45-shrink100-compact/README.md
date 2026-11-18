# Snow2S 温补候选版：紧凑 float 节点

本版对 `snow2s-20261113-gate45-shrink100` 进行无损存储压缩。
保留绝对温度、水汽压残差、九项温差、四项热状态四组模型，各 500 棵树，共 56,756 个节点。
模型 JSON、全部 float32 数值、树链接及训练配对数据与原版一致。

## 体积

| 项目 | 原格式 | 本版 |
|---|---:|---:|
| 每个节点 | 12 字节 | 8 字节 |
| 头文件文本 | 1,991,688 字节 | 1,254,127 字节 |
| 宿主编译后的模型只读数组，含树根索引 | 685,072 字节 | 458,048 字节 |

分裂节点只使用阈值，叶子节点只使用预测值，因此两者共用一个 float 字段；浮点精度保持不变。
模型数组仍为只读常量，直接供推理使用。

## 策略及接入

- 原始温度不高于 26℃：使用原绝对温度模型。
- 原始温度高于 26℃：使用九项输入预测温差。
- 四项热状态预测温差不高于 4.5℃ 时，以 100% 比例少扣，实际扣减 `min(预测温差, 2.6℃)`；
  高于 4.5℃ 时扣除完整的预测温差。

将本目录的 `truetemp.c`、`truetemp.h`、`truetemp_snow2s_model_data.h` 一起接入，
编译库和调用方均定义 `TRUETEMP_SNOW2S`，链接 `libm`。现有十项输入和函数签名保持兼容。
新模型头会拒绝缺少紧凑节点支持的旧推理代码；新推理代码同时兼容旧 12 字节节点格式。

从 TrueTemp 仓库根目录编译：

```bash
make -C linux snow2s SNOW2S_MODEL_DIR=../models/snow2s-20261113-gate45-shrink100-compact
./linux/truetemp_infer_snow2s 27 32.3 55.1 45.3 572 980 466 7.7 241 0
```

该示例输出仍为 `temperature=24.4000 humidity=34.4461`。

## 验证

13,503 组输入覆盖原配对数据、原始温度 27℃ 模拟、26℃ 分支边界、随机输入和非有限输入；
温度、湿度、露点、绝对湿度四项 C 输出与原版逐位一致。59 项测试、17 个子测试通过。
全部 56,756 个节点的有效 float32 位模式及树链接、2,000 个树根索引均核对一致。
详情见 `validation.json`，生成命令和代码校验值见 `model_2stage_metadata.json`。

26℃ 处仍直接切换，原历史工况模拟的最大跳变约 2.301℃。本版未进行 Snow2SApp 编译或实机验收。

## 复现生成

从 TrueTemp 仓库根目录执行，输出目录须不存在：

```bash
.venv/bin/python train_snow2s_hybrid.py \
  --base-dir models/snow2s-20261112 \
  --output-dir models/snow2s-gate45-shrink100-compact-new \
  --heat-gate-max 4.5 --shrink-fraction 1.0
```
