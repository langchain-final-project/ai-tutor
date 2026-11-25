export type ProblemCondition = {
  id: string
  kind:
    | "point"
    | "radius"
    | "length"
    | "angle"
    | "formula"
    | "interval"
    | "relation"
    | "note"
  label: string
  value: string
  value_format: "text" | "latex"
  source_text: string
}

export type ConditionExtraction = {
  schema_version: "conditions-v1"
  status: "not_run" | "completed" | "failed"
  conditions: ProblemCondition[]
}