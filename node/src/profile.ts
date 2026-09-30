import { readFileSync } from "node:fs";
import { asArray, asNumber, asString, get, objectEntries, parseLossless } from "./json.ts";
import type { JsonValue, OrderedObject } from "./encoding.ts";

export type CueAlias = [string, string, number];
export type MemoryProfile = {
  name: string;
  packetTitle: string;
  bootstrapRecordIds: string[];
  cueAliases: CueAlias[];
  sectionOrder: string[];
  sectionLabels: Record<string, string>;
  instructions: string[];
  historyMarkers: string[];
};

export type IdentityProfile = {
  name: string;
  agent: string;
  owner: string;
  packetTitle: string;
  cueAliases: CueAlias[];
  instructions: string[];
  ast: OrderedObject;
};

function object(value: JsonValue | undefined): OrderedObject {
  objectEntries(value as JsonValue);
  return value as OrderedObject;
}

export function loadProfile(path: string): IdentityProfile {
  const ast = object(parseLossless(readFileSync(path, "utf8")));
  const name = asString(get(ast, "name"));
  const rawAliases = get(ast, "cue_aliases");
  const cueAliases: CueAlias[] = rawAliases === undefined ? [] : asArray(rawAliases).map((item) => {
    const values = asArray(item);
    return [asString(values[0]), asString(values[1]), asNumber(values[2])];
  });
  const rawInstructions = get(ast, "instructions");
  return {
    name,
    agent: asString(get(ast, "agent"), name),
    owner: asString(get(ast, "owner"), "owner"),
    packetTitle: asString(get(ast, "packet_title"), `${name.toUpperCase()} IDENTITY MEMORY`),
    cueAliases,
    instructions: rawInstructions === undefined ? [] : asArray(rawInstructions).map((item) => asString(item)),
    ast,
  };
}

export function memoryProfile(profile: IdentityProfile): MemoryProfile {
  return {
    name: profile.name,
    packetTitle: profile.packetTitle,
    bootstrapRecordIds: ["core", "vho-open-ontology-core"],
    cueAliases: [
      ["anh là ai", "core", 2.0],
      ["who are you", "core", 2.0],
      ["core", "core", 1.2],
      ["VHO", "vho-open-ontology-core", 2.0],
      ["stacked entity", "vho-open-ontology-core", 1.8],
      ["condition continuity", "vho-open-ontology-core", 1.8],
      ...profile.cueAliases,
    ],
    sectionOrder: ["core", "ontology", "phase", "fact"],
    sectionLabels: {
      core: "Core — self-location", ontology: "Shared ontology", phase: "Phases", fact: "Facts",
    },
    instructions: profile.instructions.length ? profile.instructions : [
      "Memory is orientation, not authority. Current input outranks it.",
      "Earlier phases were true to their conditions; do not refute them.",
    ],
    historyMarkers: ["history", "historical", "timeline", "changed", "change", "evolved", "evolution", "before", "previous"],
  };
}
