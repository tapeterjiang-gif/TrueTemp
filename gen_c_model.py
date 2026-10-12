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
    args = [a for a in sys.argv[1:] if a != '--float']
    use_float = '--float' in sys.argv

    if len(args) >= 3:
        gen_c_header(args[0], args[1], args[2], use_float=use_float)
    else:
        default_generate(use_float=False)
        print()
        default_generate(use_float=True)
