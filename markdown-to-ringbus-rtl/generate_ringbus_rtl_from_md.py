#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


DEFAULT_TARGET_SPACE_MB = 4
DEFAULT_PARTITIONS = ["up", "dn"]

ROLE_ALIASES = {
    "iniu": "iniu",
    "initiator": "iniu",
    "master": "iniu",
    "mst": "iniu",
    "source": "iniu",
    "tniu": "tniu",
    "target": "tniu",
    "slave": "tniu",
    "slv": "tniu",
    "sink": "tniu",
}

DEFAULT_CFG_BY_FAMILY_ROLE = {
    ("npu", "iniu"): "npu_iniu_cfg",
    ("npu", "tniu"): "npu_tniu_cfg",
    ("ocm", "tniu"): "ocm_tniu_cfg",
    ("pcie", "iniu"): "pcie_iniu_cfg",
    ("pcie", "tniu"): "pcie_tniu_cfg",
    ("d2d", "iniu"): "d2d_iniu_cfg",
    ("d2d", "tniu"): "d2d_tniu_cfg",
    ("mmu", "iniu"): "mmu_iniu_cfg",
    ("mmu", "tniu"): "mmu_tniu_cfg",
}


def infer_default_role_for_family(family: str) -> Optional[str]:
    roles = sorted({role for cfg_family, role in DEFAULT_CFG_BY_FAMILY_ROLE if cfg_family == family})
    if len(roles) == 1:
        return roles[0]
    return None

RING_LINE_RE = re.compile(
    r"^\s*(?:[-*+]\s*)?(?:`)?ring\s*(?P<index>\d+)(?:`)?\s*[:：-]\s*(?:`)?(?P<body>.+?)(?:`)?\s*$",
    re.IGNORECASE,
)
TOPOLOGY_HINT_RE = re.compile(r"->")
BUFFER_RE = re.compile(r"^buf(?P<start>\d+)(?:(?:\s*[~\-]\s*|\.\.)(?P<end>\d+))?$", re.IGNORECASE)
ASYNC_RE = re.compile(r"^async(?P<index>\d+)$", re.IGNORECASE)
SP_RE = re.compile(r"^sp$", re.IGNORECASE)
PAREN_SUFFIX_RE = re.compile(r"^(?P<label>.+?)\s*\((?P<body>[^)]*)\)\s*$")
GLOBAL_SIZE_RE = re.compile(
    r"(?:每个|each)?\s*(?P<label>[A-Za-z0-9_]+)\s*(?:地址空间|address(?:\s+space)?)\s*(?:为|是|is)?\s*(?P<size>\d+)\s*(?P<unit>KB|MB|GB)",
    re.IGNORECASE,
)
FAMILY_ID_BASE_RE = re.compile(r"(?P<label>[A-Za-z0-9_]+)\s*从\s*(?P<base>\d+)\s*开始编号", re.IGNORECASE)
PARTITION_RE = re.compile(r"(?P<name>[A-Za-z0-9_]+)\s+harden.*[:：]\s*(?P<body>.+)", re.IGNORECASE)


class SpecError(ValueError):
    pass


@dataclass
class RawElement:
    kind: str
    text: str
    buffer_index: Optional[int] = None
    async_index: Optional[int] = None
    label: Optional[str] = None
    role: Optional[str] = None
    family: Optional[str] = None
    cfg_symbol: Optional[str] = None
    size_mb: Optional[int] = None


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower()).strip("_")
    return slug or "node"


def strip_code_quotes(text: str) -> str:
    value = text.strip()
    if value.startswith("`") and value.endswith("`") and len(value) >= 2:
        return value[1:-1].strip()
    return value


def has_explicit_no_physical_harden(markdown_text: str) -> bool:
    lines = markdown_text.splitlines()
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if "harden" not in line.lower() or "划分" not in line:
            continue
        normalized_inline = re.sub(r"[`>:#*\-\s]+", "", line).lower()
        if normalized_inline.endswith("无") or normalized_inline.endswith("none") or normalized_inline.endswith("no"):
            return True
        for candidate in lines[index + 1 :]:
            candidate_text = candidate.strip()
            if not candidate_text:
                continue
            candidate_normalized = re.sub(r"^[>*\-+\s`]+", "", candidate_text).strip().strip("。.;；:")
            lowered = candidate_normalized.lower()
            return candidate_normalized in {"无", "无harden", "无 harden"} or lowered in {"none", "no", "n/a", "na"}
    return False


def normalize_role(raw_role: str) -> str:
    key = raw_role.strip().lower().replace("-", " ")
    key = key.replace("side", "").strip()
    if key in ROLE_ALIASES:
        return ROLE_ALIASES[key]
    raise SpecError("Unsupported endpoint role: %s" % raw_role)


def infer_family(label: str) -> str:
    lowered = slugify(label)
    prefix = re.match(r"[a-z_]+", lowered)
    if not prefix:
        raise SpecError("Cannot infer endpoint family from label: %s" % label)
    return prefix.group(0).rstrip("_")


def parse_size_mb(raw_value: str) -> int:
    match = re.search(r"(\d+)\s*(KB|MB|GB)", raw_value, re.IGNORECASE)
    if not match:
        if raw_value.strip().isdigit():
            return int(raw_value.strip())
        raise SpecError("Unable to parse size in MB from: %s" % raw_value)
    value = int(match.group(1))
    unit = match.group(2).upper()
    if unit == "KB":
        if value % 1024 != 0:
            raise SpecError("Size must be a multiple of 1024KB to convert into MB: %s" % raw_value)
        return value // 1024
    if unit == "GB":
        return value * 1024
    return value


def extract_family_sizes(markdown_text: str) -> Dict[str, int]:
    sizes: Dict[str, int] = {}
    for match in GLOBAL_SIZE_RE.finditer(markdown_text):
        family = infer_family(match.group("label"))
        sizes[family] = parse_size_mb("%s%s" % (match.group("size"), match.group("unit")))
    return sizes


def extract_family_id_bases(markdown_text: str) -> Dict[str, int]:
    bases: Dict[str, int] = {}
    for match in FAMILY_ID_BASE_RE.finditer(markdown_text):
        family = infer_family(match.group("label"))
        base = int(match.group("base"))
        existing = bases.get(family)
        if existing is not None and existing != base:
            raise SpecError("Conflicting data_topo_id base rules for family '%s': %d vs %d" % (family, existing, base))
        bases[family] = base
    return bases


def assign_data_topo_ids(endpoints: List[Dict[str, Any]], explicit_family_bases: Dict[str, int]) -> Tuple[int, Dict[str, int]]:
    if explicit_family_bases:
        endpoint_families = sorted({str(endpoint["family"]) for endpoint in endpoints})
        missing = [family for family in endpoint_families if family not in explicit_family_bases]
        if missing:
            raise SpecError("Missing data_topo_id base rule for families: %s" % ", ".join(missing))

        family_offsets = {family: 0 for family in endpoint_families}
        used_ids: Dict[int, str] = {}
        for endpoint in endpoints:
            family = str(endpoint["family"])
            data_topo_id = explicit_family_bases[family] + family_offsets[family]
            if data_topo_id in used_ids:
                raise SpecError(
                    "data_topo_id collision at %d between %s and %s; adjust markdown numbering rules"
                    % (data_topo_id, used_ids[data_topo_id], endpoint["attr_name"])
                )
            endpoint["data_topo_id"] = data_topo_id
            family_offsets[family] += 1
            used_ids[data_topo_id] = str(endpoint["attr_name"])
        return max(used_ids) + 1, explicit_family_bases

    initiators = [endpoint for endpoint in endpoints if endpoint["role"] == "iniu"]
    targets = [endpoint for endpoint in endpoints if endpoint["role"] == "tniu"]
    target_id_base = max(10, len(initiators))
    for index, endpoint in enumerate(initiators):
        endpoint["data_topo_id"] = index
    for index, endpoint in enumerate(targets):
        endpoint["data_topo_id"] = target_id_base + index
    return target_id_base + len(targets), {"iniu": 0, "tniu": target_id_base}


def extract_partition_names(markdown_text: str) -> List[str]:
    names: List[str] = []
    for line in markdown_text.splitlines():
        match = PARTITION_RE.search(line)
        if not match:
            continue
        name = slugify(match.group("name"))
        if name not in names:
            names.append(name)
    return names[:2]


def normalize_async_side(raw_side: str) -> str:
    normalized = raw_side.strip().lower().replace("-", " ")
    normalized = normalized.replace("side", "").strip()
    if normalized in {"mst", "master", "src", "source"}:
        return "mst"
    if normalized in {"slv", "slave", "dst", "dest", "destination", "sink"}:
        return "slv"
    raise SpecError("Unsupported async side: %s" % raw_side)


def normalize_partition_token(token: str) -> str:
    normalized = strip_code_quotes(token.strip()).replace("（", "(").replace("）", ")")
    normalized = normalized.replace("`", "").rstrip("。.;；,，")
    compact = normalized.replace(" ", "")
    async_match = re.match(r"^async(?P<index>\d+)\((?P<side>[^)]*)\)$", compact, re.IGNORECASE)
    if async_match:
        return "async%s(%s)" % (async_match.group("index"), normalize_async_side(async_match.group("side")))
    if compact.lower().startswith("buf"):
        return "buf"
    if compact.lower() == "sp":
        return "sp"
    endpoint = parse_endpoint_token(normalized)
    if endpoint.label is None or endpoint.role is None:
        raise SpecError("Unable to normalize partition token: %s" % token)
    return "%s(%s)" % (slugify(endpoint.label), endpoint.role)


def extract_partition_specs(markdown_text: str) -> List[Dict[str, Any]]:
    specs: List[Dict[str, Any]] = []
    for line in markdown_text.splitlines():
        match = PARTITION_RE.search(line)
        if not match:
            continue
        name = slugify(match.group("name"))
        body = match.group("body").replace("→", "->")
        body = re.sub(r"^(?:每条|每个|每一种)?\s*ring\s*的\s*", "", body, flags=re.IGNORECASE)
        body = re.sub(r"^ring\s*[\d\s,/_-]+\s*的\s*", "", body, flags=re.IGNORECASE)
        tokens = [normalize_partition_token(token) for token in split_sequence(body)]
        specs.append({"name": name, "tokens": tokens})
    return specs[:2]


def normalize_element_token(element: Dict[str, Any]) -> str:
    if element["kind"] == "sp":
        return "sp"
    if element["kind"] == "buf":
        return "buf"
    if element["kind"] != "endpoint":
        raise SpecError("Unsupported partition element kind in normalization: %s" % element["kind"])
    return "%s(%s)" % (slugify(element["endpoint_label"]), element["role"])


def build_partition_candidate_tokens(
    elements: List[Dict[str, Any]],
    start_async_pos: int,
    end_async_pos: int,
) -> List[str]:
    start_async = elements[start_async_pos].get("async_index")
    end_async = elements[end_async_pos].get("async_index")
    if start_async is None or end_async is None:
        raise SpecError("Partition candidate boundaries must be async nodes")
    tokens = ["async%d(mst)" % start_async]
    index = (start_async_pos + 1) % len(elements)
    while index != end_async_pos:
        tokens.append(normalize_element_token(elements[index]))
        index = (index + 1) % len(elements)
    tokens.append("async%d(slv)" % end_async)
    return tokens


def apply_partition_assignment(
    elements: List[Dict[str, Any]],
    partition_name: str,
    start_async_pos: int,
    end_async_pos: int,
) -> None:
    index = (start_async_pos + 1) % len(elements)
    while index != end_async_pos:
        elements[index]["partition"] = partition_name
        index = (index + 1) % len(elements)


def collect_partition_tokens_by_name(elements: List[Dict[str, Any]], partition_names: Sequence[str]) -> Dict[str, List[str]]:
    async_positions = [index for index, element in enumerate(elements) if element["kind"] == "async"]
    if len(async_positions) != 2:
        return {}
    tokens_by_name: Dict[str, List[str]] = {}
    for start_async_pos in async_positions:
        end_async_pos = next(pos for pos in async_positions if pos != start_async_pos)
        next_partition = elements[(start_async_pos + 1) % len(elements)].get("partition")
        if next_partition in partition_names:
            tokens_by_name[str(next_partition)] = build_partition_candidate_tokens(elements, start_async_pos, end_async_pos)
    return tokens_by_name


def extract_ring_descriptions(markdown_text: str) -> List[Tuple[int, str]]:
    rings: List[Tuple[int, str]] = []
    fallback_candidates: List[str] = []
    for line in markdown_text.splitlines():
        stripped = strip_code_quotes(line.strip())
        if not stripped:
            continue
        match = RING_LINE_RE.match(stripped)
        if match:
            rings.append((int(match.group("index")), match.group("body").strip()))
            continue
        if stripped.startswith("#"):
            continue
        if stripped.startswith(">"):
            stripped = stripped[1:].strip()
        if stripped.startswith(("- ", "* ", "+ ")):
            stripped = stripped[2:].strip()
        if TOPOLOGY_HINT_RE.search(stripped) and (
            "buf" in stripped.lower() or "async" in stripped.lower() or "sp" in stripped.lower()
        ):
            fallback_candidates.append(stripped)
    if rings:
        return sorted(rings, key=lambda item: item[0])
    if not fallback_candidates:
        raise SpecError("No ring topology description found in markdown input")
    return list(enumerate(fallback_candidates))


def split_sequence(sequence_text: str) -> List[str]:
    parts = [strip_code_quotes(part.strip()) for part in sequence_text.split("->")]
    return [part for part in parts if part]


def parse_endpoint_token(token: str) -> RawElement:
    working = token.strip()
    metadata: Dict[str, str] = {}
    role: Optional[str] = None
    match = PAREN_SUFFIX_RE.match(working)
    if match:
        working = match.group("label").strip()
        body_parts = [part.strip() for part in match.group("body").split(",") if part.strip()]
        for index, item in enumerate(body_parts):
            if "=" in item:
                key, value = item.split("=", 1)
                metadata[key.strip().lower()] = value.strip()
            elif index == 0:
                role = normalize_role(item)
            else:
                metadata[item.strip().lower()] = "true"
    lowered = working.lower()
    if role is None:
        for alias, normalized in ROLE_ALIASES.items():
            if lowered.endswith("_" + alias) or lowered.endswith("-" + alias):
                role = normalized
                working = working[: -len(alias) - 1].strip("_-")
                lowered = working.lower()
                break
    family = infer_family(working)
    if role is None:
        role = infer_default_role_for_family(family)
    if role is None:
        raise SpecError("Endpoint token must declare a role: %s" % token)
    cfg_symbol = metadata.get("cfg")
    size_mb = parse_size_mb(metadata["size"]) if "size" in metadata else None
    return RawElement(
        kind="endpoint",
        text=token,
        label=working,
        role=role,
        family=family,
        cfg_symbol=cfg_symbol,
        size_mb=size_mb,
    )


def parse_sequence(sequence_text: str) -> List[RawElement]:
    parsed: List[RawElement] = []
    next_buffer_index = 0
    for token in split_sequence(sequence_text):
        normalized = token.replace("（", "(").replace("）", ")")
        compact = normalized.replace(" ", "")
        if compact.lower() == "buf":
            parsed.append(RawElement(kind="buf", text="buf%d" % next_buffer_index, buffer_index=next_buffer_index))
            next_buffer_index += 1
            continue
        buffer_match = BUFFER_RE.match(compact)
        if buffer_match:
            start = int(buffer_match.group("start"))
            end = int(buffer_match.group("end") or start)
            if end < start:
                raise SpecError("Buffer range is reversed: %s" % token)
            for buffer_index in range(start, end + 1):
                parsed.append(RawElement(kind="buf", text="buf%d" % buffer_index, buffer_index=buffer_index))
            next_buffer_index = max(next_buffer_index, end + 1)
            continue
        async_match = ASYNC_RE.match(compact)
        if async_match:
            parsed.append(
                RawElement(kind="async", text="async%s" % async_match.group("index"), async_index=int(async_match.group("index")))
            )
            continue
        if SP_RE.match(normalized.strip()):
            parsed.append(RawElement(kind="sp", text="sp"))
            continue
        parsed.append(parse_endpoint_token(normalized))
    if len(parsed) < 3:
        raise SpecError("Ring sequence is too short: %s" % sequence_text)
    if parsed[0].kind == parsed[-1].kind == "sp":
        parsed.pop()
    return parsed


def determine_partition_map(
    elements: List[Dict[str, Any]],
    partition_names: Sequence[str],
    partition_specs: Optional[Sequence[Dict[str, Any]]] = None,
) -> Tuple[List[str], List[Dict[str, str]]]:
    if not elements:
        raise SpecError("Cannot partition an empty ring")
    async_positions = [index for index, element in enumerate(elements) if element["kind"] == "async"]
    if not async_positions:
        for element in elements:
            if element["kind"] != "async":
                element["partition"] = partition_names[0]
        return [partition_names[0]], []
    if len(async_positions) != 2:
        raise SpecError("Auto partitioning currently requires exactly 0 or 2 async bridges per ring")
    if partition_specs:
        candidate_specs = [
            {
                "start_async_pos": async_positions[0],
                "end_async_pos": async_positions[1],
                "tokens": build_partition_candidate_tokens(elements, async_positions[0], async_positions[1]),
            },
            {
                "start_async_pos": async_positions[1],
                "end_async_pos": async_positions[0],
                "tokens": build_partition_candidate_tokens(elements, async_positions[1], async_positions[0]),
            },
        ]
        unmatched_candidates = candidate_specs.copy()
        ordered_names: List[str] = []
        for partition_spec in partition_specs:
            matched_candidate = None
            for candidate in unmatched_candidates:
                if candidate["tokens"] == partition_spec["tokens"]:
                    matched_candidate = candidate
                    break
            if matched_candidate is None:
                available = [candidate["tokens"] for candidate in candidate_specs]
                raise SpecError(
                    "Partition description for '%s' does not match any ring segment. expected one of %s, got %s"
                    % (partition_spec["name"], available, partition_spec["tokens"])
                )
            apply_partition_assignment(
                elements,
                str(partition_spec["name"]),
                int(matched_candidate["start_async_pos"]),
                int(matched_candidate["end_async_pos"]),
            )
            unmatched_candidates.remove(matched_candidate)
            ordered_names.append(str(partition_spec["name"]))
        partition_names = ordered_names
    else:
        initiator_positions = [
            index
            for index, element in enumerate(elements)
            if element["kind"] == "endpoint" and element["role"] == "iniu"
        ]
        if not initiator_positions:
            raise SpecError("At least one initiator endpoint is required to derive ring partitions")
        anchor = initiator_positions[0]
        async_before = async_positions[-1]
        async_after = async_positions[0]
        for position in async_positions:
            if position < anchor:
                async_before = position
            if position > anchor:
                async_after = position
                break
        up_partition, dn_partition = partition_names[0], partition_names[1]
        index = (async_before + 1) % len(elements)
        while index != async_after:
            elements[index]["partition"] = up_partition
            index = (index + 1) % len(elements)
        index = (async_after + 1) % len(elements)
        while index != async_before:
            elements[index]["partition"] = dn_partition
            index = (index + 1) % len(elements)
    async_specs: List[Dict[str, str]] = []
    for async_index in async_positions:
        prev_element = elements[(async_index - 1) % len(elements)]
        next_element = elements[(async_index + 1) % len(elements)]
        prev_partition = prev_element.get("partition")
        next_partition = next_element.get("partition")
        if not prev_partition or not next_partition:
            raise SpecError("Unable to derive async bridge partitions around %s" % elements[async_index]["attr_name"])
        elements[async_index]["src_partition"] = prev_partition
        elements[async_index]["dst_partition"] = next_partition
        async_specs.append(
            {
                "attr_name": elements[async_index]["attr_name"],
                "src_partition": prev_partition,
                "dst_partition": next_partition,
            }
        )
    return list(dict.fromkeys(str(name) for name in partition_names)), async_specs


def validate_spec_against_markdown(spec: Dict[str, Any], markdown_text: str) -> None:
    partition_specs = extract_partition_specs(markdown_text)
    if not partition_specs:
        return
    expected = {str(partition_spec["name"]): partition_spec["tokens"] for partition_spec in partition_specs}
    actual: Dict[str, List[str]] = {}
    for ring in spec["rings"]:
        actual.update(collect_partition_tokens_by_name(ring["elements"], spec["partitions"]))
    missing = [name for name in expected if name not in actual]
    if missing:
        raise SpecError("Missing partition validation data for: %s" % ", ".join(missing))
    mismatches = [
        "%s expected %s got %s" % (name, expected[name], actual[name])
        for name in expected
        if expected[name] != actual[name]
    ]
    if mismatches:
        raise SpecError("Partition validation failed: %s" % "; ".join(mismatches))


def build_spec(markdown_text: str, top_id: str) -> Dict[str, Any]:
    family_sizes = extract_family_sizes(markdown_text)
    explicit_family_id_bases = extract_family_id_bases(markdown_text)
    partition_specs = extract_partition_specs(markdown_text)
    has_physical_harden = not has_explicit_no_physical_harden(markdown_text)
    partition_names = [str(partition_spec["name"]) for partition_spec in partition_specs] or extract_partition_names(markdown_text) or DEFAULT_PARTITIONS
    if len(partition_names) == 1:
        partition_names = [partition_names[0], DEFAULT_PARTITIONS[1]]

    ring_descriptions = extract_ring_descriptions(markdown_text)
    endpoint_counter: Dict[Tuple[str, str], int] = {}
    endpoints: List[Dict[str, Any]] = []
    endpoints_by_id: Dict[str, Dict[str, Any]] = {}
    rings: List[Dict[str, Any]] = []

    for ring_index, sequence_text in ring_descriptions:
        raw_elements = parse_sequence(sequence_text)
        sp_count = sum(1 for element in raw_elements if element.kind == "sp")
        if sp_count != 1:
            raise SpecError("Each ring must contain exactly one sp token; ring%d has %d" % (ring_index, sp_count))
        ring_elements: List[Dict[str, Any]] = []
        buffers: List[int] = []
        async_indices: List[int] = []
        for raw_element in raw_elements:
            if raw_element.kind == "sp":
                ring_elements.append(
                    {
                        "kind": "sp",
                        "attr_name": "sp_node%d" % ring_index,
                        "text": raw_element.text,
                    }
                )
                continue
            if raw_element.kind == "buf":
                if raw_element.buffer_index is None:
                    raise SpecError("Buffer token is missing its index: %s" % raw_element.text)
                buffer_index = raw_element.buffer_index
                buffers.append(buffer_index)
                ring_elements.append(
                    {
                        "kind": "buf",
                        "attr_name": "ring%d_buff_node_%d" % (ring_index, buffer_index),
                        "buffer_index": buffer_index,
                        "text": raw_element.text,
                    }
                )
                continue
            if raw_element.kind == "async":
                if raw_element.async_index is None:
                    raise SpecError("Async token is missing its index: %s" % raw_element.text)
                async_index = raw_element.async_index
                async_indices.append(async_index)
                ring_elements.append(
                    {
                        "kind": "async",
                        "attr_name": "ring%d_async_node%d" % (ring_index, async_index),
                        "async_index": async_index,
                        "text": raw_element.text,
                    }
                )
                continue
            family = str(raw_element.family)
            role = str(raw_element.role)
            counter_key = (family, role)
            instance_index = endpoint_counter.get(counter_key, 0)
            endpoint_counter[counter_key] = instance_index + 1
            node_name = "%s_%s%d" % (family, role, instance_index)
            attr_name = "%s_node" % node_name
            cfg_symbol = raw_element.cfg_symbol or DEFAULT_CFG_BY_FAMILY_ROLE.get((family, role))
            if cfg_symbol is None:
                raise SpecError(
                    "No cfg symbol found for endpoint family '%s' with role '%s'; use an inline cfg override"
                    % (family, role)
                )
            endpoint_spec = {
                "attr_name": attr_name,
                "node_name": node_name,
                "label": raw_element.label,
                "family": family,
                "role": role,
                "cfg_symbol": cfg_symbol,
                "ring_index": ring_index,
                "target_space_mb": raw_element.size_mb or family_sizes.get(family, DEFAULT_TARGET_SPACE_MB),
            }
            endpoints.append(endpoint_spec)
            endpoints_by_id[attr_name] = endpoint_spec
            ring_elements.append(
                {
                    "kind": "endpoint",
                    "attr_name": attr_name,
                    "endpoint_label": raw_element.label,
                    "family": family,
                    "role": role,
                    "text": raw_element.text,
                }
            )
        ring_partitions, async_partition_specs = determine_partition_map(ring_elements, partition_names, partition_specs=partition_specs or None)
        rings.append(
            {
                "ring_index": ring_index,
                "elements": ring_elements,
                "sp_attr_name": "sp_node%d" % ring_index,
                "buffer_indices": sorted(buffers),
                "async_indices": sorted(async_indices),
                "partitions": ring_partitions,
                "async_partition_specs": async_partition_specs,
            }
        )

    initiators = [endpoint for endpoint in endpoints if endpoint["role"] == "iniu"]
    targets = [endpoint for endpoint in endpoints if endpoint["role"] == "tniu"]
    if not initiators:
        raise SpecError("At least one initiator endpoint is required")
    if not targets:
        raise SpecError("At least one target endpoint is required")

    required_node_num, family_id_bases = assign_data_topo_ids(endpoints, explicit_family_id_bases)

    partition_set: List[str] = []
    for ring in rings:
        for name in ring["partitions"]:
            if name not in partition_set:
                partition_set.append(name)

    for ring in rings:
        for element in ring["elements"]:
            if element["kind"] != "endpoint":
                continue
            endpoint = endpoints_by_id[element["attr_name"]]
            endpoint["top_partition"] = element.get("partition", partition_set[0])

    system_families: List[str] = []
    for endpoint in endpoints:
        if endpoint["family"] not in system_families:
            system_families.append(endpoint["family"])

    memory_views: List[Dict[str, Any]] = []
    initiator_families: List[str] = []
    for endpoint in initiators:
        if endpoint["family"] not in initiator_families:
            initiator_families.append(endpoint["family"])
    for family in initiator_families:
        view_name = "%s_memory_view" % family
        family_initiators = [endpoint["attr_name"] for endpoint in initiators if endpoint["family"] == family]
        memory_views.append(
            {
                "attr_name": view_name,
                "display_name": view_name,
                "initiators": family_initiators,
                "targets": [endpoint["attr_name"] for endpoint in targets],
            }
        )

    partition_attachments: Dict[str, List[Dict[str, str]]] = {name: [] for name in partition_set}
    for ring in rings:
        for element in ring["elements"]:
            kind = element["kind"]
            if kind == "async":
                partition_attachments[element["src_partition"]].append(
                    {
                        "kind": "async_side",
                        "source_attr": element["attr_name"],
                        "wrapper_attr": "%s_slv_side" % element["attr_name"],
                        "side": "slv",
                    }
                )
                partition_attachments[element["dst_partition"]].append(
                    {
                        "kind": "async_side",
                        "source_attr": element["attr_name"],
                        "wrapper_attr": "%s_mst_side" % element["attr_name"],
                        "side": "mst",
                    }
                )
                continue
            partition_name = element.get("partition", partition_set[0])
            if kind in {"sp", "buf"}:
                partition_attachments[partition_name].append(
                    {
                        "kind": "node",
                        "source_attr": element["attr_name"],
                        "wrapper_attr": element["attr_name"],
                    }
                )
                continue
            endpoint = endpoints_by_id[element["attr_name"]]
            partition_attachments[partition_name].append(
                {
                    "kind": "endpoint_top_wrap",
                    "source_attr": element["attr_name"],
                    "wrapper_attr": "%s_top_wrap" % endpoint["node_name"],
                    "role": endpoint["role"],
                }
            )

    return {
        "top_id": top_id,
        "rings": rings,
        "endpoints": endpoints,
        "targets": [endpoint["attr_name"] for endpoint in targets],
        "initiators": [endpoint["attr_name"] for endpoint in initiators],
        "system_families": system_families,
        "partitions": partition_set,
        "has_physical_harden": has_physical_harden,
        "memory_views": memory_views,
        "partition_attachments": partition_attachments,
        "required_node_num": required_node_num,
        "family_id_bases": family_id_bases,
    }


def _contiguous_ranges(indices: Sequence[int]) -> List[Tuple[int, int]]:
    if not indices:
        return []
    ordered = sorted(dict.fromkeys(indices))
    ranges: List[Tuple[int, int]] = []
    start = ordered[0]
    end = ordered[0]
    for value in ordered[1:]:
        if value == end + 1:
            end = value
            continue
        ranges.append((start, end))
        start = value
        end = value
    ranges.append((start, end))
    return ranges


def _group_endpoints(endpoints: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: List[Dict[str, Any]] = []
    group_by_key: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for endpoint in endpoints:
        key = (endpoint["family"], endpoint["role"], endpoint["cfg_symbol"])
        group = group_by_key.get(key)
        if group is None:
            group = {
                "family": endpoint["family"],
                "role": endpoint["role"],
                "cfg_symbol": endpoint["cfg_symbol"],
                "items": [],
            }
            group_by_key[key] = group
            groups.append(group)
        group["items"].append(endpoint)
    return groups


def _ring_endpoint_groups(spec: Dict[str, Any]) -> Dict[int, List[Dict[str, Any]]]:
    grouped: Dict[int, List[Dict[str, Any]]] = {}
    for ring in spec["rings"]:
        grouped[ring["ring_index"]] = []
    endpoint_by_attr = {endpoint["attr_name"]: endpoint for endpoint in spec["endpoints"]}
    for ring in spec["rings"]:
        endpoint_items: List[Dict[str, Any]] = []
        for element in ring["elements"]:
            if element["kind"] != "endpoint":
                continue
            endpoint_items.append(endpoint_by_attr[element["attr_name"]])
        grouped[ring["ring_index"]] = endpoint_items
    return grouped


def _uniform_ring_buffer_ranges(spec: Dict[str, Any]) -> Optional[Dict[str, List[Tuple[int, int]]]]:
    reference: Optional[Dict[str, List[Tuple[int, int]]]] = None
    for ring in spec["rings"]:
        partition_indices: Dict[str, List[int]] = {partition: [] for partition in spec["partitions"]}
        for element in ring["elements"]:
            if element["kind"] != "buf":
                continue
            partition_indices[element["partition"]].append(element["buffer_index"])
        current = {partition: _contiguous_ranges(indices) for partition, indices in partition_indices.items() if indices}
        if reference is None:
            reference = current
            continue
        if current != reference:
            return None
    return reference or {}


def _uniform_ring_async_partitions(spec: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    reference: Optional[List[Dict[str, Any]]] = None
    for ring in spec["rings"]:
        current: List[Dict[str, Any]] = []
        for element in ring["elements"]:
            if element["kind"] != "async":
                continue
            current.append(
                {
                    "async_index": element["async_index"],
                    "src_partition": element["src_partition"],
                    "dst_partition": element["dst_partition"],
                }
            )
        current = sorted(current, key=lambda item: item["async_index"])
        if reference is None:
            reference = current
            continue
        if current != reference:
            return None
    return reference or []


def _memory_base_expr(base_mb: int) -> str:
    if base_mb == 0:
        return "0*KB"
    return "%d*MB" % base_mb


def _append_line(lines: List[str], text: str = "", indent: int = 0) -> None:
    if text:
        lines.append("    " * indent + text)
    else:
        lines.append("")


def _top_level_package(module_name: str) -> str:
    return module_name.split(".", 1)[0]


def _clk_ring_signal(partition: str, has_physical_harden: bool) -> str:
    return "clk_ring_%s" % partition if has_physical_harden else "clk_ring"


def _rst_ring_signal(partition: str, has_physical_harden: bool) -> str:
    return "rst_ring_%s" % partition if has_physical_harden else "rst_ring"


def render_memtopo(spec: Dict[str, Any], class_name: str, template_module: str, node_module: str, project_root: Path) -> str:
    endpoint_groups = _group_endpoints(spec["endpoints"])
    ring_count = len(spec["rings"])
    ring_indices = [ring["ring_index"] for ring in spec["rings"]]
    endpoint_by_ring = _ring_endpoint_groups(spec)
    buffer_ranges = _uniform_ring_buffer_ranges(spec)
    async_specs = _uniform_ring_async_partitions(spec)
    endpoint_pattern_safe = ring_count > 0
    if endpoint_pattern_safe:
        for group in endpoint_groups:
            if len(group["items"]) != ring_count:
                endpoint_pattern_safe = False
                break
    if endpoint_pattern_safe:
        for ring in spec["rings"]:
            seen_endpoint_kinds: set[Tuple[str, str]] = set()
            for endpoint in endpoint_by_ring[ring["ring_index"]]:
                key = (endpoint["family"], endpoint["role"])
                if key in seen_endpoint_kinds:
                    endpoint_pattern_safe = False
                    break
                seen_endpoint_kinds.add(key)
            if not endpoint_pattern_safe:
                break
    uniform_ring_sequence = True
    if spec["rings"]:
        reference_sequence: List[Tuple[Any, ...]] = []
        for element in spec["rings"][0]["elements"]:
            if element["kind"] == "buf":
                reference_sequence.append((element["kind"], element["buffer_index"]))
            elif element["kind"] == "async":
                reference_sequence.append((element["kind"], element["async_index"]))
            elif element["kind"] == "endpoint":
                reference_sequence.append((element["kind"], element["family"], element["role"]))
            else:
                reference_sequence.append((element["kind"],))
        for ring in spec["rings"][1:]:
            current_sequence: List[Tuple[Any, ...]] = []
            for element in ring["elements"]:
                if element["kind"] == "buf":
                    current_sequence.append((element["kind"], element["buffer_index"]))
                elif element["kind"] == "async":
                    current_sequence.append((element["kind"], element["async_index"]))
                elif element["kind"] == "endpoint":
                    current_sequence.append((element["kind"], element["family"], element["role"]))
                else:
                    current_sequence.append((element["kind"],))
            if current_sequence != reference_sequence:
                uniform_ring_sequence = False
                break

    has_physical_harden = spec.get("has_physical_harden", True)
    top_clock_domains = list(spec["partitions"]) if has_physical_harden else ["ring"]
    shared_top_clock_domain = top_clock_domains[0]
    shared_clk_ring = _clk_ring_signal(shared_top_clock_domain, has_physical_harden)
    shared_rst_ring = _rst_ring_signal(shared_top_clock_domain, has_physical_harden)
    node_package = _top_level_package(node_module)
    template_package = _top_level_package(template_module)

    lines: List[str] = []
    _append_line(lines, "import sys")
    _append_line(lines, "from pathlib import Path")
    _append_line(lines, "from importlib import import_module")
    _append_line(lines)
    _append_line(lines, "from math import log2,ceil")
    _append_line(lines)
    _append_line(lines, "NODE_MODULE = %r" % node_module)
    _append_line(lines, "TEMPLATE_MODULE = %r" % template_module)
    _append_line(lines, "GENERATED_PROJECT_ROOT = Path(%r)" % str(project_root))
    _append_line(lines, "NODE_PACKAGE = NODE_MODULE.split('.', 1)[0]")
    _append_line(lines, "TEMPLATE_PACKAGE = TEMPLATE_MODULE.split('.', 1)[0]")
    _append_line(lines)
    _append_line(lines, "def _find_project_root(start_dir: Path) -> Path:")
    _append_line(lines, "required_dirs = [NODE_PACKAGE, TEMPLATE_PACKAGE]", 1)
    _append_line(lines, "for candidate in [start_dir] + list(start_dir.parents):", 1)
    _append_line(lines, "if all((candidate / item).exists() for item in required_dirs):", 2)
    _append_line(lines, "return candidate", 3)
    _append_line(lines, "if GENERATED_PROJECT_ROOT.exists() and all((GENERATED_PROJECT_ROOT / item).exists() for item in required_dirs):", 1)
    _append_line(lines, "return GENERATED_PROJECT_ROOT", 2)
    _append_line(lines, "return start_dir.parent", 1)
    _append_line(lines)
    _append_line(lines, "PROJECT_ROOT = _find_project_root(Path(__file__).resolve().parent)")
    _append_line(lines, "# Add project root so sibling packages can be imported when running generated scripts.")
    _append_line(lines, "sys.path.insert(0, str(PROJECT_ROOT))")
    _append_line(lines, "# Add lwnoc_topo to path when topo_core is vendored under that directory.")
    _append_line(lines, "if (PROJECT_ROOT / 'lwnoc_topo').exists():")
    _append_line(lines, "sys.path.insert(0, str(PROJECT_ROOT / 'lwnoc_topo'))", 1)
    _append_line(lines)
    _append_line(lines, "from topo_core.node.uhdlWrapperNode import UhdlWrapperNode")
    _append_line(lines, "from topo_core.utils.networkHierOpt import connect")
    _append_line(lines, "from topo_core.utils.data_topology import DataTopology")
    _append_line(lines, "_template_module = import_module(TEMPLATE_MODULE)")
    _append_line(lines, "for _name in dir(_template_module):")
    _append_line(lines, "if not _name.startswith('_'):", 1)
    _append_line(lines, "globals()[_name] = getattr(_template_module, _name)", 2)
    _append_line(lines, "REQUIRED_NODE_NUM = %d" % spec["required_node_num"])
    _append_line(lines, "NODE_NUM = max(NODE_NUM, REQUIRED_NODE_NUM)")
    _append_line(lines, "for _name in dir(_template_module):")
    _append_line(lines, "_value = getattr(_template_module, _name)", 1)
    _append_line(lines, "_set_macro = getattr(_value, 'set_macro', None)", 1)
    _append_line(lines, "if callable(_set_macro) and not isinstance(_value, type):", 1)
    _append_line(lines, "_set_macro('MNOC_RING_NODE_NUM', NODE_NUM)", 2)
    _append_line(lines, "_node_module = import_module(NODE_MODULE)")
    _append_line(lines, "MnocRingBufNode = getattr(_node_module, 'MnocRingBufNode')")
    _append_line(lines, "MnocRingSpNode = getattr(_node_module, 'MnocRingSpNode')")
    _append_line(lines, "MnocRingAsyncBridgeNode = getattr(_node_module, 'MnocRingAsyncBridgeNode')")
    _append_line(lines, "MnocIniuNode = getattr(_node_module, 'MnocIniuNode')")
    _append_line(lines, "MnocTniuNode = getattr(_node_module, 'MnocTniuNode')")
    _append_line(lines, "from topo_core.memory_view import MemoryView, MemorySpace, KB, MB, GB")
    _append_line(lines)
    _append_line(lines, "NODE_ID_WIDTH=ceil(log2(NODE_NUM))")
    _append_line(lines)
    _append_line(lines, "class %s(UhdlWrapperNode):" % class_name)
    _append_line(lines, "def __init__(self, id: str = %r):" % spec["top_id"], 1)
    _append_line(lines, "super().__init__(id=id)", 2)
    _append_line(lines)
    _append_line(lines, "# Instantiate nodes", 2)

    if ring_count > 0:
        _append_line(lines, "for i in range(%d):" % ring_count, 2)
        for group in endpoint_groups:
            if len(group["items"]) != ring_count:
                continue
            family = group["family"]
            role = group["role"]
            cfg_symbol = group["cfg_symbol"]
            node_cls = "MnocIniuNode" if role == "iniu" else "MnocTniuNode"
            _append_line(
                lines,
                "setattr(self, f'%s_%s{i}_node', %s(id=f'%s_%s{i}_node', cfg=%s, node_name=f'%s_%s{i}', node_id_width=NODE_ID_WIDTH))"
                % (family, role, node_cls, family, role, cfg_symbol, family, role),
                3,
            )
        _append_line(lines, "setattr(self, f'sp_node{i}', MnocRingSpNode(id=f'sp_node{i}', cfg=network_config))", 3)
        _append_line(lines)

    for group in endpoint_groups:
        if len(group["items"]) == ring_count and ring_count > 0:
            continue
        family = group["family"]
        role = group["role"]
        cfg_symbol = group["cfg_symbol"]
        node_cls = "MnocIniuNode" if role == "iniu" else "MnocTniuNode"
        _append_line(lines, "for i in range(%d):" % len(group["items"]), 2)
        _append_line(
            lines,
            "setattr(self, f'%s_%s{i}_node', %s(id=f'%s_%s{i}_node', cfg=%s, node_name=f'%s_%s{i}', node_id_width=NODE_ID_WIDTH))"
            % (family, role, node_cls, family, role, cfg_symbol, family, role),
            3,
        )
        _append_line(lines)

    if ring_count > 0 and buffer_ranges is not None:
        total_buffers = len(spec["rings"][0]["buffer_indices"])
        _append_line(lines, "for i in range(%d):" % ring_count, 2)
        _append_line(lines, "for j in range(%d):" % total_buffers, 3)
        _append_line(lines, "setattr(self, f'ring{i}_buff_node_{j}', MnocRingBufNode(id=f'ring{i}_buff_node_{j}', cfg=network_config))", 4)
        _append_line(lines)
    else:
        for ring in spec["rings"]:
            for start, end in _contiguous_ranges(ring["buffer_indices"]):
                if start == end:
                    _append_line(lines, "setattr(self, 'ring%d_buff_node_%d', MnocRingBufNode(id='ring%d_buff_node_%d', cfg=network_config))" % (ring["ring_index"], start, ring["ring_index"], start), 2)
                else:
                    _append_line(lines, "for j in range(%d, %d):" % (start, end + 1), 2)
                    _append_line(lines, "setattr(self, f'ring%d_buff_node_{j}', MnocRingBufNode(id=f'ring%d_buff_node_{j}', cfg=network_config))" % (ring["ring_index"], ring["ring_index"]), 3)
                    _append_line(lines)

    if ring_count > 0 and async_specs is not None:
        total_async = len(spec["rings"][0]["async_indices"])
        _append_line(lines, "for i in range(%d):" % ring_count, 2)
        _append_line(lines, "for j in range(%d):" % total_async, 3)
        if has_physical_harden:
            _append_line(lines, "setattr(self, f'ring{i}_async_node{j}', MnocRingAsyncBridgeNode(id=f'ring{i}_async_node{j}'))", 4)
        else:
            _append_line(lines, "setattr(self, f'ring{i}_async_node{j}', MnocRingAsyncBridgeNode(id=f'ring{i}_async_node{j}', sync_mode=True))", 4)
        _append_line(lines)
    else:
        for ring in spec["rings"]:
            for async_index in ring["async_indices"]:
                _append_line(lines, "setattr(self, 'ring%d_async_node%d', MnocRingAsyncBridgeNode(id='ring%d_async_node%d'))" % (ring["ring_index"], async_index, ring["ring_index"], async_index), 2)
        _append_line(lines)

    for group in endpoint_groups:
        items = group["items"]
        ids = [int(item["data_topo_id"]) for item in items]
        start_id = ids[0]
        if ids == list(range(start_id, start_id + len(ids))):
            _append_line(lines, "for i in range(%d):" % len(items), 2)
            if start_id == 0:
                id_expr = "i"
            else:
                id_expr = "i + %d" % start_id
            _append_line(lines, "getattr(self, f'%s_%s{i}_node').set_data_topo_id(%s)" % (group["family"], group["role"], id_expr), 3)
        else:
            for item in items:
                _append_line(lines, "self.%s.set_data_topo_id(%d)" % (item["attr_name"], item["data_topo_id"]), 2)
        _append_line(lines)

    _append_line(lines, "# build data topology", 2)
    _append_line(lines, "self.datatopo = DataTopology('mnoc_ring')", 2)
    if ring_count > 0 and endpoint_pattern_safe and buffer_ranges is not None and async_specs is not None:
        _append_line(lines, "for i in range(%d):" % ring_count, 2)
        for endpoint in endpoint_by_ring[ring_indices[0]]:
            _append_line(lines, "self.datatopo.add(getattr(self, f'%s_%s{i}_node'))" % (endpoint["family"], endpoint["role"]), 3)
        _append_line(lines, "self.datatopo.add(getattr(self, f'sp_node{i}'))", 3)
        _append_line(lines, "for j in range(%d):" % len(spec["rings"][0]["buffer_indices"]), 3)
        _append_line(lines, "self.datatopo.add(getattr(self, f'ring{i}_buff_node_{j}'))", 4)
        _append_line(lines, "for j in range(%d):" % len(spec["rings"][0]["async_indices"]), 3)
        _append_line(lines, "self.datatopo.add(getattr(self, f'ring{i}_async_node{j}'))", 4)
        _append_line(lines)
    else:
        for ring in spec["rings"]:
            for endpoint in endpoint_by_ring[ring["ring_index"]]:
                _append_line(lines, "self.datatopo.add(self.%s)" % endpoint["attr_name"], 2)
            _append_line(lines, "self.datatopo.add(self.%s)" % ring["sp_attr_name"], 2)
            for buffer_index in ring["buffer_indices"]:
                _append_line(lines, "self.datatopo.add(self.ring%d_buff_node_%d)" % (ring["ring_index"], buffer_index), 2)
            for async_index in ring["async_indices"]:
                _append_line(lines, "self.datatopo.add(self.ring%d_async_node%d)" % (ring["ring_index"], async_index), 2)
        _append_line(lines)

    _append_line(lines, "# global interfaces", 2)
    for family in spec["system_families"]:
        _append_line(lines, "self.add_interface('clk_%s_sys', is_global=True)" % family, 2)
        _append_line(lines, "self.add_interface('rst_%s_sys', is_global=True)" % family, 2)
    _append_line(lines)
    if has_physical_harden and top_clock_domains == ["up", "dn"]:
        _append_line(lines, "for side in ['up', 'dn']:", 2)
        _append_line(lines, "self.add_interface(f'clk_ring_{side}', is_global=True)", 3)
        _append_line(lines, "self.add_interface(f'rst_ring_{side}', is_global=True)", 3)
    else:
        for partition in top_clock_domains:
            _append_line(lines, "self.add_interface('%s', is_global=True)" % _clk_ring_signal(partition, has_physical_harden), 2)
            _append_line(lines, "self.add_interface('%s', is_global=True)" % _rst_ring_signal(partition, has_physical_harden), 2)
    _append_line(lines)

    _append_line(lines, "# system functional clocks", 2)
    if ring_count > 0 and endpoint_pattern_safe:
        _append_line(lines, "for i in range(%d):" % ring_count, 2)
        for endpoint in endpoint_by_ring[ring_indices[0]]:
            _append_line(lines, "connect(self.clk_%s_sys, getattr(self, f'%s_%s{i}_node').clk_sys_func)" % (endpoint["family"], endpoint["family"], endpoint["role"]), 3)
            _append_line(lines, "connect(self.rst_%s_sys, getattr(self, f'%s_%s{i}_node').rst_sys_func_n)" % (endpoint["family"], endpoint["family"], endpoint["role"]), 3)
        sp_partition = next(element["partition"] for element in spec["rings"][0]["elements"] if element["kind"] == "sp") if has_physical_harden else shared_top_clock_domain
        _append_line(lines, "connect(self.%s, getattr(self, f'sp_node{i}').clk)" % _clk_ring_signal(sp_partition, has_physical_harden), 3)
        _append_line(lines)
    else:
        for ring in spec["rings"]:
            for endpoint in endpoint_by_ring[ring["ring_index"]]:
                _append_line(lines, "connect(self.clk_%s_sys, self.%s.clk_sys_func)" % (endpoint["family"], endpoint["attr_name"]), 2)
                _append_line(lines, "connect(self.rst_%s_sys, self.%s.rst_sys_func_n)" % (endpoint["family"], endpoint["attr_name"]), 2)
            sp_partition = next(element["partition"] for element in ring["elements"] if element["kind"] == "sp") if has_physical_harden else shared_top_clock_domain
            _append_line(lines, "connect(self.%s, self.%s.clk)" % (_clk_ring_signal(sp_partition, has_physical_harden), ring["sp_attr_name"]), 2)
        _append_line(lines)

    _append_line(lines, "# top clocks and buffers", 2)
    if ring_count > 0 and endpoint_pattern_safe:
        _append_line(lines, "for i in range(%d):" % ring_count, 2)
        for endpoint in endpoint_by_ring[ring_indices[0]]:
            endpoint_top_partition = endpoint["top_partition"] if has_physical_harden else shared_top_clock_domain
            _append_line(lines, "connect(self.%s, getattr(self, f'%s_%s{i}_node').clk_top_func)" % (_clk_ring_signal(endpoint_top_partition, has_physical_harden), endpoint["family"], endpoint["role"]), 3)
            _append_line(lines, "connect(self.%s, getattr(self, f'%s_%s{i}_node').rst_top_func_n)" % (_rst_ring_signal(endpoint_top_partition, has_physical_harden), endpoint["family"], endpoint["role"]), 3)
        if buffer_ranges is not None and has_physical_harden:
            for partition, ranges in buffer_ranges.items():
                for start, end in ranges:
                    range_expr = "range(%d)" % (end + 1) if start == 0 else "range(%d, %d)" % (start, end + 1)
                    _append_line(lines, "for j in %s:" % range_expr, 3)
                    _append_line(lines, "connect(self.%s, getattr(self, f'ring{i}_buff_node_{j}').clk)" % _clk_ring_signal(partition, has_physical_harden), 4)
                    _append_line(lines, "connect(self.%s, getattr(self, f'ring{i}_buff_node_{j}').rst_n)" % _rst_ring_signal(partition, has_physical_harden), 4)
            if async_specs is not None:
                for async_spec in async_specs:
                    async_index = async_spec["async_index"]
                    _append_line(lines, "connect(self.%s, getattr(self, f'ring{i}_async_node%d').clk_src)" % (_clk_ring_signal(async_spec["src_partition"], has_physical_harden), async_index), 3)
                    _append_line(lines, "connect(self.%s, getattr(self, f'ring{i}_async_node%d').rst_src_n)" % (_rst_ring_signal(async_spec["src_partition"], has_physical_harden), async_index), 3)
                    _append_line(lines, "connect(self.%s, getattr(self, f'ring{i}_async_node%d').clk_dst)" % (_clk_ring_signal(async_spec["dst_partition"], has_physical_harden), async_index), 3)
                    _append_line(lines, "connect(self.%s, getattr(self, f'ring{i}_async_node%d').rst_dst_n)" % (_rst_ring_signal(async_spec["dst_partition"], has_physical_harden), async_index), 3)
        else:
            total_buffers = len(spec["rings"][0]["buffer_indices"])
            _append_line(lines, "for j in range(%d):" % total_buffers, 3)
            _append_line(lines, "connect(self.%s, getattr(self, f'ring{i}_buff_node_{j}').clk)" % shared_clk_ring, 4)
            _append_line(lines, "connect(self.%s, getattr(self, f'ring{i}_buff_node_{j}').rst_n)" % shared_rst_ring, 4)
            if async_specs is not None:
                for async_spec in async_specs:
                    async_index = async_spec["async_index"]
                    _append_line(lines, "connect(self.%s, getattr(self, f'ring{i}_async_node%d').clk)" % (shared_clk_ring, async_index), 3)
                    _append_line(lines, "connect(self.%s, getattr(self, f'ring{i}_async_node%d').rst_n)" % (shared_rst_ring, async_index), 3)
        _append_line(lines)
    else:
        for ring in spec["rings"]:
            for endpoint in endpoint_by_ring[ring["ring_index"]]:
                endpoint_top_partition = endpoint["top_partition"] if has_physical_harden else shared_top_clock_domain
                _append_line(lines, "connect(self.%s, self.%s.clk_top_func)" % (_clk_ring_signal(endpoint_top_partition, has_physical_harden), endpoint["attr_name"]), 2)
                _append_line(lines, "connect(self.%s, self.%s.rst_top_func_n)" % (_rst_ring_signal(endpoint_top_partition, has_physical_harden), endpoint["attr_name"]), 2)
            for element in ring["elements"]:
                if element["kind"] == "buf":
                    element_partition = element["partition"] if has_physical_harden else shared_top_clock_domain
                    _append_line(lines, "connect(self.%s, self.%s.clk)" % (_clk_ring_signal(element_partition, has_physical_harden), element["attr_name"]), 2)
                    _append_line(lines, "connect(self.%s, self.%s.rst_n)" % (_rst_ring_signal(element_partition, has_physical_harden), element["attr_name"]), 2)
                elif element["kind"] == "async":
                    if has_physical_harden:
                        src_partition = element["src_partition"]
                        dst_partition = element["dst_partition"]
                        _append_line(lines, "connect(self.%s, self.%s.clk_src)" % (_clk_ring_signal(src_partition, has_physical_harden), element["attr_name"]), 2)
                        _append_line(lines, "connect(self.%s, self.%s.rst_src_n)" % (_rst_ring_signal(src_partition, has_physical_harden), element["attr_name"]), 2)
                        _append_line(lines, "connect(self.%s, self.%s.clk_dst)" % (_clk_ring_signal(dst_partition, has_physical_harden), element["attr_name"]), 2)
                        _append_line(lines, "connect(self.%s, self.%s.rst_dst_n)" % (_rst_ring_signal(dst_partition, has_physical_harden), element["attr_name"]), 2)
                    else:
                        _append_line(lines, "connect(self.%s, self.%s.clk)" % (shared_clk_ring, element["attr_name"]), 2)
                        _append_line(lines, "connect(self.%s, self.%s.rst_n)" % (shared_rst_ring, element["attr_name"]), 2)
        _append_line(lines)

    _append_line(lines, "# data flow topology", 2)
    if endpoint_pattern_safe and uniform_ring_sequence and spec["rings"]:
        ring = spec["rings"][0]
        _append_line(lines, "for i in range(%d):" % ring_count, 2)
        _append_line(lines, "ring_nodes = [", 3)
        for element in ring["elements"]:
            if element["kind"] == "sp":
                expr = "getattr(self, f'sp_node{i}')"
            elif element["kind"] == "buf":
                expr = "getattr(self, f'ring{i}_buff_node_%d')" % element["buffer_index"]
            elif element["kind"] == "async":
                expr = "getattr(self, f'ring{i}_async_node%d')" % element["async_index"]
            else:
                expr = "getattr(self, f'%s_%s{i}_node')" % (element["family"], element["role"])
            _append_line(lines, expr + ",", 4)
        _append_line(lines, "]", 3)
        _append_line(lines, "for src_node, dst_node in zip(ring_nodes, ring_nodes[1:] + ring_nodes[:1]):", 3)
        _append_line(lines, "connect(src_node.ring_flow_ctrl_out, dst_node.ring_flow_ctrl_in)", 4)
        _append_line(lines, "connect(src_node.pring_out_if, dst_node.pring_in_if)", 4)
        _append_line(lines, "connect(dst_node.nring_out_if, src_node.nring_in_if)", 4)
        _append_line(lines)
    else:
        for ring in spec["rings"]:
            _append_line(lines, "ring_nodes = [", 2)
            for element in ring["elements"]:
                if element["kind"] == "sp":
                    expr = "self.%s" % ring["sp_attr_name"]
                elif element["kind"] == "buf":
                    expr = "self.%s" % element["attr_name"]
                elif element["kind"] == "async":
                    expr = "self.%s" % element["attr_name"]
                else:
                    expr = "self.%s" % element["attr_name"]
                _append_line(lines, expr + ",", 3)
            _append_line(lines, "]", 2)
            _append_line(lines, "for src_node, dst_node in zip(ring_nodes, ring_nodes[1:] + ring_nodes[:1]):", 2)
            _append_line(lines, "connect(src_node.ring_flow_ctrl_out, dst_node.ring_flow_ctrl_in)", 3)
            _append_line(lines, "connect(src_node.pring_out_if, dst_node.pring_in_if)", 3)
            _append_line(lines, "connect(dst_node.nring_out_if, src_node.nring_in_if)", 3)
            _append_line(lines)

    _append_line(lines, "self.expose_unconnected_interfaces()", 2)
    _append_line(lines)
    _append_line(lines, "# memory and views", 2)
    for endpoint in spec["endpoints"]:
        if endpoint["role"] == "tniu":
            _append_line(lines, "MemorySpace(%d*MB).attach(self.%s)" % (endpoint["target_space_mb"], endpoint["attr_name"]), 2)
    _append_line(lines)
    for view_spec in spec["memory_views"]:
        family = view_spec["attr_name"].replace("_memory_view", "")
        _append_line(lines, "%s_iniu_nodes = [" % family, 2)
        for initiator in view_spec["initiators"]:
            _append_line(lines, "self.%s," % initiator, 3)
        _append_line(lines, "]", 2)
        _append_line(lines)
        _append_line(lines, "self.%s = MemoryView('%s')" % (view_spec["attr_name"], view_spec["display_name"]), 2)
        targets = view_spec["targets"]
        base_mb = 0
        for target_attr in targets[:-1]:
            _append_line(lines, "self.%s.append(self.%s, base_addr=%s)" % (view_spec["attr_name"], target_attr, _memory_base_expr(base_mb)), 2)
            target_endpoint = next(endpoint for endpoint in spec["endpoints"] if endpoint["attr_name"] == target_attr)
            base_mb += target_endpoint["target_space_mb"]
        _append_line(lines, "self.%s.append(self.%s, default=True)" % (view_spec["attr_name"], targets[-1]), 2)
        _append_line(lines, "self.%s.attach(*%s_iniu_nodes)" % (view_spec["attr_name"], family), 2)
        _append_line(lines)

    return "\n".join(lines).rstrip() + "\n"


def render_test_script(spec: Dict[str, Any], topo_module_name: str, topo_class_name: str, node_module: str) -> str:
    ring_count = len(spec["rings"])
    ring_indices = [ring["ring_index"] for ring in spec["rings"]]
    endpoint_by_ring = _ring_endpoint_groups(spec)
    buffer_ranges = _uniform_ring_buffer_ranges(spec) or {}
    async_specs = _uniform_ring_async_partitions(spec) or []
    partition_names = spec["partitions"]

    partition_endpoint_groups: Dict[str, List[Dict[str, Any]]] = {partition: [] for partition in partition_names}
    for partition in partition_names:
        seen_keys: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for ring_index in ring_indices:
            for endpoint in endpoint_by_ring[ring_index]:
                if endpoint["top_partition"] != partition:
                    continue
                key = (endpoint["family"], endpoint["role"])
                group = seen_keys.get(key)
                if group is None:
                    group = {"family": endpoint["family"], "role": endpoint["role"], "items": []}
                    seen_keys[key] = group
                    partition_endpoint_groups[partition].append(group)
                group["items"].append(endpoint)

    sp_partitions: Dict[str, bool] = {partition: False for partition in partition_names}
    for ring in spec["rings"]:
        for element in ring["elements"]:
            if element["kind"] == "sp":
                sp_partitions[element["partition"]] = True

    async_partition_sides: Dict[str, List[Tuple[int, str]]] = {partition: [] for partition in partition_names}
    for async_spec in async_specs:
        async_partition_sides[async_spec["dst_partition"]].append((async_spec["async_index"], "mst"))
        async_partition_sides[async_spec["src_partition"]].append((async_spec["async_index"], "slv"))

    lines: List[str] = []
    _append_line(lines, "import networkx as nx")
    _append_line(lines, "import html")
    _append_line(lines, "import re")
    _append_line(lines, "from importlib import import_module")
    _append_line(lines)
    _append_line(lines, "from %s import *" % topo_module_name)
    _append_line(lines, "_node_module = import_module(%r)" % node_module)
    _append_line(lines, "for _name in dir(_node_module):")
    _append_line(lines, "if not _name.startswith('_'):", 1)
    _append_line(lines, "globals()[_name] = getattr(_node_module, _name)", 2)
    _append_line(lines, "from topo_core.utils.serialization import TopologySerializer")
    _append_line(lines)
    _append_line(lines)
    _append_line(lines, "def visualize_node_level_topology(G: nx.MultiDiGraph, output_path: str = 'data_topology.png',")
    _append_line(lines, "                                   figsize=(20, 16), show_edge_labels=True):")
    _append_line(lines, '"""', 1)
    _append_line(lines, "Visualize a node-level data topology NetworkX graph and save to image.", 1)
    _append_line(lines, "Uses ring-aware layout to minimize edge crossings.", 1)
    _append_line(lines, '"""', 1)
    _append_line(lines, "if G.number_of_nodes() == 0:", 1)
    _append_line(lines, 'print("Graph is empty, nothing to visualize.")', 2)
    _append_line(lines, "return", 2)
    _append_line(lines)
    _append_line(lines, "try:", 1)
    _append_line(lines, "import matplotlib.pyplot as plt", 2)
    _append_line(lines, "except ModuleNotFoundError:", 1)
    _append_line(lines, "fallback_path = output_path.rsplit('.', 1)[0] + '.svg' if '.' in output_path else output_path + '.svg'", 2)
    _append_line(lines, "write_topology_svg(G, fallback_path, figsize=figsize)", 2)
    _append_line(lines, 'print(f"matplotlib is not installed, topology SVG saved to: {fallback_path}")', 2)
    _append_line(lines, "return", 2)
    _append_line(lines, "plt.figure(figsize=figsize)", 1)
    _append_line(lines)
    _append_line(lines, "pos = get_ring_layout(G)", 1)
    _append_line(lines)
    _append_line(lines, "nx.draw_networkx_nodes(G, pos, node_color='#87CEEB', node_size=2000, alpha=0.9)", 1)
    _append_line(lines, "nx.draw_networkx_labels(G, pos, font_size=8, font_weight='bold')", 1)
    _append_line(lines)
    _append_line(lines, "pring_edges = []", 1)
    _append_line(lines, "nring_edges = []", 1)
    _append_line(lines, "for u, v, key, data in G.edges(keys=True, data=True):", 1)
    _append_line(lines, "src_port = data.get('src_port', '')", 2)
    _append_line(lines, "if 'pring' in src_port:", 2)
    _append_line(lines, "pring_edges.append((u, v))", 3)
    _append_line(lines, "elif 'nring' in src_port:", 2)
    _append_line(lines, "nring_edges.append((u, v))", 3)
    _append_line(lines)
    _append_line(lines, "if pring_edges:", 1)
    _append_line(lines, "nx.draw_networkx_edges(G, pos, edgelist=pring_edges, arrows=True,", 2)
    _append_line(lines, "                               arrowsize=15, alpha=0.7, width=1.5,")
    _append_line(lines, "                               edge_color='#4169E1',")
    _append_line(lines, "                               connectionstyle=\"arc3,rad=0.1\",")
    _append_line(lines, "                               min_source_margin=25, min_target_margin=25)")
    _append_line(lines, "if nring_edges:", 1)
    _append_line(lines, "nx.draw_networkx_edges(G, pos, edgelist=nring_edges, arrows=True,", 2)
    _append_line(lines, "                               arrowsize=15, alpha=0.7, width=1.5,")
    _append_line(lines, "                               edge_color='#FF8C00',")
    _append_line(lines, "                               connectionstyle=\"arc3,rad=0.1\",")
    _append_line(lines, "                               min_source_margin=25, min_target_margin=25)")
    _append_line(lines)
    _append_line(lines, "import matplotlib.patches as mpatches", 1)
    _append_line(lines, "pring_patch = mpatches.Patch(color='#4169E1', label='pring')", 1)
    _append_line(lines, "nring_patch = mpatches.Patch(color='#FF8C00', label='nring')", 1)
    _append_line(lines, "plt.legend(handles=[pring_patch, nring_patch], loc='upper right', fontsize=10)", 1)
    _append_line(lines)
    _append_line(lines, "plt.title(f'Data Topology Graph ({G.number_of_nodes()} nodes, {G.number_of_edges()} edges)')", 1)
    _append_line(lines, "plt.axis('off')", 1)
    _append_line(lines, "plt.tight_layout()", 1)
    _append_line(lines, "plt.savefig(output_path, dpi=150, bbox_inches='tight')", 1)
    _append_line(lines, "plt.close()", 1)
    _append_line(lines, 'print(f"Topology graph saved to: {output_path}")', 1)
    _append_line(lines)
    _append_line(lines)
    _append_line(lines, "def _component_sort_key(nodes) -> tuple:")
    _append_line(lines, "ring_ids = []", 1)
    _append_line(lines, "for node in nodes:", 1)
    _append_line(lines, "match = re.search(r'ring(\\d+)', str(node))", 2)
    _append_line(lines, "if match:", 2)
    _append_line(lines, "ring_ids.append(int(match.group(1)))", 3)
    _append_line(lines, "labels = sorted(str(node) for node in nodes)", 1)
    _append_line(lines, "if ring_ids:", 1)
    _append_line(lines, "return (min(ring_ids), labels[0])", 2)
    _append_line(lines, "return (10 ** 9, labels[0])", 1)
    _append_line(lines)
    _append_line(lines)
    _append_line(lines, "def _component_ring_order(G: nx.MultiDiGraph, component_nodes) -> list:")
    _append_line(lines, "subgraph = G.subgraph(component_nodes)", 1)
    _append_line(lines, "pring_next = {}", 1)
    _append_line(lines, "for u, v, key, data in subgraph.edges(keys=True, data=True):", 1)
    _append_line(lines, "src_port = data.get('src_port', '')", 2)
    _append_line(lines, "if 'pring_out' in src_port:", 2)
    _append_line(lines, "pring_next[u] = v", 3)
    _append_line(lines, "if not pring_next:", 1)
    _append_line(lines, "return sorted(component_nodes, key=lambda node: str(node))", 2)
    _append_line(lines, "preferred = sorted((node for node in component_nodes if 'sp_node' in str(node)), key=lambda node: str(node))", 1)
    _append_line(lines, "start_node = preferred[0] if preferred else sorted(pring_next.keys(), key=lambda node: str(node))[0]", 1)
    _append_line(lines, "order = []", 1)
    _append_line(lines, "visited = set()", 1)
    _append_line(lines, "current = start_node", 1)
    _append_line(lines, "while current in pring_next and current not in visited:", 1)
    _append_line(lines, "order.append(current)", 2)
    _append_line(lines, "visited.add(current)", 2)
    _append_line(lines, "current = pring_next[current]", 2)
    _append_line(lines, "remaining = [node for node in sorted(component_nodes, key=lambda node: str(node)) if node not in visited]", 1)
    _append_line(lines, "order.extend(remaining)", 1)
    _append_line(lines, "return order", 1)
    _append_line(lines)
    _append_line(lines)
    _append_line(lines, "def get_ring_layout(G: nx.MultiDiGraph) -> dict:")
    _append_line(lines, '"""Lay out each disconnected ring component as its own circle."""', 1)
    _append_line(lines, "import math", 1)
    _append_line(lines)
    _append_line(lines, "nodes = list(G.nodes())", 1)
    _append_line(lines, "if len(nodes) == 0:", 1)
    _append_line(lines, "return {}", 2)
    _append_line(lines)
    _append_line(lines, "pos = {}", 1)
    _append_line(lines, "components = list(nx.weakly_connected_components(G)) if G.is_directed() else list(nx.connected_components(G))", 1)
    _append_line(lines, "if not components:", 1)
    _append_line(lines, "return nx.spring_layout(G, k=3, iterations=100, seed=42)", 2)
    _append_line(lines, "components = sorted(components, key=_component_sort_key)", 1)
    _append_line(lines, "cols = max(1, math.ceil(math.sqrt(len(components))))", 1)
    _append_line(lines, "rows = math.ceil(len(components) / cols)", 1)
    _append_line(lines, "component_radius = 1.15", 1)
    _append_line(lines, "gap_x = 3.6", 1)
    _append_line(lines, "gap_y = 3.6", 1)
    _append_line(lines, "for component_index, component_nodes in enumerate(components):", 1)
    _append_line(lines, "row = component_index // cols", 2)
    _append_line(lines, "col = component_index % cols", 2)
    _append_line(lines, "center_x = (col - (cols - 1) / 2.0) * gap_x", 2)
    _append_line(lines, "center_y = (((rows - 1) / 2.0) - row) * gap_y", 2)
    _append_line(lines, "ring_order = _component_ring_order(G, component_nodes)", 2)
    _append_line(lines, "node_count = len(ring_order)", 2)
    _append_line(lines, "for node_index, node in enumerate(ring_order):", 2)
    _append_line(lines, "angle = 2 * math.pi * node_index / node_count - math.pi / 2", 3)
    _append_line(lines, "pos[node] = (center_x + component_radius * math.cos(angle), center_y + component_radius * math.sin(angle))", 3)
    _append_line(lines, "return pos", 1)
    _append_line(lines)
    _append_line(lines)
    _append_line(lines, "def write_topology_svg(G: nx.MultiDiGraph, output_path: str, figsize=(20, 16)):")
    _append_line(lines, '"""Write a simple self-contained SVG topology diagram without matplotlib."""', 1)
    _append_line(lines, "pos = get_ring_layout(G)", 1)
    _append_line(lines, "if not pos:", 1)
    _append_line(lines, 'print("Graph is empty, skip SVG topology generation.")', 2)
    _append_line(lines, "return", 2)
    _append_line(lines, "width = int(figsize[0] * 80)", 1)
    _append_line(lines, "height = int(figsize[1] * 80)", 1)
    _append_line(lines, "padding = 80", 1)
    _append_line(lines, "node_radius = 28", 1)
    _append_line(lines, "xs = [coord[0] for coord in pos.values()]", 1)
    _append_line(lines, "ys = [coord[1] for coord in pos.values()]", 1)
    _append_line(lines, "min_x = min(xs)", 1)
    _append_line(lines, "max_x = max(xs)", 1)
    _append_line(lines, "min_y = min(ys)", 1)
    _append_line(lines, "max_y = max(ys)", 1)
    _append_line(lines, "span_x = max(max_x - min_x, 1.0)", 1)
    _append_line(lines, "span_y = max(max_y - min_y, 1.0)", 1)
    _append_line(lines, "scale_x = (width - 2 * padding) / span_x", 1)
    _append_line(lines, "scale_y = (height - 2 * padding) / span_y", 1)
    _append_line(lines, "scale = min(scale_x, scale_y)", 1)
    _append_line(lines, "canvas_pos = {}", 1)
    _append_line(lines, "for node, (x, y) in pos.items():", 1)
    _append_line(lines, "canvas_x = padding + (x - min_x) * scale", 2)
    _append_line(lines, "canvas_y = padding + (max_y - y) * scale", 2)
    _append_line(lines, "canvas_pos[node] = (canvas_x, canvas_y)", 2)
    _append_line(lines, "svg_lines = [", 1)
    _append_line(lines, "f'<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"{width}\" height=\"{height}\" viewBox=\"0 0 {width} {height}\">',", 2)
    _append_line(lines, "'<defs>' ,", 2)
    _append_line(lines, "'<marker id=\"arrow-blue\" markerWidth=\"10\" markerHeight=\"10\" refX=\"8\" refY=\"3\" orient=\"auto\" markerUnits=\"strokeWidth\"><path d=\"M0,0 L0,6 L9,3 z\" fill=\"#4169E1\"/></marker>',", 2)
    _append_line(lines, "'<marker id=\"arrow-orange\" markerWidth=\"10\" markerHeight=\"10\" refX=\"8\" refY=\"3\" orient=\"auto\" markerUnits=\"strokeWidth\"><path d=\"M0,0 L0,6 L9,3 z\" fill=\"#FF8C00\"/></marker>',", 2)
    _append_line(lines, "'</defs>',", 2)
    _append_line(lines, "f'<rect x=\"0\" y=\"0\" width=\"{width}\" height=\"{height}\" fill=\"white\"/>',", 2)
    _append_line(lines, "]", 1)
    _append_line(lines, "for u, v, key, data in G.edges(keys=True, data=True):", 1)
    _append_line(lines, "src_x, src_y = canvas_pos[u]", 2)
    _append_line(lines, "dst_x, dst_y = canvas_pos[v]", 2)
    _append_line(lines, "src_port = data.get('src_port', '')", 2)
    _append_line(lines, "if 'pring' in src_port:", 2)
    _append_line(lines, "color = '#4169E1'", 3)
    _append_line(lines, "marker = 'url(#arrow-blue)'", 3)
    _append_line(lines, "else:", 2)
    _append_line(lines, "color = '#FF8C00'", 3)
    _append_line(lines, "marker = 'url(#arrow-orange)'", 3)
    _append_line(lines, "svg_lines.append(f'<line x1=\"{src_x:.1f}\" y1=\"{src_y:.1f}\" x2=\"{dst_x:.1f}\" y2=\"{dst_y:.1f}\" stroke=\"{color}\" stroke-width=\"3\" marker-end=\"{marker}\" opacity=\"0.78\" />')", 2)
    _append_line(lines, "for node, (x, y) in canvas_pos.items():", 1)
    _append_line(lines, "label = html.escape(str(node))", 2)
    _append_line(lines, "svg_lines.append(f'<circle cx=\"{x:.1f}\" cy=\"{y:.1f}\" r=\"{node_radius}\" fill=\"#87CEEB\" stroke=\"#1F3A5F\" stroke-width=\"2\" />')", 2)
    _append_line(lines, "svg_lines.append(f'<text x=\"{x:.1f}\" y=\"{y + 4:.1f}\" text-anchor=\"middle\" font-size=\"12\" font-family=\"DejaVu Sans, sans-serif\" fill=\"#0F172A\">{label}</text>')", 2)
    _append_line(lines, "svg_lines.append('</svg>')", 1)
    _append_line(lines, "with open(output_path, 'w', encoding='utf-8') as svg_file:", 1)
    _append_line(lines, "svg_file.write('\\n'.join(svg_lines))", 2)
    _append_line(lines, 'print(f"Topology graph saved to: {output_path}")', 1)
    _append_line(lines)
    _append_line(lines)
    _append_line(lines, "def main():")
    _append_line(lines, "# 1. Define topology", 1)
    _append_line(lines, "logic_wrapper = %s()" % topo_class_name, 1)
    _append_line(lines, "TopologySerializer().save_to_file(logic_wrapper, './npu_mnoc_logic_topology.json')", 1)
    _append_line(lines)
    _append_line(lines, "# 2. Build (auto-configures logging, captures prints, shows progress)", 1)
    _append_line(lines, "comp = logic_wrapper.build(output_dir='build_logic')", 1)
    _append_line(lines)
    _append_line(lines, "# 3. Generate outputs", 1)
    _append_line(lines, "comp.generate_verilog(iteration=True)", 1)
    _append_line(lines, "comp.generate_filelist(abs_path=False, prefix='$MNOC_RING_LOGIC_TOP')", 1)
    _append_line(lines)
    _append_line(lines, "# 4. Visualize data topology", 1)
    _append_line(lines, "G = logic_wrapper.datatopo.to_networkx(node_level=True)", 1)
    _append_line(lines, "visualize_node_level_topology(G, 'mnoc_data_topology.png')", 1)
    if spec.get("has_physical_harden", True):
        _append_line(lines)
        _append_line(lines, "#################", 1)
        _append_line(lines, "# physical wrapper harden", 1)
        _append_line(lines, "#################", 1)
        _append_line(lines)

        wrapper_vars: List[str] = []
        for partition in partition_names:
            wrapper_var = "%s_harden" % partition
            wrapper_vars.append(wrapper_var)
            _append_line(lines, "%s = UhdlWrapperNode('ring_%s_harden_wrapper')" % (wrapper_var, partition), 1)
            _append_line(lines)
            for attachment in spec["partition_attachments"][partition]:
                if attachment["kind"] == "node":
                    _append_line(lines, "setattr(%s, '%s', getattr(logic_wrapper, '%s'))" % (wrapper_var, attachment["wrapper_attr"], attachment["source_attr"]), 1)
                    continue
                if attachment["kind"] == "endpoint_top_wrap":
                    endpoint_var = "%s_ref" % attachment["wrapper_attr"]
                    wrap_suffix = "iniu_top_wrap" if attachment["role"] == "iniu" else "tniu_top_wrap"
                    _append_line(lines, "%s = getattr(logic_wrapper, '%s')" % (endpoint_var, attachment["source_attr"]), 1)
                    _append_line(lines, "setattr(%s, '%s', %s.%s)" % (wrapper_var, attachment["wrapper_attr"], endpoint_var, wrap_suffix), 1)
                    continue
                if attachment["kind"] == "async_side":
                    _append_line(lines, "setattr(%s, '%s', getattr(logic_wrapper, '%s').%s_side)" % (wrapper_var, attachment["wrapper_attr"], attachment["source_attr"], attachment["side"]), 1)
            _append_line(lines)

        _append_line(lines, "#################", 1)
        _append_line(lines, "# ring_top_wrapper", 1)
        _append_line(lines, "#################", 1)
        _append_line(lines, "ring_top_wrap = UhdlWrapperNode('ring_top_wrap')", 1)
        for partition, wrapper_var in zip(partition_names, wrapper_vars):
            _append_line(lines, "ring_top_wrap.u_%s_harden = %s" % (partition, wrapper_var), 1)
        _append_line(lines)
        for wrapper_var in wrapper_vars:
            _append_line(lines, "%s.expose_unconnected_interfaces()" % wrapper_var, 1)
            _append_line(lines, "%s_comp = %s.build_uhdl()" % (wrapper_var, wrapper_var), 1)
            _append_line(lines, "%s_comp.output_dir = \"./build_logic\"" % wrapper_var, 1)
            _append_line(lines, "%s_comp.generate_verilog(iteration=True)" % wrapper_var, 1)
            _append_line(lines, "%s_comp.generate_filelist(abs_path=False, prefix='$MNOC_RING_LOGIC_TOP')" % wrapper_var, 1)
            _append_line(lines)
        _append_line(lines, "ring_top_wrap.expose_unconnected_interfaces()", 1)
        _append_line(lines, "ring_top_wrap_comp = ring_top_wrap.build_uhdl()", 1)
        _append_line(lines, "ring_top_wrap_comp.output_dir = \"./build_logic\"", 1)
        _append_line(lines, "ring_top_wrap_comp.generate_verilog(iteration=True)", 1)
        _append_line(lines, "ring_top_wrap_comp.generate_filelist(abs_path=False, prefix='$MNOC_RING_LOGIC_TOP')", 1)
        _append_line(lines)
    else:
        _append_line(lines)
        _append_line(lines, 'print("Skip physical harden wrapper generation: markdown declares no harden partition.")', 1)
        _append_line(lines)
    _append_line(lines, 'print("All done!")', 1)
    _append_line(lines)
    _append_line(lines)
    _append_line(lines, "if __name__ == '__main__':")
    _append_line(lines, "main()", 1)

    return "\n".join(lines).rstrip() + "\n"


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def run_generated_test(test_script: Path, output_dir: Path) -> None:
    command = [sys.executable, str(test_script), "--output-dir", str(output_dir), "--skip-visualize"]
    subprocess.run(command, cwd=test_script.parent, check=True)


def run_vcs(rtl_qc_dir: Path) -> None:
    subprocess.run(["make", "vcs"], cwd=rtl_qc_dir, check=True)


def discover_project_root(search_paths: Sequence[Path], required_roots: Sequence[str]) -> Optional[Path]:
    checked: set[Path] = set()
    for base_path in search_paths:
        for candidate in [base_path] + list(base_path.parents):
            if candidate in checked:
                continue
            checked.add(candidate)
            if all((candidate / required_root).exists() for required_root in required_roots):
                return candidate
    return None


def summarize_spec(spec: Dict[str, Any]) -> str:
    endpoint_summary = [
        "%s[%s:%s->id%d]" % (
            endpoint["label"],
            endpoint["family"],
            endpoint["role"],
            endpoint["data_topo_id"],
        )
        for endpoint in spec["endpoints"]
    ]
    return "rings=%d partitions=%s endpoints=%s" % (
        len(spec["rings"]),
        ",".join(spec["partitions"]),
        ", ".join(endpoint_summary),
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate MemTopo.py and test driver from a markdown ring bus description")
    parser.add_argument("--markdown", required=True, help="Input markdown file")
    parser.add_argument("--output-dir", required=True, help="Directory where generated Python files will be written")
    parser.add_argument("--topo-file", default="MemTopo.py", help="Output topology Python filename")
    parser.add_argument("--test-file", default="test_ringbus.py", help="Output test Python filename")
    parser.add_argument("--class-name", default="MnocRingLogicTopo", help="Generated topology class name")
    parser.add_argument("--top-id", default="mnoc_ring_logic_topo", help="Top-level wrapper id used for generated RTL")
    parser.add_argument("--template-module", default="ai_ring.MemTemplate", help="Python module that exports cfg/template symbols used by generated MemTopo")
    parser.add_argument("--node-module", default="ai_ring.MemNode", help="Python module that exports MnocRing* and endpoint node classes")
    parser.add_argument("--project-root", default=None, help="Optional project root used for module discovery and embedded runtime fallback")
    parser.add_argument("--run", action="store_true", help="Run the generated test script after file generation")
    parser.add_argument(
        "--rtl-output-dir",
        default=None,
        help="RTL output dir used when --run is specified; defaults to <project_root>/build_logic",
    )
    parser.add_argument("--vcs", action="store_true", help="Run rtl_qc Makefile vcs after RTL generation")
    parser.add_argument(
        "--rtl-qc-dir",
        default=None,
        help="rtl_qc directory used when --vcs is specified; defaults to <project_root>/rtl_qc",
    )
    args = parser.parse_args(argv)

    markdown_path = Path(args.markdown).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    markdown_text = markdown_path.read_text(encoding="utf-8")
    spec = build_spec(markdown_text, top_id=args.top_id)
    validate_spec_against_markdown(spec, markdown_text)

    topo_path = output_dir / args.topo_file
    test_path = output_dir / args.test_file
    required_roots = [_top_level_package(args.template_module), _top_level_package(args.node_module)]
    project_root = Path(args.project_root).resolve() if args.project_root else None
    if project_root is not None and not all((project_root / required_root).exists() for required_root in required_roots):
        raise SpecError("Configured project root %s does not contain required directories %s" % (project_root, ", ".join(required_roots)))
    if project_root is None:
        project_root = discover_project_root(
            [markdown_path.parent, output_dir.parent, Path(__file__).resolve().parent],
            required_roots,
        )
    if project_root is None:
        raise SpecError("Unable to locate project root from markdown path %s, output dir %s, or generator location with required directories %s" % (markdown_path, output_dir, ", ".join(required_roots)))

    write_text(topo_path, render_memtopo(spec, class_name=args.class_name, template_module=args.template_module, node_module=args.node_module, project_root=project_root))
    write_text(test_path, render_test_script(spec, topo_module_name=topo_path.stem, topo_class_name=args.class_name, node_module=args.node_module))

    print("Generated topology: %s" % topo_path)
    print("Generated test driver: %s" % test_path)
    print("Parsed spec: %s" % summarize_spec(spec))
    print("Validated spec against markdown partition descriptions")

    rtl_output_dir = Path(args.rtl_output_dir).resolve() if args.rtl_output_dir else project_root / "build_logic"
    rtl_qc_dir = Path(args.rtl_qc_dir).resolve() if args.rtl_qc_dir else project_root / "rtl_qc"

    if args.run:
        run_generated_test(test_path, rtl_output_dir)
        print("Generated RTL output: %s" % rtl_output_dir)

    if args.vcs:
        if not args.run and not (rtl_output_dir / "mnoc_ring_logic_topo").exists():
            raise SpecError("VCS requested but build_logic output is missing; rerun with --run or generate RTL first")
        run_vcs(rtl_qc_dir)
        print("VCS completed in: %s" % rtl_qc_dir)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())