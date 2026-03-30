---
name: markdown-to-ringbus-rtl
description: 'Generate repository-ready ring bus topology Python and test driver files from a markdown architecture description, then optionally run the generated test script to produce build_logic RTL output and execute rtl_qc VCS compilation. Use when the user provides a .md ring bus architecture, wants MemTopo.py plus test_xxx.py, asks for markdown to rtl generation, ring_for_pd style topology generation, or wants an end-to-end markdown to build_logic to VCS flow.'
argument-hint: 'Input markdown path and output directory; optional class name, run flag, or VCS flag'
user-invocable: true
---

# Markdown To RingBus RTL

This workspace skill implements a standalone markdown-driven ring bus generator.

It defaults to the aichip_memnoc conventions, but the generator can target a different compatible workspace layout by overriding the node/template module paths instead of assuming fixed source file locations.

## Workflow

1. Read workspace guidance files such as AGENTS.md, README, or topology docs when present.
2. Discover the compatible node library, template cfg module, and one existing generated example from the current workspace instead of assuming fixed absolute paths.
3. Confirm the markdown description can be normalized into a ring bus topology supported by the discovered node classes.
4. Run the markdown helper generator to emit MemTopo.py and a test driver into the requested output directory.
5. If requested, run the generated test driver so build_logic is produced.
6. If requested, run rtl_qc Makefile VCS elaboration.
7. Report generated files, detected ring count, endpoint mapping, whether build_logic was produced, and whether VCS passed.

## Helper Command

```bash
python3 .github/skills/markdown-to-ringbus-rtl/generate_ringbus_rtl_from_md.py \
  --markdown <markdown_path> \
  --output-dir <output_dir> \
  --template-module <template_module> \
  --node-module <node_module> \
  --topo-file MemTopo.py \
  --test-file test_ringbus.py
```

Add these options when needed:

- --run
- --rtl-output-dir <repo_build_logic_dir>
- --vcs
- --rtl-qc-dir <repo_rtl_qc_dir>
- --class-name <name>
- --top-id <module_id>
- --template-module <python_module>
- --node-module <python_module>

## Input Expectations

- The markdown may use headings, bullets, prose, or one-line topology strings.
- Each ring must still be reducible to an ordered sequence of existing node kinds such as sp, bufN, asyncN, and endpoint nodes.
- Endpoint tokens should identify their role with iniu, tniu, initiator, target, master, or slave unless the role is already obvious from the token itself.
- If the markdown explicitly says there is no harden partition, generated MemTopo.py must use a single shared ring top clock and reset domain rather than up/dn clock-reset splitting.
- If the markdown cannot be normalized into a supported topology, stop and report the missing or ambiguous fields instead of guessing.

## Guardrails

- Do not invent new node classes.
- Do not silently assume a cfg symbol for unknown endpoint families.
- Keep generated code aligned with an existing example in the current workspace when one exists, rather than hard-coding one repository path.
- Prefer module discovery or explicit --template-module and --node-module overrides instead of assuming files like ai_ring/MemNode.py must exist at a fixed location.
- VCS validation should target the existing rtl_qc Makefile flow.
