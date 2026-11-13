# Snow2S 实机对比候选版：4.5℃ / 100%

版本目录：`snow2s-20261113-gate45-shrink100`。
沿用 `snow2s-20261113-hybrid` 的模型树与 4,611 对训练记录，仅更改两个策略参数：

| 参数 | 本版值 |
|---|---:|
| 原始温度分支上限 | 26℃ |
| 四项预测温差的少扣阈值 | 4.5℃ |
| 少扣比例 | 100% |
| 训练温差中位数 | 2.6℃ |

原始温度不高于 26℃ 时使用原绝对温度模型。高于 26℃ 时，使用九项输入预测温差；
四项热状态模型预测不高于 4.5℃ 时，将扣减量限制为 `min(预测温差, 2.6)`。
四项预测高于 4.5℃ 时使用完整预测温差。湿度、露点和绝对湿度仍由水汽压及最终温度推导。

## 接入

将本目录中的以下三个文件一起接入 Snow2SApp：

- `truetemp.c`
- `truetemp.h`
- `truetemp_snow2s_model_data.h`

编译库和调用方均定义 `TRUETEMP_SNOW2S`，链接 `libm`，调用
`truetemp_compensate_snow2s(const truetemp_input_t *, truetemp_output_t *)`。
现有十项输入字段的顺序、单位和函数签名保持兼容。重新编译并链接应用后再部署，避免复用旧模型目标文件。

## 本机运行

从仓库根目录执行：

```bash
make -C linux snow2s SNOW2S_MODEL_DIR=../models/snow2s-20261113-gate45-shrink100
./linux/truetemp_infer_snow2s 27 32.3 55.1 45.3 572 980 466 7.7 241 0
```

参数依次为：原始温度、原始湿度、SoC 温度、CO₂ 模块温度、开机秒数、亮度、
亮屏秒数、CPU 负载、Wi-Fi 发送字节/秒、充电电流 mA。

此组输入的 C/Python 输出一致：

```text
temperature=24.4000 humidity=34.4461 dew_point=7.7286 absolute_humidity=7.6658
```

## 验证范围

本机 C 编译及 9,376 组 C/Python 输出比较通过，温度最大差约 0.000032℃。
77 条接近训练上限的历史工况模拟到原始 27℃ 时，54 条触发少扣并输出 24.4℃，
另外 23 条输出约 22.40～22.54℃。这些结果属于模拟，实机精度尚待对比。

26℃ 处采用直接切换：上述历史工况的边界跳变中位数约 2.127℃、最大约 2.301℃。
此前按文件留出评估的整体 MAE 为 0.223℃，77 对越界样本 MAE 为 1.955℃。
数据、命令、模型和代码校验值在 `model_2stage_metadata.json`，本机验证详情在 `validation.json`。

## 复现

从仓库根目录执行，使用一个尚不存在的输出目录：

```bash
.venv/bin/python train_snow2s_hybrid.py \
  --base-dir models/snow2s-20261112 \
  --output-dir models/snow2s-gate45-shrink100-new \
  --heat-gate-max 4.5 --shrink-fraction 1.0
```

训练仅重放基础模型保存的配对明细。原对比版本保留在 `models/snow2s-20261113-hybrid/`。
