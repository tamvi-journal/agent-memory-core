// One evidence-identity law for the whole runtime: the governance gate, evidence
// insertion, authority evidence and migration all use this module, so the
// independence decision and the stored independence_group cannot drift.
// Mirrors memory_core/store.py `_identity_segment` and `canonical_evidence_identity`.
import { hashPayload, orderedObject, pyFloatRepr, pyStrip, toJsonValue, type PyFloat, type PyInt } from "./encoding.ts";
import { normalizeIdentityV1 } from "./text.ts";

export const EVIDENCE_IDENTITY_VERSION = "evidence-v2";

/** Python `_identity_segment`: frozen identity-v1 table, `str.strip`, then the ASCII slug. */
export function identitySegment(value: string, fallback: string): string {
  const normalized = pyStrip(normalizeIdentityV1(value))
    .replace(/[^a-z0-9._/-]+/gu, "-")
    .replace(/^-+|-+$/gu, "");
  return normalized || fallback;
}

/** Python `str(value)` for the JSON-shaped values evidence can carry. */
function pyStr(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null) return "None";
  if (typeof value === "boolean") return value ? "True" : "False";
  if (typeof value === "number") return pyFloatRepr(value);
  if (typeof value === "bigint") return value.toString();
  if (value && typeof value === "object" && "kind" in value) {
    const typed = value as PyInt | PyFloat;
    return typed.kind === "int" ? typed.value.toString() : pyFloatRepr(typed.value);
  }
  return String(value);
}

/** Python `evidence.get(key, fallback)`: a present key wins even when its value is null. */
function getOr(evidence: Record<string, unknown>, key: string, fallback: unknown): unknown {
  return Object.hasOwn(evidence, key) && evidence[key] !== undefined ? evidence[key] : fallback;
}

export type EvidenceIdentity = {
  identity_version: string;
  source_family: string;
  independence_group: string;
  source_sha256: string;
  evidence_sha256: string;
};

/** Python `canonical_evidence_identity(evidence, source_sha256=None)`. */
export function canonicalEvidenceIdentity(evidence: Record<string, unknown>, sourceSha256?: string): EvidenceIdentity {
  const sourceRef = pyStrip(pyStr(getOr(evidence, "source_ref", "")));
  const inferredFamily = sourceRef.includes(":") ? sourceRef.split(":", 1)[0] : sourceRef;
  const sourceFamily = identitySegment(pyStr(getOr(evidence, "source_family", inferredFamily)), "unknown-source");
  const independenceGroup = identitySegment(
    pyStr(getOr(evidence, "independence_group", sourceRef || sourceFamily)),
    sourceFamily,
  );
  const sourceSha =
    sourceSha256 ??
    hashPayload(
      toJsonValue(
        getOr(evidence, "source_payload", {
          source_ref: sourceRef,
          content_summary: getOr(evidence, "content_summary", ""),
        }),
      ),
    );
  const identityVersion = pyStr(getOr(evidence, "identity_version", EVIDENCE_IDENTITY_VERSION));
  const identity = orderedObject([
    ["identity_version", identityVersion],
    ["source_family", sourceFamily],
    ["independence_group", independenceGroup],
    ["source_sha256", sourceSha],
  ]);
  return {
    identity_version: identityVersion,
    source_family: sourceFamily,
    independence_group: independenceGroup,
    source_sha256: sourceSha,
    evidence_sha256: hashPayload(identity),
  };
}
