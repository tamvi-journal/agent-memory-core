export type Clock = { seconds(): string; micros(): string };

function isoFromEpochMicros(value: bigint, micros: boolean): string {
  const millis = value / 1000n;
  const remainder = value % 1_000_000n;
  const base = new Date(Number(millis)).toISOString().slice(0, 19);
  return micros ? `${base}.${remainder.toString().padStart(6, "0")}+00:00` : `${base}+00:00`;
}

export class WallClock implements Clock {
  #last = -1n;
  readonly wallMillis: () => number;
  constructor(wallMillis: () => number = Date.now) { this.wallMillis = wallMillis; }
  seconds(): string { return new Date(this.wallMillis()).toISOString().replace(/\.\d{3}Z$/u, "+00:00"); }
  micros(): string {
    const candidate = BigInt(Math.trunc(this.wallMillis())) * 1000n;
    const emitted = candidate > this.#last ? candidate : this.#last + 1n;
    this.#last = emitted;
    return isoFromEpochMicros(emitted, true);
  }
}

export class InjectedClock implements Clock {
  #seconds = 0n; #micros = 0n;
  readonly startUs: bigint;
  constructor(start = "2026-09-30T00:00:00.000000+00:00") { this.startUs = parseIsoMicros(start); }
  seconds(): string { const result = isoFromEpochMicros(this.startUs + this.#seconds * 1_000_000n, false); this.#seconds++; return result; }
  micros(): string { this.#micros++; return isoFromEpochMicros(this.startUs + this.#micros, true); }
}

export function parseIsoMicros(value: string): bigint {
  const match = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d{1,6}))?(Z|[+-]\d\d:\d\d)?$/u.exec(value);
  if (!match) throw new RangeError(`invalid ISO timestamp: ${value}`);
  const [, y, mo, d, h, mi, s, fraction = "", zone = ""] = match;
  const offset = zone === "Z" || zone === "" ? 0 : (zone[0] === "-" ? -1 : 1) * (Number(zone.slice(1, 3)) * 60 + Number(zone.slice(4, 6)));
  const utcMs = Date.UTC(Number(y), Number(mo) - 1, Number(d), Number(h), Number(mi) - offset, Number(s));
  return BigInt(utcMs) * 1000n + BigInt(fraction.padEnd(6, "0"));
}

export function isoFromMicros(value: bigint, alwaysFraction = false): string {
  const fraction = value % 1_000_000n;
  if (alwaysFraction || fraction !== 0n) return isoFromEpochMicros(value, true);
  return isoFromEpochMicros(value, false);
}
