// Pure: no Lit, so it loads in node tests.
/**
 * How the panel refers to a thing. People and pets are not "it": a thing's
 * own setting wins, then its class (a man is he, a woman she, a phone or tag
 * it), and anything else - a person, a child, a pet, a thing with no class -
 * is "they", which is never wrong the way a guess is.
 */
export const PRONOUNS = {
  he: { subj: "he", obj: "him", poss: "his", is: "is", has: "has", was: "was" },
  she: { subj: "she", obj: "her", poss: "her", is: "is", has: "has", was: "was" },
  they: { subj: "they", obj: "them", poss: "their", is: "are", has: "have", was: "were" },
  it: { subj: "it", obj: "it", poss: "its", is: "is", has: "has", was: "was" },
};
const CLASS_PRONOUNS = { man: "he", woman: "she", person: "they", child: "they", paw: "they", dog: "they", cat: "they" };
export function pronounKey(layout, ent) {
  const own = layout?.thing_pronouns?.[ent];
  if (PRONOUNS[own]) return own;
  const cls = layout?.thing_classes?.[ent];
  return CLASS_PRONOUNS[cls] || (cls ? "it" : "they");
}
export function pronounsFor(layout, ent) { return PRONOUNS[pronounKey(layout, ent)]; }
