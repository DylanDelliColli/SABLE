# Code Smells

Gaudi cites *named* smells, not vague descriptors. When the audit prefilter or
the audit interview surfaces a problem, name the smell from this catalog, give
the user the plain-language definition, then propose the refactoring technique
that fixes it.

The 23 smells below come from Martin Fowler's *Refactoring* (2nd edition) and
the curated catalog at [refactoring.guru/refactoring/smells](https://refactoring.guru/refactoring/smells).
Five families: Bloaters, Object-Orientation Abusers, Change Preventers,
Dispensables, Couplers.

> **Pedagogical rule:** when citing a smell to the user for the first time in
> a session, give them the plain-language definition and the "why it hurts"
> paragraph. Don't assume they recognize the name. After the first use you may
> cite the smell bare ("this is another instance of Shotgun Surgery").

---

## Family 1 — Bloaters

Code, methods, or classes that grew so large they're hard to work with.

### Long Method

**Definition:** A function or method that does too much in a single body — typically more than ~30–50 lines, or with deeply nested control flow.

**Why it hurts:** Long methods hide their own logic. Readers have to mentally track many variables, branches, and side effects at once. Worse, they're hard to test in isolation — you can't exercise the inner logic without running the whole thing. Most "I don't understand what this code does" moments live inside long methods.

**Fix:** *Extract Method* — pull each cohesive chunk into its own well-named helper. If a method has comments explaining sections, those sections are extraction candidates.

```
// Before
function processOrder(order) { /* 80 lines: validate, price, tax,
  inventory, charge, ship, notify */ }
// After
function processOrder(order) {
  validate(order); applyPricing(order); chargePayment(order);
  reserveInventory(order); dispatchShipment(order); notifyCustomer(order);
}
```

### Large Class

**Definition:** A class with too many fields, methods, or lines of code — often a sign that several responsibilities have been crammed together.

**Why it hurts:** Large classes are hard to understand, hard to reuse, and hard to test. They tend to accumulate unrelated state, and any change risks breaking unrelated behavior. The single-responsibility principle is violated by definition.

**Fix:** *Extract Class* (move a cohesive subset of fields and methods into a new class) or *Extract Subclass* (when the difference is behavioral, not structural).

```
// Before
class User { /* name, email, password, billingAddress, shippingAddress,
  cartItems, orderHistory, loyaltyPoints, paymentMethods... */ }
// After
class User { /* name, email, password */ }
class UserBilling { /* addresses, paymentMethods */ }
class UserCommerce { /* cart, orders, loyalty */ }
```

### Primitive Obsession

**Definition:** Using primitive types (strings, numbers, booleans) to represent domain concepts that deserve their own types.

**Why it hurts:** `userId: string` and `productId: string` look identical to the type system — nothing stops you from passing one where the other is expected. Validation gets scattered (every function that takes a phone number re-parses it). Behavior that belongs with the data (formatting, comparison, parsing) gets duplicated across consumers.

**Fix:** *Replace Data Value with Object*, *Replace Type Code with Class*, or *Introduce Parameter Object* for clumps.

```
// Before
function sendEmail(to: string, subject: string) { /* re-validates email here */ }
// After
class EmailAddress { constructor(raw: string) { /* validates once */ } }
function sendEmail(to: EmailAddress, subject: string) { /* trusts the type */ }
```

### Long Parameter List

**Definition:** A function or method that takes too many parameters — typically more than 4–5.

**Why it hurts:** Long parameter lists are hard to read at the call site, easy to misorder, and signal that the function knows too much about its caller's world. They also resist refactoring — adding one more parameter feels harmless until you realize you've shipped a 9-arg constructor.

**Fix:** *Introduce Parameter Object* (group related params into a struct/class), *Preserve Whole Object* (pass the source object instead of fields), or *Replace Parameter with Method Call* (let the function fetch what it needs).

```
// Before
function createOrder(userId, productId, qty, price, currency, taxRate, shippingMethod, address) { ... }
// After
function createOrder(user, item, options: OrderOptions) { ... }
```

### Data Clumps

**Definition:** The same group of fields appearing together in multiple places — function signatures, class fields, database rows.

**Why it hurts:** Data clumps signal a missing domain concept. Every consumer reasons about the clump in its own way, leading to duplicate validation and inconsistent behavior. The clump is conceptually one thing pretending to be three.

**Fix:** *Extract Class* (give the clump a name and a home) or *Introduce Parameter Object*.

```
// Before — (street, city, zip, country) appears in 6 functions
// After
class Address { street; city; zip; country; format() { ... } }
```

---

## Family 2 — Object-Orientation Abusers

Patterns that misuse inheritance, polymorphism, or class structure.

### Alternative Classes with Different Interfaces

**Definition:** Two or more classes that do conceptually similar work, but expose unrelated methods and types — so callers can't treat them interchangeably.

**Why it hurts:** Callers end up writing per-class adapter code (or worse, branching on the type). The classes are competing implementations of a missing shared interface.

**Fix:** *Rename Method*, *Move Method*, *Extract Superclass*, or *Extract Interface* to harmonize them.

```
// Before
class S3Storage { uploadFile(name, bytes); }
class GcsStorage { putBlob(key, data); }
// After: interface Storage { put(key, bytes): Promise<void> }
class S3Storage implements Storage { put(key, bytes) { ... } }
class GcsStorage implements Storage { put(key, bytes) { ... } }
```

### Refused Bequest

**Definition:** A subclass inherits methods from its parent but doesn't actually use (or worse, overrides them to throw) most of what it inherited.

**Why it hurts:** Inheritance was the wrong tool. The parent's interface doesn't fit the child, and now the parent's contract is a lie. Callers using the parent type can't trust the subclass to honor it (Liskov violation).

**Fix:** *Push Down Method* / *Push Down Field*, or *Replace Inheritance with Delegation*.

### Switch Statements

**Definition:** Switch (or long if/else chains) on a type tag, especially the same switch repeated in several places.

**Why it hurts:** Every new type variant requires editing every switch. Easy to miss one — and when you do, the bug shows up far from the new code. This is one of the most reliable predictors of future bugs in object-oriented code.

**Fix:** *Replace Conditional with Polymorphism* (each case becomes a method on a subclass or strategy), or *Replace Type Code with State/Strategy*.

```
// Before
function area(shape) {
  switch (shape.kind) { case 'circle': ...; case 'rect': ...; }
}
// After
class Circle { area() { ... } }
class Rect { area() { ... } }
```

### Temporary Field

**Definition:** A field on a class that's only set in certain situations and ignored the rest of the time.

**Why it hurts:** Readers can't tell when the field is meaningful. The class's invariants become "well, *sometimes* this is set." Bugs hide in the gap between "field exists" and "field is valid right now."

**Fix:** *Extract Class* for the case where the field is used, or *Introduce Null Object* if the empty state is a real concept.

---

## Family 3 — Change Preventers

Smells that make code expensive to evolve.

### Divergent Change

**Definition:** One module changes for many unrelated reasons — every change in a different part of the system touches the same module.

**Why it hurts:** Unrelated responsibilities are tangled. A change to billing breaks shipping; a change to user profile breaks notifications. The module's locality is broken — instead of concentrating one concept, it spreads many.

**Fix:** *Extract Class* — separate the module along its change axes. Each resulting class should change for one reason only.

### Parallel Inheritance Hierarchies

**Definition:** Every time you add a subclass to hierarchy A, you have to add a matching subclass to hierarchy B.

**Why it hurts:** The duplication isn't in code — it's in *structure*. Adding a new variant takes two coordinated changes that are easy to forget, and forgetting one creates inconsistent state.

**Fix:** *Move Method* / *Move Field* to collapse the second hierarchy into the first, or unify them behind a shared interface.

### Shotgun Surgery

**Definition:** A single logical change requires small edits across many different files or classes.

**Why it hurts:** Easy to miss one. The change isn't local — the concept is spread thin. Every future change of the same shape will have the same cost. This is the inverse of Divergent Change: there, one module sees many reasons to change; here, one reason touches many modules.

**Fix:** *Move Method* / *Move Field* to consolidate the scattered logic into a single module that owns the concept.

```
// Before — adding a new field to "Order" requires touching:
//   types/order.ts, db/orderSchema.ts, api/orderRoutes.ts,
//   ui/orderForm.tsx, validation/orderRules.ts, tests/order.spec.ts
// After — order field definition lives in one place; the rest derives from it.
```

---

## Family 4 — Dispensables

Things in the code that don't earn their keep.

### Comments

**Definition:** Comments that explain *what* the code does (rather than *why* it does it the non-obvious way it does).

**Why it hurts:** "What" comments rot — code changes, comment doesn't. They also signal that the code itself isn't expressive enough. A comment explaining a 40-line function is a request for *Extract Method* with a meaningful name.

**Fix:** *Extract Method* (give the chunk a name that replaces the comment), *Rename Variable* / *Rename Method*, or *Introduce Assertion* (encode the constraint the comment described).

### Duplicate Code

**Definition:** The same (or very similar) code appearing in two or more places.

**Why it hurts:** Every duplicate is a future bug. Fix one, miss the others. Modify one, drift from the others. The literal lines aren't the problem — the *concept* has no single home.

**Fix:** *Extract Method* (if the duplicates are in one class), *Pull Up Method* (across a hierarchy), or *Extract Class* / *Extract Superclass* (if duplicate behavior spans unrelated classes).

### Data Class

**Definition:** A class that's nothing but fields and getters/setters, with no behavior of its own.

**Why it hurts:** Behavior that belongs *with* the data ends up scattered across consumers. Each consumer re-implements the same validation, formatting, comparison logic. The class is a record pretending to be an object.

**Fix:** *Move Method* — find behaviors elsewhere that operate primarily on this class's data and move them in. If the class genuinely should be a record (immutable data with no behavior), that's fine — but most data classes accumulate too much surrounding logic.

### Dead Code

**Definition:** Code that's no longer called from anywhere — unused functions, classes, parameters, or branches.

**Why it hurts:** Readers have to mentally check whether it's really unused (and waste effort doing so). Refactoring tools maintain it. It rots over time and becomes a trap when someone "revives" it without knowing it had bugs.

**Fix:** Delete it. Version control remembers. If you're nervous, mark it deprecated for one release first.

### Lazy Class

**Definition:** A class that does so little it's not pulling its weight — usually a leftover from a refactoring that didn't go far enough.

**Why it hurts:** Adds a layer of indirection for no benefit. Readers have to navigate to it and discover it's almost empty. Apply the **deletion test** ([LANGUAGE.md](LANGUAGE.md)) — if complexity doesn't reappear at callers, the class earned nothing.

**Fix:** *Inline Class* — fold its contents back into the class that uses it, or merge it with a sibling.

### Speculative Generality

**Definition:** Abstractions added for "future use" that no current caller needs — empty methods, unused hook points, unimplemented subclasses, single-implementation interfaces.

**Why it hurts:** Hypothetical seams cost real complexity. Every caller has to navigate the abstraction; every change has to maintain it. And the speculated future usually doesn't arrive — or arrives in a shape the abstraction can't fit.

**Fix:** *Inline Class*, *Collapse Hierarchy*, *Remove Parameter*, or *Rename Method* to a simpler name. Apply "one adapter = hypothetical seam; two = real" — if there's only one implementation, the interface is just indirection.

---

## Family 5 — Couplers

Smells where modules know too much about each other.

### Feature Envy

**Definition:** A method that uses another class's data more than its own — typically reaching into another object's fields to compute something that belongs there.

**Why it hurts:** The method lives in the wrong place. Every change to the other class's data shape forces a change here. Locality is inverted — this method is more about *that* class than its own.

**Fix:** *Move Method* — relocate the method to the class whose data it uses.

```
// Before
class Order {
  total() { return this.items.reduce((s, i) => s + i.price * i.qty, 0); }
}
// After: total() lives on Item, or on a Cart that owns the items.
```

### Inappropriate Intimacy

**Definition:** Two classes that know too much about each other's internals — accessing each other's private fields, depending on internal implementation details.

**Why it hurts:** They can't evolve independently. A refactor of one breaks the other. The seam between them is broken — instead of a clean interface, there's a leaky contract.

**Fix:** *Move Method* / *Move Field* to consolidate the entangled logic, *Change Bidirectional Association to Unidirectional* if the relationship is one-directional in practice, or *Extract Class* to formalize the shared piece.

### Incomplete Library Class

**Definition:** A library or framework class that's almost what you need but missing one method, and you can't modify it.

**Why it hurts:** Callers either reach around the library (fragile) or copy-paste its internals (fragile in a different way).

**Fix:** *Introduce Foreign Method* (a free function that does what the missing method would) or *Introduce Local Extension* (a subclass or wrapper that adds the missing behavior).

### Message Chains

**Definition:** Long chains of getter calls — `a.getB().getC().getD().doThing()` — where the caller has to navigate deep through object structure to reach what it actually wants.

**Why it hurts:** The caller is coupled to the entire chain. Any refactor along the chain breaks the call. The Law of Demeter says "only talk to your immediate friends" — message chains do the opposite.

**Fix:** *Hide Delegate* — add a method to the first object that does the chaining internally and returns just what the caller needs.

```
// Before
const street = user.getAccount().getBilling().getAddress().getStreet();
// After
const street = user.getBillingStreet();
```

### Middle Man

**Definition:** A class whose methods all delegate to another class — it doesn't add behavior, it just forwards.

**Why it hurts:** Pure indirection. The deletion test rejects it. Readers have to navigate through it to find the real implementation.

**Fix:** *Remove Middle Man* (have callers talk directly to the delegate) or *Inline Class*.

---

## How Gaudi cites smells in beads

Every `gaudi-arch-gap` bead names the smell *and* gives the user the
plain-language summary first. Example bead `## Plain-language summary` section:

> The same change to "Order" requires editing 6 files (types, schema, API
> routes, UI form, validation, tests). This is Shotgun Surgery — every future
> field addition pays the same 6-file cost, and missing one creates inconsistent
> state. The fix is to define the field shape in one module and have the other
> 5 derive from it.

Then the technical section names the smell ("Shotgun Surgery — Change
Preventers family"), cites the refactoring technique ("Move Method to
consolidate ownership in a single Order schema module"), and gives the
before/after sketch.
