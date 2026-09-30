import assert from "node:assert/strict";
import test from "node:test";
import {
  canonicalJson, codePointSlice, hashPayload, normalizeText, orderedObject,
  parseLossless, pyFixed, pyFloat, pyFloatRepr, pyInt, pySplit,
} from "../src/index.ts";

test("typed canonical JSON preserves Python int and float meanings", () => {
  assert.equal(canonicalJson(pyInt(1)), "1");
  assert.equal(canonicalJson(pyFloat(1)), "1.0");
  assert.notEqual(hashPayload(pyInt(1)), hashPayload(pyFloat(1)));
  assert.equal(canonicalJson(orderedObject([["10", pyInt(10)], ["2", pyFloat(2)], ["é", "\u2028"]])), '{"10":10,"2":2.0,"é":"\u2028"}');
});

test("lossless parser preserves written object order and number kind", () => {
  const value = parseLossless('{"10":1,"2":1.0,"nested":{"b":2e0,"a":2}}');
  assert.equal(canonicalJson(value, false), '{"10":1,"2":1.0,"nested":{"b":2.0,"a":2}}');
});

test("Python float formatting and exact binary half-even fixed formatting", () => {
  assert.equal(pyFloatRepr(1e16), "1e+16");
  assert.equal(pyFloatRepr(1e-5), "1e-05");
  assert.equal(pyFloatRepr(1e-4), "0.0001");
  assert.equal(pyFixed(2.675, 2), "2.67");
  assert.equal(pyFixed(2.5, 0), "2");
  assert.equal(pyFixed(3.5, 0), "4");
});

test("code point slicing, Python whitespace, and table boundary separators", () => {
  assert.equal(codePointSlice("😀a", 1), "😀");
  assert.deepEqual(pySplit(" a\u0085b\ufeffc\u200bd\u3000e "), ["a", "b﻿c​d", "e"]);
  assert.equal(normalizeText("a⑴b"), "a 1 b");
  assert.equal(normalizeText("a\u0301b"), "ab");
});

test("canonical encoder rejects lone surrogates and non-finite floats", () => {
  assert.throws(() => canonicalJson("\ud800"), TypeError);
  assert.throws(() => canonicalJson(pyFloat(Number.NaN)), TypeError);
  assert.throws(() => parseLossless('"\\ud800"'), SyntaxError);
  assert.throws(() => parseLossless('"line\nbreak"'), SyntaxError);
  assert.throws(() => parseLossless("1e9999"), SyntaxError);
  assert.equal(parseLossless('"\\ud83d\\ude00"'), "😀");
});
