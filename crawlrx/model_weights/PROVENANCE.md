# Model weight provenance

## crawlrx-detr-v1.onnx (primary grid-classification weights)

- **What it is:** a real-time transformer object detector (RT-DETR-L architecture,
  exported to ONNX), 131,313,735 bytes. Input: `images`, dynamic batch/height/width,
  float32, 3-channel BGR/RGB image tensor. Output: `(1, 300, 84)` — 300 detections of
  4 box coordinates + 80 class scores.
- **Role:** first-pass grid/tile classifier for the challenge-solving service — fast,
  deterministic per-tile object classification. Novel or non-grid challenge imagery is
  escalated to the vision-language fallback lane instead.
- **Origin:** exported locally with ultralytics 8.4.21 from the upstream pretrained
  RT-DETR-L COCO checkpoint (`rtdetr-l.pt`, github.com/ultralytics/assets), then
  renamed and re-validated with onnxruntime (dummy inference + output-shape check).
  Architecture/weights lineage: **AGPL-3.0** (Ultralytics RT-DETR implementations) —
  see `THIRD_PARTY_NOTICES.md`.
- **SHA256:** `37f4bc4e749f7f071848ab0b1fc329cf260150b7f9f1fe445a59c0097b3424f3`
- **Numerics:** shipped float32. A float16 conversion was attempted and rejected:
  onnxconverter-common truncated a stored `inf` to `10000.0` and produced a
  type-inconsistent Cast node, so fp32 is the correctness-preserving choice.

## yolov8m-seg.pt

- **What it is:** an Ultralytics YOLOv8m-segmentation export (`yolov8m-seg.pt`,
  54,921,020 bytes).
- **Upstream / lineage:** trained and released by Ultralytics; the Ultralytics codebase and
  its released weights carry **AGPL-3.0** lineage
  (<https://github.com/ultralytics/ultralytics>). See `THIRD_PARTY_NOTICES.md` in the
  repository root for the license row.
- **SHA256:** `51fa7e5ef385efa6d5b1d8e31b73399be6ed5d7ca71bda4bd4b2794bb445c4f4`
- **Placement:** this file is not copied into the wheel or Docker image by the packaging in
  this repository; weight placement is decided at deployment time. Verify any copy against
  the SHA256 above before use.
- **Runtime inference:** the challenge-solving service's runtime inference paths use
  `yolov8n.onnx` (the YOLOv8n ONNX export published in the hcaptcha-challenger project
  releases, <https://github.com/QIN2DIM/hcaptcha-challenger>) executed through onnxruntime,
  plus the ultralytics model cache — not this `.pt` file. `yolov8m-seg.pt` is retained only
  as the recorded source/export lineage for the segmentation weights.
