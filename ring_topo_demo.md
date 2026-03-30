# ring_topo_demo

> 示例 memnoc ring bus 拓扑

## 整体特性

1. 包含 4 条 ring bus。
2. `iniu` 为 master，`tniu` 为 slave，每个 OCM 地址空间为 4 MB。

## 具体连接细节

- `ring0`: `sp -> buf0~4 -> NPU (iniu) -> buf5~9 -> async1 -> buf10~14 -> ocm (tniu) -> buf15~19 -> async0 -> sp`
- `ring1`: `sp -> buf0~4 -> NPU (iniu) -> buf5~9 -> async1 -> buf10~14 -> ocm (tniu) -> buf15~19 -> async0 -> sp`
- `ring2`: `sp -> buf0~4 -> NPU (iniu) -> buf5~9 -> async1 -> buf10~14 -> ocm (tniu) -> buf15~19 -> async0 -> sp`
- `ring3`: `sp -> buf0~4 -> NPU (iniu) -> buf5~9 -> async1 -> buf10~14 -> ocm (tniu) -> buf15~19 -> async0 -> sp`

## 物理 harden 划分

1. ring bus top 部分分为上下两个 harden，以及一个包含上下 harden 的整体 top wrapper。
2. up harden 包含的组件：每条 ring 的 `async0`（mst side）→ `sp` → `buf0~4` → `NPU (iniu)` → `buf5~9` → `async1`（slv side）。
3. dn harden 包含的组件：每条 ring 的 `async1`（mst side）→ `buf10~14` → `ocm (tniu)` → `buf15~19` → `async0`（slv side）。