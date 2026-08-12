# Working rules (repo-agnostic)

These apply to every target repository. Product-specific law stays in that repo's
own `AGENTS.md`. Do not copy this file into the target.

## One fact, one home

A rule has one living place. Two copies held in step by good intentions is how
they drift. If this text and the target's `AGENTS.md` disagree, say so and follow
the target for product facts, this file for how to reason.

## Enumerate the space, not the exceptions

Name the whole space the rule is about — all paths, all encodings, all
transports, all callers — not the case in front of you.

| Error | Symptom | Response |
|-------|---------|----------|
| Under-draw | Missed case, allowlist thrash, first element only | Widen to the real space; put the rule at the layer that sees it |
| Over-draw | Merged unlike sites; pattern wider than intent | Split rules, or name the difference and check exclusions in the build |

If you are about to add one more case to a denylist or allowlist, stop. The
supply of shapes is usually unbounded. Prefer a better layer over a longer list.

## Close the class, not the instance

A fix that only patches the failing example has not closed anything. Name the
class. If the mechanism is still missing, say so in the artifact (open class),
do not pretend review will hold it.

## Prefer a higher rung

Reach as high as the problem allows:

1. Make the bad state unrepresentable (total signature, type, schema).
2. Delegate to the layer that already has the semantics.
3. One authority, pinned by a census of call sites.
4. A contract test across a **seam** (neither side owns the agreement).
5. Review, convention, a comment — **does not close the class.**

If your change lands on rung 5, keep the class open.

## Boundary vs approximation

A **boundary** is load-bearing (auth, grants, the type system). An
**approximation** (regex, denylist, "read-only" default) is labelled and
optional. Do not sell an approximation as the fix. A bypass of an approximation
is a product bug; a bypass of the boundary is an incident.

## A set is only half a contract

If you assert "every X may Y", also ask "every Y that happens produced a
decision about X". The unwritten converse is where the next hole appears.

## Guards

- Assert the **property you want**, not a proxy that can pass on the fail-closed
  path (a census of "who was asked" is not "the caller's tools survived").
- Derive membership from something **already in the tree** and **orthogonal** to
  the rule. Do not pin a count.
- Mutation-check a new guard: break the thing it protects and confirm it fails.
- Do not put current-state numbers in living docs (test counts, line counts).
  They drift on every normal commit.

## Seams

When a second subsystem, encoding, process, or test-double appears, name the
**pair that must agree**. Neither side owns the agreement. Prefer deriving one
side from the other over testing that two hand lists match. Rank a multi-side
rule at the **weakest crossing**, not the cleanest side.

## One implementation

Before a second caller of any rule, extract the rule. Consolidation alone does
not stop a third copy. Prefer one total step over conditions scattered through
a long procedure.

## Spec is input, not transcript

Design, acceptance criteria, and invariants are written first and then the tree
is made to match. Do not update the design to narrate whatever shipped.

## Before you claim done

- What class did this close, at which rung? What remains representable?
- What stopped carrying? What started? Trace one value across a trust boundary.
- Can the new test pass without ever evaluating the property?
