---
type: "query"
date: "2026-09-18T09:02:09.766663+00:00"
question: "请修复：当前主机未安装 ODA File Converter，无法安全转换 DWG"
contributor: "graphify"
outcome: "useful"
source_nodes: ["FloorPlanParseError", "parse_floor_plan()", "_read_document()", "import_floor_plan()"]
---

# Q: 请修复：当前主机未安装 ODA File Converter，无法安全转换 DWG

## Answer

Expanded from original query via graph vocab: [dwg, dxf, converted, floor, plan, upload, parse, file]. The DWG upload path is SmartConversation importFloorPlan -> POST /floor-plan -> parse_floor_plan -> _read_document. Root cause: ezdxf checked only its default Windows executable path and missed the custom F:\oda install. Fixed by resolving ODA_FILE_CONVERTER_PATH, configured ezdxf paths, PATH, versioned Program Files directories, and bounded drive-root oda directories before conversion. Real DWG conversion and parsing succeeded.

## Outcome

- Signal: useful

## Source Nodes

- FloorPlanParseError
- parse_floor_plan()
- _read_document()
- import_floor_plan()