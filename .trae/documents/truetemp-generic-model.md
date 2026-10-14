# TrueTemp：从"单台 DUT 单独建模"改为"同型号通用模型"

## Context

当前让 TrueTemp 的 LightGBM 校正模型**绑定单台 DUT 设备**：数据集只允许同一对 REF/DUT、
训练强制样本来自同一台 DUT、模型元数据保存单个 `device_id`、推理时强制输入设备与绑定设备一致。

用户确认 DUT 用于**生成同一型号（同系列所有单元）共用的通用校正模型**，模型不绑定单台设备 ID，
采集应能覆盖多台 DUT 单元。两个设计决策已确认：
- **不加型号标识字段**：模型适用性仅依赖已有的 DUT 协议版本 `protocol_version=3`，不新增 `dut_series` 之类的字段。
- **推理只做协议 v3 + 字段范围校验**：不再校验输入 `device_id` 是否匹配某个绑定设备；广播字节本身无法识别具体产品型号，因此不强加无法保证的身份校验。

改动核心：**移除所有"单台设备身份绑定/校验"逻辑，不新增任何标识字段**。

## 改动清单

### 1. `src/truetemp/dataset.py` — 数据集允许跨多台 DUT 单元
删除强制"同一对 REF/DUT"的约束。具体：删除
- 第 86 行 `devices = set()`
- 第 104 行 `devices.add(...)`
- 第 180–181 行 `if len(devices) != 1: raise ValueError('一个数据集必须对应同一对 REF / DUT')`

`manifest.json` 的 `sources` 已逐会话记录 `reference` / `dut` 设备 id，足以支撑通用模型溯源，无需额外改动。

### 2. `src/truetemp/features.py` — `validate_records` 去掉设备绑定
- 函数签名 `validate_records(frame, device=None)` → `validate_records(frame)`
- 删除设备身份相等校验（第 25–26 行）：
  `if device and not frame.device_id.str.casefold().eq(device.casefold()).all(): raise ValueError('输入 DUT 与模型绑定设备不同')`
- 保留：必需列校验、协议 v3 校验、字段范围校验（协议仍是通用模型适用性的唯一依据）。

### 3. `src/truetemp/calibration.py` — 推理不再按设备身份校验
第 16 行 `validate_records(records, self.metadata['device_id'])` → `validate_records(records)`。
其余预测逻辑不变（模型会继续从元数据读取并理解 `device_id` 列，只是不做相等匹配）。

### 4. `src/truetemp/training.py` — 训练允许来自多台 DUT 单元
- 删除第 58–61 行（`identifiers` 唯一性检查 + `device = str(...)`）。
- 第 62 行：`validate_records(train_data, device), validate_records(valid_data, device)` → `validate_records(train_data), validate_records(valid_data)`。
- 第 70–71 行元数据：删除 `device_id=device`；新增溯源字段 `dut_ids=sorted(set(train_data.device_id.str.casefold().unique()))`（只作来源记录，不做绑定/校验）。其余（`protocol_version=3`、`units`、`sources` 等）保持不变。

### 5. `tests/test_calibration.py` — 更新受影响断言
第 133–134 行原本要求"输入不同 `device_id` 时抛 `设备` 错误"，改为验证**通用模型接受不同设备的输入**，例如：
```python
bundle.predict(frame.assign(device_id='another-unit'))  # 不应抛错
```
相邻的"缺少列"校验（第 135–136 行）保留。

### 6. 文档同步更新
用 grep 找出的含"每台 DUT 单独建模 / device 绑定"表述的文件（`SYSTEM_DESIGN.md`、`CLI.md`、`REQUIREMENTS.md`、`DUT_PROTOCOL.md`、`README.md`、`GUIDE.md`）统一改写为：
- 模型面向**同一型号所有 DUT 单元**的通用校正，不受单台设备绑定。
- 训练可合并多台 DUT 单元的样本；元数据仅作溯源记录见过的设备 id。
- 推理仅校验 DUT 协议 v3 与字段范围，无法从广播字节识别具体型号，故不校验设备身份。

重点更新 `SYSTEM_DESIGN.md` 第 1、3、4.3、11、14 节中"每台 DUT 单独建模 / 单设备绑定"的表述，并相应说明推理处"DUT 身份校验"改为"协议 v3 + 范围校验"。

## 不改的部分
- `ble.py`、`export.py`、`protocol.py`、`physics.py`：与设备身份绑定无关，不动。
- `evaluation.py`：只用 `model_id` / `baseline` / `coverage` / `sources`，不依赖 `device_id`，不动。
- 不做 `--series` CLI、不新增元数据型号字段、不在本地校验输入设备的"未见设备"警告。

## 验证
1. `uv run pytest` — 全部通过，尤其确认更新后的 `test_calibration.py:end_to_end`（不同 `device_id` 输入不再报错，缺列仍报错）。
2. 用 `tests/test_calibration.py` 中构造的会话，改造成 **两个不同 DUT 单元** 的会话后再 `build_dataset`，确认不再触发"必须同一对 REF/DUT"报错，且能 `train`、`evaluate`、`predict` 全流程跑通。
3. `predict` 输入一个 `device_id` 与建模设备不同的 DUT CSV，确认正常输出校正结果（此前会报错）。