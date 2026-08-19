import type { DesignBrief } from "./types";

type TemplateValues = Pick<
  DesignBrief,
  | "space_type"
  | "workplane_height_m"
  | "target_illuminance_lx"
  | "target_cct_k"
  | "min_cri"
  | "target_ugr"
  | "target_uniformity_u0"
  | "max_lpd_w_m2"
>;

export type LightingTemplate = {
  id: string;
  name: string;
  standardReference: string;
  values: TemplateValues;
  lpdNote?: string;
};

const officeReference = "GB 50034-2024 表 5.3.2、4.5.1/4.5.2、表 6.3.5";

export const LIGHTING_TEMPLATES: readonly LightingTemplate[] = [
  {
    id: "standard-office",
    name: "普通办公室",
    standardReference: officeReference,
    values: {
      space_type: "普通办公室",
      workplane_height_m: 0.75,
      target_illuminance_lx: 300,
      target_cct_k: 4000,
      min_cri: 80,
      target_ugr: 19,
      target_uniformity_u0: 0.6,
      max_lpd_w_m2: 6.5,
    },
  },
  {
    id: "meeting-room",
    name: "会议室",
    standardReference: officeReference,
    values: {
      space_type: "会议室",
      workplane_height_m: 0.75,
      target_illuminance_lx: 300,
      target_cct_k: 4000,
      min_cri: 80,
      target_ugr: 19,
      target_uniformity_u0: 0.6,
      max_lpd_w_m2: 6.5,
    },
  },
  {
    id: "video-conference-room",
    name: "视频会议室",
    standardReference: officeReference,
    lpdNote: "LPD 按会议室对照值预填",
    values: {
      space_type: "视频会议室",
      workplane_height_m: 0.75,
      target_illuminance_lx: 750,
      target_cct_k: 4000,
      min_cri: 80,
      target_ugr: 19,
      target_uniformity_u0: 0.6,
      max_lpd_w_m2: 6.5,
    },
  },
];
