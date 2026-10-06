// How the panel refers to a thing: never "it" for people and pets.
import { test } from "node:test";
import assert from "node:assert/strict";
import { pronounKey, pronounsFor } from "../../custom_components/sextant/frontend/sextant-pronouns.js";

test("a thing's own pronouns win, then its class, and people and pets default to they", () => {
  const layout = {
    thing_classes: { dave: "man", jo: "woman", kid: "child", meg: "cat", rex: "dog", phone: "phone", keys: "keys", x: "person" },
    thing_pronouns: { meg: "he", rex: "bogus" },
  };
  assert.equal(pronounKey(layout, "meg"), "he");        // set
  assert.equal(pronounKey(layout, "dave"), "he");       // from class
  assert.equal(pronounKey(layout, "jo"), "she");
  assert.equal(pronounKey(layout, "phone"), "it");
  assert.equal(pronounKey(layout, "keys"), "it");
  for (const ent of ["kid", "rex", "x", "nothing-known"]) assert.equal(pronounKey(layout, ent), "they");
  assert.deepEqual(pronounsFor(layout, "kid"), { subj: "they", obj: "them", poss: "their", is: "are", has: "have", was: "were" });
  assert.equal(pronounsFor(undefined, "anything").subj, "they");
});
