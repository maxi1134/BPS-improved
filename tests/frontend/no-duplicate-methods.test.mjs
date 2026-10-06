// A class with the same method twice keeps only the last one, silently. That
// cost a day: sextant-devices had two updated(), so the one that opens a
// thing's dialog from the Live page had never run once.
import { test } from "node:test";
import assert from "node:assert";
import { readdirSync, readFileSync } from "node:fs";

const DIR = new URL("../../custom_components/sextant/frontend/", import.meta.url);
const KEYWORDS = new Set(["if", "for", "while", "return", "switch", "catch", "function"]);

test("no class defines the same method twice", () => {
  const offenders = [];
  for (const file of readdirSync(DIR).filter((f) => f.startsWith("sextant-") && f.endsWith(".js"))) {
    let cls = null;
    let seen = new Map();
    readFileSync(new URL(file, DIR), "utf8").split("\n").forEach((line, i) => {
      const start = line.match(/^\s*class\s+(\w+)/);
      if (start) { cls = start[1]; seen = new Map(); return; }
      const m = line.match(/^  (?:async\s+|static\s+|get\s+|set\s+)?([a-zA-Z_]\w*)\s*\(/);
      if (!m || !cls || KEYWORDS.has(m[1])) return;
      if (seen.has(m[1])) offenders.push(`${file}: ${cls}.${m[1]}() at lines ${seen.get(m[1])} and ${i + 1}`);
      seen.set(m[1], i + 1);
    });
  }
  assert.deepEqual(offenders, [], `duplicate methods:\n  ${offenders.join("\n  ")}`);
});
