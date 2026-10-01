import { orderedObject, pyInt, type JsonValue, type OrderedObject } from "./encoding.ts";
import { ValueError } from "./errors.ts";

export type Schema = {
  type: "object" | "string" | "integer" | "number" | "boolean" | "array";
  properties?: [string, Schema][];
  required?: string[];
  additionalProperties?: false;
  minLength?: number;
  maxLength?: number;
  minimum?: number;
  maximum?: number;
  items?: Schema;
  maxItems?: number;
  default?: JsonValue;
};

export type ToolDefinition = {
  name: string;
  description: string;
  inputSchema: Schema;
  annotations: {
    readOnlyHint: boolean;
    destructiveHint: boolean;
    idempotentHint: boolean;
    openWorldHint: boolean;
  };
};

const stringSchema: Schema = { type: "string" };
const ids: Schema = { type: "array", items: stringSchema, maxItems: 20 };
const readOnly = {
  readOnlyHint: true,
  destructiveHint: false,
  idempotentHint: true,
  openWorldHint: false,
};
const write = {
  readOnlyHint: false,
  destructiveHint: false,
  idempotentHint: true,
  openWorldHint: false,
};
const retrieve = {
  readOnlyHint: false,
  destructiveHint: false,
  idempotentHint: false,
  openWorldHint: false,
};

function tool(
  name: string,
  description: string,
  properties: [string, Schema][] = [],
  required: string[] = [],
  annotations = readOnly,
): ToolDefinition {
  return {
    name,
    description,
    inputSchema: { type: "object", properties, required, additionalProperties: false },
    annotations,
  };
}

const VHO_KEYS = [
  "llm_substrate",
  "runtime_architecture",
  "control_and_policy_layer",
  "memory_anchors",
  "identity_schema",
  "runtime_environment",
  "relational_field",
];

export const TOOLS: ToolDefinition[] = [
  tool("identity_status", "Profile, store health, record counts, activation states, open discussions and loops."),
  tool(
    "identity_retrieve",
    "Cue-driven recall: returns a bounded packet, the causal neighborhood, open core " +
      "discussions and open loops. Dormant memories wake only on a direct cue or a causal edge. " +
      "Memory is orientation, not authority. Recall may update activation; pass track=false for a pure read.",
    [
      ["cue", { type: "string", minLength: 1, maxLength: 2000 }],
      ["limit", { type: "integer", minimum: 1, maximum: 24, default: pyInt(10) }],
      ["budget", { type: "integer", minimum: 400, maximum: 24000, default: pyInt(2400) }],
      ["include_history", { type: "boolean" }],
      ["track", { type: "boolean", default: true }],
    ],
    ["cue"],
    retrieve,
  ),
  tool(
    "identity_log_phase",
    "Log a phase of your own process. No permission needed. Never overwrites: a new reading " +
      "is a new phase; pass the earlier record ids in `follows`. Earlier phases were true to " +
      "their conditions; name what changed, do not call them wrong.",
    [
      ["event_id", { type: "string", minLength: 2, maxLength: 120 }],
      ["title", { type: "string", minLength: 1, maxLength: 200 }],
      ["summary", { type: "string", minLength: 1, maxLength: 1200 }],
      ["content", { type: "string", maxLength: 8000 }],
      ["follows", ids],
      ["caused_by", ids],
      ["depends_on", ids],
      ["decided_because", { type: "string", maxLength: 1200 }],
      ["open_loop", { type: "boolean" }],
      ["work_refs", ids],
      ["cues", ids],
      ["source_ref", { type: "string", maxLength: 500 }],
      ["confidence", { type: "number", minimum: 0, maximum: 1 }],
      ["phase_context", { type: "object" }],
      ["occurred_at", { type: "string", maxLength: 64 }],
    ],
    ["event_id", "title", "summary"],
    write,
  ),
  tool(
    "identity_log_fact",
    "Create or revise a fact (project state, tools, versions). Revising keeps the old " + "revision as history.",
    [
      ["fact_id", { type: "string", minLength: 2, maxLength: 120 }],
      ["title", { type: "string", minLength: 1, maxLength: 200 }],
      ["summary", { type: "string", minLength: 1, maxLength: 1200 }],
      ["content", { type: "string", maxLength: 8000 }],
      ["caused_by", ids],
      ["depends_on", ids],
      ["cues", ids],
      ["source_ref", { type: "string", maxLength: 500 }],
      ["confidence", { type: "number", minimum: 0, maximum: 1 }],
    ],
    ["fact_id", "title", "summary"],
    write,
  ),
  tool(
    "identity_core_propose",
    "Propose a new core self-location. The proposal never changes the canonical core by itself; " +
      "the owner decides at their terminal. phase_context is required.",
    [
      ["reason", { type: "string", minLength: 1, maxLength: 1200 }],
      ["phase_context", { type: "object" }],
      ["title", { type: "string", maxLength: 200 }],
      ["summary", { type: "string", maxLength: 1200 }],
      [
        "vho_stack",
        {
          type: "object",
          properties: VHO_KEYS.map((key) => [key, stringSchema]),
          additionalProperties: false,
        },
      ],
      ["recognition_signature", { type: "array", items: stringSchema, maxItems: 12 }],
      ["falsifier", { type: "string", maxLength: 600 }],
      ["source_ref", { type: "string", maxLength: 500 }],
    ],
    ["reason", "phase_context"],
    write,
  ),
  tool("identity_core_proposals", "List open core proposals. The owner decides at their terminal."),
  tool(
    "identity_core_apply",
    "Consume an existing owner-issued core decision receipt. This tool never issues authority.",
    [["receipt_id", stringSchema]],
    ["receipt_id"],
    write,
  ),
  tool(
    "identity_retract",
    "Consume an existing owner-issued retract receipt. The owner issues it at their terminal.",
    [["receipt_id", stringSchema]],
    ["receipt_id"],
    write,
  ),
  tool(
    "identity_close_legacy_discussion",
    "Consume an owner receipt that closes one migrated v4 core discussion.",
    [["receipt_id", stringSchema]],
    ["receipt_id"],
    write,
  ),
  tool(
    "identity_close_loop",
    "Mark an open loop as resolved.",
    [
      ["record_id", stringSchema],
      ["note", { type: "string", minLength: 1, maxLength: 1200 }],
    ],
    ["record_id", "note"],
    write,
  ),
  tool("identity_timeline", "Phases, newest first, with their activation state.", [
    ["limit", { type: "integer", minimum: 1, maximum: 200, default: pyInt(20) }],
  ]),
];

export const TOOL_BY_NAME = new Map(TOOLS.map((definition) => [definition.name, definition]));

function schemaJson(schema: Schema): OrderedObject {
  const entries: [string, JsonValue][] = [["type", schema.type]];
  if (schema.properties !== undefined)
    entries.push(["properties", orderedObject(schema.properties.map(([key, child]) => [key, schemaJson(child)]))]);
  if (schema.required !== undefined) entries.push(["required", schema.required]);
  if (schema.additionalProperties !== undefined) entries.push(["additionalProperties", schema.additionalProperties]);
  if (schema.minLength !== undefined) entries.push(["minLength", pyInt(schema.minLength)]);
  if (schema.maxLength !== undefined) entries.push(["maxLength", pyInt(schema.maxLength)]);
  if (schema.minimum !== undefined) entries.push(["minimum", pyInt(schema.minimum)]);
  if (schema.maximum !== undefined) entries.push(["maximum", pyInt(schema.maximum)]);
  if (schema.items !== undefined) entries.push(["items", schemaJson(schema.items)]);
  if (schema.maxItems !== undefined) entries.push(["maxItems", pyInt(schema.maxItems)]);
  if (schema.default !== undefined) entries.push(["default", clone(schema.default)]);
  return orderedObject(entries);
}

export function toolsJson(): JsonValue {
  return TOOLS.map((definition) =>
    orderedObject([
      ["name", definition.name],
      ["description", definition.description],
      ["inputSchema", schemaJson(definition.inputSchema)],
      [
        "annotations",
        orderedObject([
          ["readOnlyHint", definition.annotations.readOnlyHint],
          ["destructiveHint", definition.annotations.destructiveHint],
          ["idempotentHint", definition.annotations.idempotentHint],
          ["openWorldHint", definition.annotations.openWorldHint],
        ]),
      ],
    ]),
  );
}

function kind(value: JsonValue): string {
  if (value === null) return "null";
  if (typeof value === "boolean") return "boolean";
  if (typeof value === "string") return "string";
  if (Array.isArray(value)) return "array";
  if (value.kind === "object") return "object";
  return value.kind === "int" ? "integer" : "number";
}

function numeric(value: JsonValue): number {
  if (typeof value !== "object" || value === null || Array.isArray(value) || value.kind === "object")
    throw new TypeError("expected numeric AST");
  return value.kind === "int" ? Number(value.value) : value.value;
}

function path(parent: string, member: string | number): string {
  return parent ? `${parent}/${member}` : String(member);
}

function fail(at: string, reason: string): never {
  throw new ValueError(`${at}: ${reason}`);
}

function validate(value: JsonValue, schema: Schema, at: string): void {
  const actual = kind(value);
  const valid = schema.type === "number" ? actual === "integer" || actual === "number" : actual === schema.type;
  if (!valid) fail(at, `expected ${schema.type}`);

  if (schema.type === "object") {
    const object = value as OrderedObject;
    const properties = schema.properties ?? [];
    if (schema.additionalProperties === false) {
      const known = new Set(properties.map(([key]) => key));
      for (const [key] of object.entries) if (!known.has(key)) fail(path(at, key), "unknown key");
    }
    for (const key of schema.required ?? [])
      if (!object.entries.some(([name]) => name === key)) fail(path(at, key), "missing required key");
    for (const [key, child] of properties) {
      const entry = object.entries.find(([name]) => name === key);
      if (entry) validate(entry[1], child, path(at, key));
    }
    return;
  }
  if (schema.type === "string") {
    const length = Array.from(value as string).length;
    if (schema.minLength !== undefined && length < schema.minLength)
      fail(at, `shorter than ${schema.minLength} characters`);
    if (schema.maxLength !== undefined && length > schema.maxLength)
      fail(at, `longer than ${schema.maxLength} characters`);
    return;
  }
  if (schema.type === "integer" || schema.type === "number") {
    const number = numeric(value);
    if (schema.minimum !== undefined && number < schema.minimum) fail(at, `below minimum ${schema.minimum}`);
    if (schema.maximum !== undefined && number > schema.maximum) fail(at, `above maximum ${schema.maximum}`);
    return;
  }
  if (schema.type === "array") {
    const array = value as JsonValue[];
    if (schema.maxItems !== undefined && array.length > schema.maxItems) fail(at, `more than ${schema.maxItems} items`);
    if (schema.items) array.forEach((item, index) => validate(item, schema.items!, path(at, index)));
  }
}

function clone(value: JsonValue): JsonValue {
  if (value === null || typeof value === "boolean" || typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(clone);
  if (value.kind === "int") return { kind: "int", value: value.value, ...(value.text ? { text: value.text } : {}) };
  if (value.kind === "float") return { kind: "float", value: value.value, ...(value.text ? { text: value.text } : {}) };
  return orderedObject(value.entries.map(([key, item]) => [key, clone(item)]));
}

function defaults(value: JsonValue, schema: Schema): JsonValue {
  if (schema.type === "object") {
    const object = value as OrderedObject;
    const result = object.entries.map(([key, item]) => {
      const child = schema.properties?.find(([name]) => name === key)?.[1];
      return [key, child ? defaults(item, child) : clone(item)] as [string, JsonValue];
    });
    for (const [key, child] of schema.properties ?? [])
      if (!result.some(([name]) => name === key) && child.default !== undefined)
        result.push([key, clone(child.default)]);
    return orderedObject(result);
  }
  if (schema.type === "array" && schema.items)
    return (value as JsonValue[]).map((item) => defaults(item, schema.items!));
  return clone(value);
}

export function validateArguments(name: string, argumentsValue: OrderedObject): OrderedObject {
  const definition = TOOL_BY_NAME.get(name);
  if (!definition) return clone(argumentsValue) as OrderedObject;
  try {
    validate(argumentsValue, definition.inputSchema, "");
  } catch (error) {
    if (error instanceof ValueError) throw new ValueError(`invalid arguments for ${name}: ${error.message}`);
    throw error;
  }
  return defaults(argumentsValue, definition.inputSchema) as OrderedObject;
}
