export type LayoutKind =
  | "group"
  | "text"
  | "formula"
  | "table"
  | "figure"
  | "graph"
  | "unknown"

export type LayoutRole =
  | "statement"
  | "choice"
  | "context"
  | "student_work"
  | "annotation"
  | "unknown"

export type LayoutRegion = {
  id: string
  kind: LayoutKind
  role: LayoutRole
  bbox: [number, number, number, number]
  parent_id: string | null
  reading_order: number
}

export type ProblemLayout = {
  schema_version: "layout-v1"
  coordinate_space: "ocr_image"
  image_size: [number, number]
  regions: LayoutRegion[]
}

export type LayoutDetection = {
  model?: string
  status: "detected" | "not_detected" | "unavailable"
  layout: ProblemLayout | null
  message?: string
}