"""
从 model JSON 生成 C 语言头文件，树结构硬编码为 C 数组，零依赖，适合嵌入式部署

用法:
    python3 gen_c_model.py                           # 默认生成 double 版本
    python3 gen_c_model.py --float                    # 额外生成 float 版本 (NEON 优化)
    python3 gen_c_model.py <json_in> <h_out> <name>   # 单文件生成 (double)
    python3 gen_c_model.py --float <json_in> <h_out> <name>  # 单文件生成 (float)
"""

import json
import os
import sys


def gen_c_header(json_path, output_path, model_name, use_float=False):
    with open(json_path, 'r') as f:
        model = json.load(f)

    features = model['features']
    trees = model['trees']
    dtype = 'float' if use_float else 'double'
    suffix = 'f' if use_float else ''

    all_nodes = []
    tree_roots = []

    for tree in trees:
        offset = len(all_nodes)
        nodes = []

        def collect(node):
            idx = len(nodes)
            nodes.append(None)
            if 'v' in node:
                nodes[idx] = {
                    'is_leaf': 1, 'feature': 0, 'threshold': 0.0,
                    'left': 0, 'right': 0, 'value': node['v'],
                }
            else:
                left_idx = collect(node['l'])
                right_idx = collect(node['r'])
                nodes[idx] = {
                    'is_leaf': 0, 'feature': node['f'], 'threshold': node['t'],
                    'left': left_idx, 'right': right_idx, 'value': 0.0,
                }
            return idx

        root = collect(tree)
        for nd in nodes:
            if not nd['is_leaf']:
                nd['left'] += offset
                nd['right'] += offset
        tree_roots.append(offset + root)
        all_nodes.extend(nodes)

    lines = []
    lines.append(f'// Auto-generated from {os.path.basename(json_path)}')
    lines.append(f'// {len(trees)} trees, {len(all_nodes)} nodes, {len(features)} features')
    if use_float:
        lines.append('// Float version: optimized for Cortex-A7 NEON (4-way SIMD)')
    lines.append('')
    lines.append(f'#ifndef {model_name}_H')
    lines.append(f'#define {model_name}_H')
    lines.append('#include <stdint.h>')
    lines.append('')
    lines.append('typedef struct {')
    lines.append('    int32_t is_leaf;')
    lines.append('    int32_t feature;')
    lines.append(f'    {dtype} threshold;')
    lines.append('    int32_t left;')
    lines.append('    int32_t right;')
    lines.append(f'    {dtype} value;')
    lines.append(f'}} {model_name}_node_t;')
    lines.append('')
    lines.append(f'#define {model_name}_NUM_FEATURES {len(features)}')
    lines.append(f'#define {model_name}_NUM_TREES {len(trees)}')
    lines.append(f'#define {model_name}_NUM_NODES {len(all_nodes)}')
    lines.append('')

    lines.append(f'static const int {model_name}_tree_roots[{model_name}_NUM_TREES] = {{')
    lines.append('    ' + ', '.join(str(r) for r in tree_roots))
    lines.append('};')
    lines.append('')

    lines.append(f'static const {model_name}_node_t {model_name}_nodes[{model_name}_NUM_NODES] = {{')
    for nd in all_nodes:
        ts = nd['threshold']
        vs = nd['value']
        t_str = f'{ts}{suffix}' if ts != int(ts) else f'{float(ts)}{suffix}'
        v_str = f'{vs}{suffix}' if vs != int(vs) else f'{float(vs)}{suffix}'
        lines.append(
            f'    {{{nd["is_leaf"]}, {nd["feature"]}, {t_str}, '
            f'{nd["left"]}, {nd["right"]}, {v_str}}},'
        )
    lines.append('};')
    lines.append('')
    func_name = f'{model_name}_predict_one' if not use_float else f'{model_name}_predict_one_f'
    lines.append(f'static inline {dtype} {func_name}(const {dtype}* x) {{')
    lines.append(f'    {dtype} s = 0.0{suffix};')
    lines.append(f'    for (int t = 0; t < {model_name}_NUM_TREES; t++) {{')
    lines.append(f'        int node = {model_name}_tree_roots[t];')
    lines.append(f'        while (!{model_name}_nodes[node].is_leaf) {{')
    lines.append(f'            const {model_name}_node_t* n = &{model_name}_nodes[node];')
    lines.append(f'            node = (x[n->feature] <= n->threshold) ? n->left : n->right;')
    lines.append(f'        }}')
    lines.append(f'        s += {model_name}_nodes[node].value;')
    lines.append(f'    }}')
    lines.append('    return s;')
    lines.append('}')
    lines.append('')
    lines.append('#endif')

    with open(output_path, 'w') as f:
        f.write('\n'.join(lines))
    label = 'float' if use_float else 'double'
    print(f'Generated ({label}): {output_path} ({len(trees)} trees, {len(all_nodes)} nodes)')


def gen_esp32_data(temp_json_path, de_json_path, output_path, max_trees=200):
    """生成 ESP32 用的紧凑模型头文件（数据只读，放在 flash）

    与默认格式的区别:
      - 只保留前 max_trees 棵树，减少 flash 占用（默认 200，精度与 500 棵相当）
      - 阈值和叶子值量化为 int16，节点 8 字节
      - 利用 LightGBM 树的先序存储特性: 左子节点索引恒为 node+1，只存右子节点
      - 两个模型共用一套特征索引宏，防止重训练后特征顺序变化导致错位
    """
    models = []
    for json_path in (temp_json_path, de_json_path):
        with open(json_path, 'r') as f:
            model = json.load(f)
        model['trees'] = model['trees'][:max_trees]
        models.append(model)

    features = models[0]['features']
    if models[1]['features'] != features:
        raise SystemExit('两个模型的特征顺序不一致，无法生成 ESP32 头文件')

    def build_compact(trees):
        roots, nodes = [], []
        for tree in trees:
            def collect(node):
                idx = len(nodes)
                if 'v' in node:
                    nodes.append({'is_leaf': 1, 'feature': 0, 'threshold': 0.0,
                                  'right': 0, 'value': float(node['v'])})
                else:
                    nodes.append(None)
                    left_idx = collect(node['l'])
                    right_idx = collect(node['r'])
                    if left_idx != idx + 1:
                        raise SystemExit('树不是先序存储，无法使用 node+1 左子节点优化')
                    nodes[idx] = {'is_leaf': 0, 'feature': node['f'],
                                  'threshold': float(node['t']),
                                  'right': right_idx, 'value': 0.0}
                return idx

            # collect 返回的是全局节点索引，直接作为根
            roots.append(collect(tree))
        return roots, nodes

    temp_roots, temp_nodes = build_compact(models[0]['trees'])
    de_roots, de_nodes = build_compact(models[1]['trees'])

    # int16 量化: 阈值按特征各自缩放，叶子值按模型缩放
    max_thr = [0.0] * len(features)
    for nd in temp_nodes + de_nodes:
        if not nd['is_leaf']:
            f = nd['feature']
            max_thr[f] = max(max_thr[f], abs(nd['threshold']))
    feature_scale = [(m / 32000.0) if m > 0 else 1.0 for m in max_thr]

    def leaf_scale(nodes):
        m = max((abs(nd['value']) for nd in nodes if nd['is_leaf']), default=0.0)
        return (m / 32000.0) if m > 0 else 1.0

    temp_value_scale = leaf_scale(temp_nodes)
    de_value_scale = leaf_scale(de_nodes)

    for nodes, vscale in ((temp_nodes, temp_value_scale), (de_nodes, de_value_scale)):
        for nd in nodes:
            if nd['is_leaf']:
                nd['value_q'] = int(round(nd['value'] / vscale))
                nd['threshold_q'] = 0
            else:
                nd['value_q'] = 0
                nd['threshold_q'] = int(round(nd['threshold'] / feature_scale[nd['feature']]))

    def cf(v):
        s = f'{v:.9g}'
        if not any(c in s for c in '.eE'):
            s += '.0'
        return s + 'f'

    cond = 'TRUETEMP_MODEL_DATA_H'
    lines = [
        f'// Auto-generated by gen_c_model.py from {os.path.basename(temp_json_path)}',
        f'// and {os.path.basename(de_json_path)} — do not edit by hand.',
        f'// {len(temp_nodes)} + {len(de_nodes)} nodes, int16 quantized, ESP32 flash friendly.',
        '',
        f'#ifndef {cond}',
        f'#define {cond}',
        '#include <stdint.h>',
        '',
        'typedef struct {',
        '    int16_t threshold_q;  // threshold = threshold_q * feature_scale',
        '    int16_t value_q;      // leaf value = value_q * model value scale',
        '    uint16_t right;       // left child is always node + 1 (preorder layout)',
        '    uint8_t feature;',
        '    uint8_t is_leaf;',
        '} truetemp_node_t;',
        '',
        f'#define TRUETEMP_FEATURE_COUNT {len(features)}',
        f'#define TRUETEMP_TEMP_VALUE_SCALE {cf(temp_value_scale)}',
        f'#define TRUETEMP_DE_VALUE_SCALE {cf(de_value_scale)}',
        '',
        '// Feature order used by both models. Inputs must be arranged in this order.',
        'enum {',
    ]
    for i, name in enumerate(features):
        lines.append(f'    TRUETEMP_F_{name.upper()} = {i},')
    lines += [
        '};',
        '',
        '// Per-feature threshold dequantization scales',
        'static const float truetemp_feature_scale[TRUETEMP_FEATURE_COUNT] = {',
        '    ' + ', '.join(cf(s) for s in feature_scale) + ',',
        '};',
        '',
        f'#define TRUETEMP_TEMP_NUM_TREES {len(temp_roots)}',
        f'#define TRUETEMP_TEMP_NUM_NODES {len(temp_nodes)}',
        f'#define TRUETEMP_DE_NUM_TREES {len(de_roots)}',
        f'#define TRUETEMP_DE_NUM_NODES {len(de_nodes)}',
        '',
    ]

    def emit(name, roots, nodes):
        lines.append(f'static const uint16_t {name}_roots[{len(roots)}] = {{')
        for i in range(0, len(roots), 16):
            lines.append('    ' + ', '.join(str(r) for r in roots[i:i + 16]) + ',')
        lines.append('};')
        lines.append('')
        lines.append(f'static const truetemp_node_t {name}_nodes[{len(nodes)}] = {{')
        for nd in nodes:
            lines.append(f'    {{{nd["threshold_q"]}, {nd["value_q"]}, '
                         f'{nd["right"]}, {nd["feature"]}, {nd["is_leaf"]}}},')
        lines.append('};')
        lines.append('')

    emit('truetemp_temp', temp_roots, temp_nodes)
    emit('truetemp_de', de_roots, de_nodes)
    lines.append('#endif')

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w') as f:
        f.write('\n'.join(lines))

    total = len(temp_nodes) + len(de_nodes)
    print(f'Generated (esp32): {output_path} '
          f'({total} nodes, {total * 8 / 1024:.1f} KiB flash, {max_trees} trees/model)')


def default_generate(use_float):
    suffix = '_f' if use_float else ''
    name_suffix = '_F' if use_float else ''
    gen_c_header(
        'models/model_temperature.json',
        f'models/model_temperature{suffix}.h',
        f'MODEL_TEMP{name_suffix}',
        use_float=use_float,
    )
    gen_c_header(
        'models/model_vapor_pressure.json',
        f'models/model_vapor_pressure{suffix}.h',
        f'MODEL_DE{name_suffix}',
        use_float=use_float,
    )


if __name__ == '__main__':
    if '--esp32' in sys.argv:
        max_trees = 200
        if '--trees' in sys.argv:
            max_trees = int(sys.argv[sys.argv.index('--trees') + 1])
        gen_esp32_data(
            'models/model_temperature.json',
            'models/model_vapor_pressure.json',
            'esp32/truetemp_model_data.h',
            max_trees=max_trees,
        )
        raise SystemExit(0)

    args = [a for a in sys.argv[1:] if a != '--float']
    use_float = '--float' in sys.argv

    if len(args) >= 3:
        gen_c_header(args[0], args[1], args[2], use_float=use_float)
    else:
        default_generate(use_float=False)
        print()
        default_generate(use_float=True)
